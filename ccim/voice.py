"""语音输入（#445）：网页录的音在这台电脑上转成文字。

识别跑在单独的进程里（voice_worker.py，`uv run --script` 起）：sherpa-onnx 和模型都大，不进 ccim 的环境，
第一次用时才装、才下载。进程常驻，模型只加载一次，之后一句话一两秒；闲置 IDLE 秒后退出，省内存。
网页边录边分块传（chunk），每块里说完的句子马上识别回去；录音同时存在 recent/ 下，最近 KEEP 段。
"""
import asyncio
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

KEEP = 10                           # 最近几段录音留在 ~/.ccim/voice/recent（16kHz 单声道 int16 裸 PCM）
SESSION_IDLE = 10 * 60              # 一次录音这么久没新块就当它断了
POLISH_MODEL = "haiku"
POLISH_PROMPT = """下面是一段语音识别出来的原话，是用户要发给 AI 助手的消息。把它整理成通顺的书面文字：
- 去掉口头禅、语气词、重复和说错又改口的部分（改口的以后说的为准）
- 修正明显的同音错字、识别错的词（技术名词、英文按常见写法）
- 补全标点；内容长、有好几件事时分段或列成要点
- 不增加、不删减意思，不回答里面的问题，不加任何说明，保持原来的语言和人称
只输出整理后的文字，放在 <整理后></整理后> 里。"""

STEPS = {"start": "正在安装语音识别", "model": "正在下载识别模型", "unpack": "正在解压识别模型", "load": "正在加载识别模型"}


class Engine:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.state = {"state": "idle"}
        self.ready = threading.Event()
        self.waits = {}             # 请求编号 → [Event, 结果]
        self.used = 0.0
        self.sessions = {}          # 边说边识别：录音编号 → 收到第几块、交给识别进程多少字节了
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
                self._expire()
                p = self.proc
                if p and self.ready.is_set() and not self.waits and not self.sessions and time.time() - self.used > IDLE:
                    self.state = {"state": "idle"}
                    p.terminate()

    def _ask(self, msg, pcm):
        """把一段 PCM 交给识别进程，等它回话。"""
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
                p.stdin.write(json.dumps(dict(msg, id=rid, path=path)) + "\n")
                p.stdin.flush()
            w[0].wait(120)
            r = w[1]
            if not r:
                raise ValueError("识别超时了，再试一次。")
            if r.get("error"):
                log.warning("语音识别出错：%s", r["error"])
                raise ValueError("识别出错了，再试一次。")
            return r.get("text") or ""
        finally:
            self.waits.pop(rid, None)
            self.used = time.time()
            try:
                os.remove(path)
            except OSError:
                pass

    def _wait_ready(self, wait):
        """准备好了返回 None，没准备好返回 {"preparing": 状态}。"""
        if self.state.get("state") == "error" or not (self.proc and self.proc.poll() is None):
            self.prepare()
        if self.state.get("state") == "error":
            raise ValueError(self.state["error"])
        if not self.ready.wait(wait):
            if self.state.get("state") == "error":
                raise ValueError(self.state["error"])
            return {"preparing": self.status()}
        return None

    def transcribe(self, pcm, wait=8):
        """pcm：16kHz 单声道 int16。返回 {"text"}，还没准备好时返回 {"preparing": 状态}。"""
        return self._wait_ready(wait) or {"text": self._ask({}, pcm)}

    def chunk(self, sid, seq, pcm, final):
        """边说边识别（#556）：一次录音分块传来，seq 从 0 数起，重发的块不重复收。

        每块先追加到 recent/ 下这次录音的文件里（识别出了问题录音也还在），再把还没交给识别进程的部分交过去，
        返回这一块里说完的句子 {"text"}；识别还没准备好时先只存着，返回 {"preparing"}，下一块一起补上。
        """
        if not sid or not all(c.isalnum() or c == "-" for c in sid) or len(sid) > 40:
            raise ValueError("录音编号不对")
        with self.lock:
            ses = self.sessions.get(sid)
            if not ses:
                self._trim_recent()
                ses = self.sessions[sid] = {"seq": -1, "fed": 0, "gen": None, "lost": False, "lock": threading.Lock()}
                old = os.path.join(self._recent(), sid + ".pcm")
                if os.path.exists(old):                  # ccim 中途重启过：前面的已经识别过了，接着往后；缺的那半句让网页带上录音
                    ses.update(fed=os.path.getsize(old) // 2 * 2, lost=True, seq=seq - 1)
            ses["used"] = time.time()
        with ses["lock"]:
            path = os.path.join(self._recent(), sid + ".pcm")
            if seq > ses["seq"]:
                with open(path, "ab") as f:
                    f.write(pcm)
                ses["seq"] = seq
            busy = self._wait_ready(8 if final else 0)
            if busy:
                return busy
            if ses["gen"] is not self.proc:
                if ses["gen"] is not None:                # 识别进程中途换过：前面说了一半的那句接不上了，告诉网页把录音也带上
                    log.info("语音识别进程换过，录音 %s 可能缺了半句", sid)
                    ses["lost"] = True
                ses["gen"] = self.proc
            with open(path, "rb") as f:
                f.seek(ses["fed"])
                new = f.read()
            new = new[:len(new) // 2 * 2]
            if not new and not final:
                return {"text": ""}
            text = self._ask({"sid": sid, "final": bool(final)}, new)
            ses["fed"] += len(new)
            if final:
                with self.lock:
                    self.sessions.pop(sid, None)
                return {"text": text, "lost": ses["lost"]}
            return {"text": text}

    def _recent(self):
        d = os.path.join(self._dir(), "recent")
        os.makedirs(d, exist_ok=True)
        return d

    def _trim_recent(self):
        """最近的录音留 KEEP 段，多的删掉。"""
        d = self._recent()
        try:
            fs = sorted((os.path.join(d, f) for f in os.listdir(d) if f.endswith(".pcm")), key=os.path.getmtime)
        except OSError:
            return
        for f in fs[:-KEEP]:
            try:
                os.remove(f)
            except OSError:
                pass

    def _expire(self):
        now = time.time()
        for sid, ses in list(self.sessions.items()):
            if now - ses.get("used", now) > SESSION_IDLE:
                self.sessions.pop(sid, None)
                p = self.proc
                if p and ses["gen"] is p:
                    try:
                        p.stdin.write(json.dumps({"drop": sid}) + "\n")
                        p.stdin.flush()
                    except (OSError, ValueError):
                        pass


async def polish(text):
    """识别出的原话整理一遍（#556）：去口头禅、修正明显的同音错字、补标点、长了分段；整理不出来就原样返回。"""
    text = (text or "").strip()
    if not 6 <= len(text) <= 6000:                       # 太长的整理不全，宁可不整理
        return {"text": text}
    try:
        out = await asyncio.wait_for(_polish(text), 90)
    except Exception:
        log.warning("整理语音文字出错", exc_info=True)
        return {"text": text}
    if not out or len(out) > len(text) * 1.5 + 40 or len(out) < len(text) * 0.3:   # 空的、明显加了或丢了东西：不用
        return {"text": text}
    return {"text": out}


async def _polish(text):
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query
    from .session import CLI
    opts = ClaudeAgentOptions(model=POLISH_MODEL, max_turns=1, allowed_tools=[], setting_sources=[],
                              cwd=os.path.expanduser("~"), cli_path=CLI, permission_mode="default",
                              extra_args={"no-session-persistence": None})   # 不留对话记录（#67）
    out = ""
    async for m in query(prompt=POLISH_PROMPT + "\n<原话>\n" + text + "\n</原话>", options=opts):
        if isinstance(m, AssistantMessage):
            for b in m.content:
                if isinstance(b, TextBlock):
                    out += b.text
    return _unwrap(out)


def _unwrap(out):
    out = out.strip()
    a, b = out.find("<整理后>"), out.rfind("</整理后>")
    if a >= 0:
        out = out[a + len("<整理后>"):b if b > a else None]
    return out.strip()


ENGINE = Engine()
