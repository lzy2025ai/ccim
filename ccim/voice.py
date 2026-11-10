"""语音输入（#445）：网页录的音在这台电脑上转成文字。

识别跑在单独的进程里（voice_worker.py，`uv run --script` 起）：sherpa-onnx 和模型都大，不进 ccim 的环境，
第一次用时才装、才下载。进程常驻，模型只加载一次，之后一句话一两秒；闲置 IDLE 秒后退出，省内存。
"""
import json
import logging
import os
import subprocess
import threading
import time
import uuid

from . import registry

log = logging.getLogger("ccim.voice")

IDLE = 30 * 60
LIMIT = 20 * 1024 * 1024            # 16kHz 单声道 int16 一秒 32KB，网页最长录 5 分钟（约 9.6MB）

STEPS = {"start": "正在安装语音识别", "model": "正在下载识别模型", "unpack": "正在解压识别模型", "load": "正在加载识别模型"}


class Engine:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.state = {"state": "idle"}
        self.ready = threading.Event()
        self.waits = {}             # 请求编号 → [Event, 结果]
        self.used = 0.0
        threading.Thread(target=self._reaper, daemon=True).start()

    def _dir(self):
        d = os.path.join(registry.HOME, "voice")
        os.makedirs(d, exist_ok=True)
        return d

    def status(self):
        s = dict(self.state)
        if s.get("state") == "preparing":
            s["text"] = STEPS.get(s.get("step"), STEPS["start"]) + (f" {s['pct']}%" if s.get("pct") is not None else "")
        return s

    def prepare(self):
        """开始准备（装依赖、下模型、加载），马上返回状态。"""
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return self.status()
            from .upgrade import _uv
            uv = _uv()
            if not uv:
                self.state = {"state": "error", "error": "这台电脑上找不到 uv，用不了语音输入。"}
                return self.status()
            self.ready.clear()
            self.state = {"state": "preparing", "step": "start"}
            d = self._dir()
            err = open(os.path.join(d, "worker.log"), "ab")
            self.proc = subprocess.Popen(
                [uv, "run", "--quiet", "--no-project", "--script", os.path.join(os.path.dirname(__file__), "voice_worker.py"),
                 os.path.join(d, "models")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, cwd=d, text=True, bufsize=1,
                start_new_session=True)
            err.close()
            self.used = time.time()
            threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
            return self.status()

    def _read(self, proc):
        for line in proc.stdout:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("ready"):
                self.state = {"state": "ready"}
                self.ready.set()
            elif m.get("stage"):
                self.state = {"state": "preparing", "step": m["stage"], "pct": m.get("pct")}
            elif m.get("id") in self.waits:
                w = self.waits[m["id"]]
                w[1] = m
                w[0].set()
        code = proc.wait()
        if self.proc is proc:
            if self.state.get("state") != "idle":           # 不是闲置时我们自己关的
                log.warning("语音识别进程退出了（%s）", code)
                self.state = {"state": "idle"} if self.ready.is_set() else \
                    {"state": "error", "error": "语音识别没准备好，网络不好时可能下载失败，再试一次。"}
            self.ready.clear()
            self.proc = None
        for w in list(self.waits.values()):
            w[0].set()

    def _reaper(self):
        while True:
            time.sleep(60)
            with self.lock:
                p = self.proc
                if p and self.ready.is_set() and not self.waits and time.time() - self.used > IDLE:
                    self.state = {"state": "idle"}
                    p.terminate()

    def transcribe(self, pcm, wait=8):
        """pcm：16kHz 单声道 int16。返回 {"text"}，还没准备好时返回 {"preparing": 状态}。"""
        if self.state.get("state") == "error" or not (self.proc and self.proc.poll() is None):
            self.prepare()
        if self.state.get("state") == "error":
            raise ValueError(self.state["error"])
        if not self.ready.wait(wait):
            if self.state.get("state") == "error":
                raise ValueError(self.state["error"])
            return {"preparing": self.status()}
        rid = uuid.uuid4().hex[:10]
        path = os.path.join(self._dir(), rid + ".pcm")
        with open(path, "wb") as f:
            f.write(pcm)
        w = self.waits[rid] = [threading.Event(), None]
        try:
            self.used = time.time()
            with self.lock:
                p = self.proc
                if not p:
                    raise ValueError("语音识别出错了，再试一次。")
                p.stdin.write(json.dumps({"id": rid, "path": path}) + "\n")
                p.stdin.flush()
            w[0].wait(120)
            r = w[1]
            if not r:
                raise ValueError("识别超时了，再试一次。")
            if r.get("error"):
                log.warning("语音识别出错：%s", r["error"])
                raise ValueError("识别出错了，再试一次。")
            return {"text": r.get("text") or ""}
        finally:
            self.waits.pop(rid, None)
            self.used = time.time()
            try:
                os.remove(path)
            except OSError:
                pass


ENGINE = Engine()
