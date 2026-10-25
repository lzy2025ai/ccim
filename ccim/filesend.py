"""对话里的文件发给浏览器（#76）：边读边发，支持 Range（断点续传、视频拖动）和 ETag（没变就 304）。

本机的文件直接从磁盘按块读。别的电脑上的文件按段取（/api/file/part，一段 PART 字节，经现有的转交通道，
签名照旧），收到一段就往浏览器写一段，同时预取后面 AHEAD 段；内存里只放这几段，所以没有大小上限。
"""
import json, logging, mimetypes, os, re, threading
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
from urllib.parse import parse_qs, quote, urlencode, urlparse

log = logging.getLogger("ccim.filesend")

BLOCK = 1 << 20                     # 本机读盘一次读这么多
PART = 4 << 20                      # 跨电脑一段取这么多
FIRST = 256 << 10                   # 第一段
AHEAD = 2                           # 跨电脑预取几段


class Gone(Exception):
    """文件不在了，或者传到一半被改了。"""


def meta(path):
    st = os.stat(path)
    return {"size": st.st_size, "mtime": st.st_mtime,
            "etag": f'"{st.st_size:x}-{st.st_mtime_ns:x}"',
            "ctype": mimetypes.guess_type(path)[0] or "application/octet-stream",
            "name": os.path.basename(path)}


def read_part(path, start, length):
    """/api/file/part 的内容：一行 JSON（文件信息）+ 换行 + 这一段的字节。"""
    m = meta(path)
    data = b""
    if 0 <= start < m["size"] and length > 0:
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read(min(length, PART))
    return json.dumps(m).encode() + b"\n" + data


def parse_part(blob):
    head, _, data = blob.partition(b"\n")
    return json.loads(head), data


def _range(header, size):
    """Range 头 → (开始, 结束含)；没有或不认识返回 None；超出范围返回 False。只认单段。"""
    m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", header or "")
    if not m or not (m.group(1) or m.group(2)):
        return None
    a, b = m.group(1), m.group(2)
    if a:
        start, end = int(a), min(int(b), size - 1) if b else size - 1
    else:
        start, end = max(size - int(b), 0), size - 1
    if start >= size or start > end:
        return False
    return start, end


def _plan(handler, m):
    """按请求头算出回应：返回 (状态码, 开始, 结束含)；304 / 416 已经回过了返回 None。"""
    h, size = handler.headers, m["size"]
    inm = h.get("If-None-Match")
    if inm and m["etag"] in [t.strip() for t in inm.split(",")]:
        handler.send_response(304)
        _common(handler, m, None)
        handler.end_headers()
        return None
    r = _range(h.get("Range"), size)
    if r and h.get("If-Range") and h.get("If-Range") != m["etag"]:
        r = None                      # 浏览器手里那份是旧的，整个重发
    if r is False:
        handler.send_response(416)
        handler.send_header("Content-Range", f"bytes */{size}")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return None
    if r:
        return 206, r[0], r[1]
    return 200, 0, size - 1


def _common(handler, m, extra):
    handler.send_header("ETag", m["etag"])
    handler.send_header("Last-Modified", formatdate(m["mtime"], usegmt=True))
    handler.send_header("Cache-Control", "private, no-cache")
    handler.send_header("Accept-Ranges", "bytes")
    for k, v in (extra or {}).items():
        handler.send_header(k, v)


def disposition(name):
    """filename* 给认 RFC 5987 的浏览器；只认 filename= 的（安卓、应用内浏览器）不认就拿地址最后一段当名字，
    所以也带一份百分号编码的 filename=，它们会解码。"""
    q = quote(name)
    return f"inline; filename=\"{q}\"; filename*=UTF-8''{q}"


def _head(handler, m, code, start, end, extra):
    handler.send_response(code)
    handler.send_header("Content-Type", m["ctype"])
    handler.send_header("Content-Length", str(end - start + 1))
    handler.send_header("Content-Disposition", disposition(m["name"]))
    if code == 206:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{m['size']}")
    _common(handler, m, extra)
    handler.end_headers()


def serve_local(handler, path, extra=None):
    m = meta(path)
    plan = _plan(handler, m)
    if not plan:
        return
    code, start, end = plan
    with open(path, "rb") as f:
        _head(handler, m, code, start, end, extra)
        f.seek(start)
        left = end - start + 1
        try:
            while left > 0:
                data = f.read(min(BLOCK, left))
                if not data:
                    break
                handler.wfile.write(data)
                left -= len(data)
        except (BrokenPipeError, ConnectionResetError):
            pass
    if left > 0:                      # 文件变短了：长度对不上，断开让浏览器知道没下完
        handler.close_connection = True


def serve_remote(handler, fetch, extra=None):
    """fetch(开始, 长度) → (文件信息, 字节)，取不到抛异常（Gone 表示 404）。"""
    m, _ = fetch(0, 0)
    plan = _plan(handler, m)
    if not plan:
        return
    code, start, end = plan
    total = end - start + 1
    segs, off = [], start                 # 第一段小一点，浏览器早点看到进度；后面按 PART 取
    while off <= end:
        n = min(FIRST if not segs else PART, end + 1 - off)
        segs.append((off, n))
        off += n
    pool = ThreadPoolExecutor(max_workers=AHEAD + 1)
    stop = threading.Event()

    def get(seg):
        if stop.is_set():
            return None
        mm, data = fetch(*seg)
        if mm["etag"] != m["etag"]:
            raise Gone("文件传到一半被改了")
        return data

    futs = [pool.submit(get, sg) for sg in segs[:AHEAD + 1]]
    nxt, sent = len(futs), 0
    _head(handler, m, code, start, end, extra)   # 文件信息有了就发头，浏览器马上进入下载
    try:
        for i in range(len(segs)):
            if nxt < len(segs):
                futs.append(pool.submit(get, segs[nxt]))
                nxt += 1
            data = futs[i].result()
            if data:
                handler.wfile.write(data)
                sent += len(data)
    except (BrokenPipeError, ConnectionResetError, Gone):
        pass
    except Exception:
        log.warning("取文件中途出错", exc_info=True)
    finally:
        stop.set()
        pool.shutdown(wait=False, cancel_futures=True)
    if sent < total:                  # 传到一半断了：断开连接，浏览器知道没下完，可以续传
        handler.close_connection = True


def serve_peer(handler, peers, mid, extra=None, headers=None):
    """文件在别的电脑上（网页请求的 /api/file 带 machine）：按段取，边收边发。错误用 handler._send 回。"""
    from .peers import Offline
    q = {k: v[0] for k, v in parse_qs(urlparse(handler.path).query).items()}
    base = "/api/file/part?" + urlencode({"chat": q.get("chat", ""), "path": q.get("path", "")})

    def fetch(start, n):
        status, _, data = peers.forward(mid, "GET", f"{base}&start={start}&len={n}", b"", headers or {})
        if status == 404:
            raise Gone()
        if status != 200:
            raise RuntimeError(f"那台电脑回应 {status}")
        return parse_part(data)
    try:
        serve_remote(handler, fetch, extra)
    except Gone:
        # 没这个文件，或者那台电脑还是旧版本（没有分段接口）：照原来的整个取过来，结果原样转回
        try:
            status, ctype, data = peers.forward(mid, "GET", handler.path, b"", headers or {})
        except Offline:
            return _offline(handler, peers, mid)
        handler._send(status, data, ctype)
    except Offline:
        _offline(handler, peers, mid)
    except Exception as e:
        log.exception("取文件出错")
        handler._send(502, {"error": f"出错了：{e}"})


def _offline(handler, peers, mid):
    name = next((m["name"] for m in peers.machines() if m["id"] == mid), "那台电脑")
    handler._send(503, {"error": f"「{name}」现在离线，等它连上再试。"})
