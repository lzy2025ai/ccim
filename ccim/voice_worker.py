# /// script
# requires-python = ">=3.9"
# dependencies = ["sherpa-onnx>=1.12", "numpy"]
# ///
"""语音输入的识别进程（#445）：由 ccim/voice.py 用 `uv run --script` 起，不导入 ccim 的任何东西。

依赖（sherpa-onnx、numpy）写在上面，uv 第一次跑时装进它自己的缓存，不进 ccim 的环境（体积大，大多数人用不到）。
模型第一次用时下载。一行一个 JSON 和父进程说话：
  收：{"id": ..., "path": 16kHz 单声道 int16 的裸 PCM 文件}
  发：{"stage": "model", "pct": 0-100}（下载模型）、{"ready": true}、{"id": ..., "text": ...} 或 {"id": ..., "error": ...}
"""
import json
import os
import sys
import tarfile
import urllib.request

MODEL = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"   # 2025-09-09 那版是粤语微调，普通话没有标点
RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
RATE = 16000

# 这个文件放在 ccim 包里，同目录有 platform.py 等，会挡住标准库的同名模块（numpy 要 import platform）
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _HERE]


def say(**k):
    sys.stdout.write(json.dumps(k, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def download(url, dst, lo, hi):
    tmp = dst + ".part"
    with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done, last = 0, -1
        while True:
            buf = resp.read(1 << 20)
            if not buf:
                break
            f.write(buf)
            done += len(buf)
            pct = lo + (hi - lo) * done // total if total else lo
            if pct != last:
                last = pct
                say(stage="model", pct=pct)
    os.replace(tmp, dst)


def models(root):
    """模型在哪：已经有了就直接用（media-transcribe 下过的也认），没有就下到 root。"""
    shared = os.environ.get("MEDIA_TRANSCRIBE_MODELS") or os.path.expanduser("~/.cache/media-transcribe/models")
    for d in (root, shared):
        if os.path.isfile(os.path.join(d, MODEL, "tokens.txt")) and os.path.isfile(os.path.join(d, "silero_vad.onnx")):
            break
    else:
        d = root
        os.makedirs(d, exist_ok=True)
        if not os.path.isfile(os.path.join(d, MODEL, "tokens.txt")):
            arc = os.path.join(d, MODEL + ".tar.bz2")
            download(RELEASE + MODEL + ".tar.bz2", arc, 0, 95)
            say(stage="unpack")
            with tarfile.open(arc) as t:
                if hasattr(tarfile, "data_filter"):
                    t.extractall(d, filter="data")
                else:
                    t.extractall(d)
            os.remove(arc)
        if not os.path.isfile(os.path.join(d, "silero_vad.onnx")):
            download(RELEASE + "silero_vad.onnx", os.path.join(d, "silero_vad.onnx"), 95, 100)
    mdir = os.path.join(d, MODEL)
    onnx = [f for f in os.listdir(mdir) if f.endswith(".onnx")]
    model = os.path.join(mdir, "model.int8.onnx" if "model.int8.onnx" in onnx else onnx[0])
    return model, os.path.join(mdir, "tokens.txt"), os.path.join(d, "silero_vad.onnx")


def join(parts):
    out = ""
    for p in parts:
        if out and out[-1].isascii() and out[-1].isalnum() and p[:1].isascii() and p[:1].isalnum():
            out += " "
        out += p
    return out


def main():
    import numpy as np
    import sherpa_onnx

    model, tokens, vad_model = models(sys.argv[1])
    say(stage="load")
    rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=model, tokens=tokens, num_threads=max(1, min(4, (os.cpu_count() or 2) // 2)),
        language="auto", use_itn=True)
    cfg = sherpa_onnx.VadModelConfig()
    cfg.silero_vad.model = vad_model
    cfg.silero_vad.threshold = 0.5
    cfg.silero_vad.min_silence_duration = 0.5
    cfg.silero_vad.min_speech_duration = 0.25
    cfg.silero_vad.max_speech_duration = 20       # SenseVoice 一段不宜超过 30 秒
    cfg.sample_rate = RATE
    cfg.num_threads = 1
    say(ready=True)

    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        try:
            with open(req["path"], "rb") as f:
                pcm = np.frombuffer(f.read(), dtype=np.int16).astype(np.float32) / 32768.0
            vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=max(30, len(pcm) // RATE + 10))
            pieces, win = [], cfg.silero_vad.window_size
            for i in range(0, len(pcm), win):
                vad.accept_waveform(pcm[i:i + win])
                while not vad.empty():
                    pieces.append(vad.front.samples)
                    vad.pop()
            vad.flush()
            while not vad.empty():
                pieces.append(vad.front.samples)
                vad.pop()
            streams = []
            for p in pieces:
                st = rec.create_stream()
                st.accept_waveform(RATE, p)
                streams.append(st)
            if streams:
                rec.decode_streams(streams)
            say(id=req.get("id"), text=join([s.result.text.strip() for s in streams if s.result.text.strip()]))
        except Exception as e:
            say(id=req.get("id"), error=str(e))


if __name__ == "__main__":
    main()
