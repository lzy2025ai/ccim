"""ccim 的工具作为标准 MCP 服务（stdio，#178）：Codex 等底座启动时挂上它。

只做转发：工具列表和调用都交给 ccim 进程的 socket（见 toolsock.py），凭 CCIM_TOKEN 认出是哪个对话。
这个文件不 import ccim 的任何东西，启动时用 `python -I 这个文件` 跑：不读环境里的 PYTHONPATH，
也不把所在目录（ccim/，里面有 platform.py 等）放进 sys.path，免得挡住标准库。
`ccim tool` 命令也用这里的 call()。
"""
import json, os, socket, sys

PROTOCOL = "2025-06-18"


def call(req, timeout=None):
    """发一个请求给 ccim 进程，返回回应（dict）。连不上抛 OSError。"""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(os.environ["CCIM_TOOLS"])
        s.sendall((json.dumps(dict(req, token=os.environ.get("CCIM_TOKEN", "")), ensure_ascii=False) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    return json.loads(buf or b"{}")


def _text(msg):
    return {"content": [{"type": "text", "text": msg}], "isError": True}


def _result(res):
    """SDK 工具的返回（is_error）→ MCP 的（isError）。"""
    out = {"content": res.get("content") or []}
    if res.get("is_error") or res.get("isError"):
        out["isError"] = True
    return out


def _answer(msg):
    meth, params = msg.get("method"), msg.get("params") or {}
    if meth == "initialize":
        return {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                "capabilities": {"tools": {}}, "serverInfo": {"name": "ccim", "version": "1"}}
    if meth == "ping":
        return {}
    if meth == "tools/list":
        try:
            r = call({"op": "list"}, timeout=30)
        except OSError:
            return {"tools": []}
        return {"tools": r.get("tools") or []}
    if meth == "tools/call":
        try:
            r = call({"op": "call", "name": params.get("name"), "args": params.get("arguments") or {}})
        except (OSError, ValueError):
            return _text("ccim 连不上了（可能正在重启），稍后再试。")
        return _text(r["error"]) if r.get("error") else _result(r.get("result") or {})
    return None


def main():
    import threading
    lock = threading.Lock()

    def one(msg):
        res = _answer(msg)
        out = {"jsonrpc": "2.0", "id": msg["id"]}
        if res is None:
            out["error"] = {"code": -32601, "message": f"不支持 {msg.get('method')}"}
        else:
            out["result"] = res
        with lock:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "id" not in msg:                   # 通知，不用回
            continue
        # 每个请求单独一个线程：问别的 Agent 要等很久，不能挡住同时来的别的调用
        threading.Thread(target=one, args=(msg,), daemon=True).start()


if __name__ == "__main__":
    main()
