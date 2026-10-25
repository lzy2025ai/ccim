"""对话里的文档预览和编辑（#34）：Markdown、HTML 能看能改，Word 只能看。

Agent 每发一次文档存一份副本（snaps/<对话>/），面板里能切换看各版本；最新的一版读原文件。
文档里用相对路径引用的图片（HTML 还有样式表、脚本）读出来打包成 data: 地址一起交给网页，
这样别的电脑、服务器上打开也能显示，HTML 放进隔离的 iframe（srcdoc）里也不用再请求。
"""
import base64, html as htmllib, mimetypes, os, re, shutil, time, uuid, zipfile
import xml.etree.ElementTree as ET

KINDS = {".md": "md", ".markdown": "md", ".html": "html", ".htm": "html", ".docx": "docx"}
# 工作区（#206）里能直接看、直接改的纯文本和代码
TEXT_EXT = {".txt", ".text", ".log", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
            ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte", ".css", ".scss", ".less", ".sh", ".zsh", ".bash",
            ".go", ".rs", ".java", ".kt", ".swift", ".c", ".h", ".cc", ".cpp", ".hpp", ".m", ".rb", ".php", ".lua", ".sql",
            ".xml", ".svg", ".gitignore", ".dockerfile", ".r", ".tex", ".bib", ".srt", ".vtt"}
TEXT_NAMES = {"Makefile", "Dockerfile", "LICENSE", "README", ".gitignore", ".env"}
EDITABLE = ("md", "html", "text")
ASSET_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".avif", ".css", ".js", ".mp4", ".webm"}
MAX_DOC = 20 << 20          # 文档本身
MAX_ASSET = 15 << 20        # 单个附带文件
MAX_ASSETS = 40 << 20       # 附带文件合计


class Problem(Exception):
    pass


def kind(name):
    ext = os.path.splitext(name or "")[1].lower()
    if ext in KINDS:
        return KINDS[ext]
    if ext in TEXT_EXT or os.path.basename(name or "") in TEXT_NAMES:
        return "text"
    return None


def snap_dir(folder, chat):
    return os.path.join(folder, "snaps", re.sub(r"[^\w-]", "_", chat))


def snapshot(folder, chat, path):
    """存一份副本，返回副本路径；不是文档或太大就不存。"""
    if not kind(path):
        return None
    try:
        if os.path.getsize(path) > MAX_DOC:
            return None
        d = snap_dir(folder, chat)
        os.makedirs(d, exist_ok=True)
        dest = os.path.join(d, f"{uuid.uuid4().hex[:10]}-{os.path.basename(path)}")
        shutil.copy(path, dest)
        return dest
    except OSError:
        return None


def remove_snaps(folder, chat):
    shutil.rmtree(snap_dir(folder, chat), ignore_errors=True)


def _read(path):
    try:
        if os.path.getsize(path) > MAX_DOC:
            raise Problem("文件太大，打不开预览。")
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        raise Problem("文件已经不在了。")
    except OSError as e:
        raise Problem(f"读不了这个文件：{e.strerror or e}")


def _data_uri(path, data=None):
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    if path.lower().endswith(".svg"):
        ctype = "image/svg+xml"
    if data is None:
        with open(path, "rb") as f:
            data = f.read()
    return f"data:{ctype};base64,{base64.b64encode(data).decode()}"


class _Assets:
    """文档里引用的本地文件：按引用时写的样子（相对路径）记下 data: 地址。"""

    def __init__(self, base):
        self.base, self.map, self.total = base, {}, 0

    def get(self, ref):
        ref = htmllib.unescape(ref.strip())
        if ref in self.map:
            return self.map[ref]
        if not ref or re.match(r"^([a-z][\w+.-]*:|//|#)", ref, re.I):
            return None
        p = ref.split("#")[0].split("?")[0]
        try:
            from urllib.parse import unquote
            p = unquote(p)
        except Exception:
            pass
        full = os.path.realpath(p if os.path.isabs(p) else os.path.join(self.base, p))
        if os.path.splitext(full)[1].lower() not in ASSET_EXT or not os.path.isfile(full):
            return None
        size = os.path.getsize(full)
        if size > MAX_ASSET or self.total + size > MAX_ASSETS:
            return None
        self.total += size
        self.map[ref] = _data_uri(full)
        return self.map[ref]


MD_IMG = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
HTML_REF = re.compile(r"""(\s(?:src|href|poster)\s*=\s*)(["'])([^"']+)\2""", re.I)
CSS_URL = re.compile(r"""url\(\s*(["']?)([^)"']+)\1\s*\)""", re.I)
SCRIPT = re.compile(r"(<script\b[^>]*>)(.*?)(</script\s*>|$)", re.I | re.S)


def _md_assets(text, base):
    a = _Assets(base)
    for m in MD_IMG.finditer(text):
        a.get(m.group(1))
    for m in HTML_REF.finditer(text):          # Markdown 里直接写的 <img src=…>
        a.get(m.group(3))
    return a.map


def _inline_html(text, base):
    """把相对路径引用的图片、样式表、脚本换成 data: 地址（样式表里的 url() 也换）。"""
    a = _Assets(base)

    def ref(m):
        uri = a.get(m.group(3))
        if uri and m.group(3).lower().split("?")[0].endswith(".css"):
            css_path = os.path.realpath(os.path.join(base, htmllib.unescape(m.group(3)).split("?")[0]))
            try:
                with open(css_path, encoding="utf-8", errors="replace") as f:
                    css = f.read()
                sub = _Assets(os.path.dirname(css_path))
                css = CSS_URL.sub(lambda u: f'url("{sub.get(u.group(2)) or u.group(2)}")', css)
                uri = _data_uri(css_path, css.encode())
            except OSError:
                pass
        return f"{m.group(1)}{m.group(2)}{uri or m.group(3)}{m.group(2)}" if uri else m.group(0)

    def css(u):
        uri = a.get(u.group(2))
        return f'url("{uri}")' if uri else u.group(0)

    def part(t):
        return CSS_URL.sub(css, HTML_REF.sub(ref, t))

    # <script> 里是代码，不能按 HTML / CSS 去换（getDataURL(t) 会被当成 url(t) 改坏）；只换开头标签里的 src
    out, pos = [], 0
    for m in SCRIPT.finditer(text):
        out += [part(text[pos:m.start()]), HTML_REF.sub(ref, m.group(1)), m.group(2), m.group(3)]
        pos = m.end()
    out.append(part(text[pos:]))
    return "".join(out)


def view(path, base=None):
    """给预览面板的内容。path 可以是副本，base 是原文件所在的目录（相对路径按它算）。"""
    k = kind(path)
    if not k:
        raise Problem("这种文件不能预览。")
    base = base or os.path.dirname(path)
    data = _read(path)
    out = {"kind": k, "mtime": os.path.getmtime(path), "editable": k in EDITABLE}
    if k == "docx":
        out["html"] = docx_html(path)
        return out
    text = data.decode("utf-8", errors="replace")
    out["text"] = text
    if k == "text":
        return out
    if k == "md":
        out["assets"] = _md_assets(text, base)
    else:
        out["html"] = _inline_html(text, base)
    return out


def save(path, text, mtime):
    """写回原文件。打开以后文件被别人（Agent）改过就不写，免得盖掉。"""
    if not kind(path) in EDITABLE:
        raise Problem("这种文件不能在这里改。")
    try:
        now = os.path.getmtime(path)
    except OSError:
        raise Problem("文件已经不在了。")
    if mtime is not None and abs(now - float(mtime)) > 0.001:
        raise Problem("这个文件刚被改过，重新打开看看最新的再改。")
    tmp = f"{path}.ccim-{uuid.uuid4().hex[:6]}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        shutil.copymode(path, tmp)
    except OSError:
        pass
    os.replace(tmp, path)
    return os.path.getmtime(path)


# ---------- Word ----------

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
V = "{urn:schemas-microsoft-com:vml}"
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def docx_html(path):
    """够看就行的 Word 转网页：标题、段落、加粗斜体下划线删除线、链接、列表、表格、图片。"""
    try:
        z = zipfile.ZipFile(path)
        doc = ET.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        raise Problem("这个 Word 文件打不开。")
    rels = {}
    try:
        for r in ET.fromstring(z.read("word/_rels/document.xml.rels")).iter(REL + "Relationship"):
            rels[r.get("Id")] = (r.get("Target"), r.get("TargetMode"))
    except (KeyError, ET.ParseError):
        pass
    styles = {}
    try:
        for s in ET.fromstring(z.read("word/styles.xml")).iter(W + "style"):
            n = s.find(W + "name")
            styles[s.get(W + "styleId")] = (n.get(W + "val") if n is not None else "").lower()
    except (KeyError, ET.ParseError):
        pass
    total = [0]

    def image(rid):
        target = (rels.get(rid) or (None, None))[0]
        if not target:
            return ""
        name = "word/" + target if not target.startswith("/") else target[1:]
        try:
            data = z.read(os.path.normpath(name).replace("\\", "/"))
        except KeyError:
            return ""
        if total[0] + len(data) > MAX_ASSETS:
            return ""
        total[0] += len(data)
        return f'<img src="{_data_uri(name, data)}" alt="">'

    def runs(p):
        out = []
        for el in p:
            if el.tag == W + "r":
                out.append(run(el))
            elif el.tag == W + "hyperlink":
                inner = "".join(run(r) for r in el.iter(W + "r"))
                t, mode = rels.get(el.get(R + "id")) or (None, None)
                out.append(f'<a href="{htmllib.escape(t)}" target="_blank" rel="noopener">{inner}</a>' if t and mode == "External" and re.match(r"(https?:|mailto:)", t, re.I) else inner)
            elif el.tag in (W + "ins", W + "smartTag", W + "sdt", W + "fldSimple"):
                out.append("".join(run(r) for r in el.iter(W + "r")))
        return "".join(out)

    def run(r):
        parts = []
        for el in r.iter():
            if el.tag == W + "t":
                parts.append(htmllib.escape(el.text or ""))
            elif el.tag == W + "tab":
                parts.append("&emsp;")
            elif el.tag in (W + "br", W + "cr"):
                parts.append("<br>")
            elif el.tag == A + "blip":
                parts.append(image(el.get(R + "embed")))
            elif el.tag == V + "imagedata":
                parts.append(image(el.get(R + "id")))
        s = "".join(parts)
        pr = r.find(W + "rPr")
        if pr is not None and s:
            on = lambda tag: pr.find(W + tag) is not None and pr.find(W + tag).get(W + "val") not in ("0", "false", "none")
            if on("b"):
                s = f"<b>{s}</b>"
            if on("i"):
                s = f"<i>{s}</i>"
            if on("u"):
                s = f"<u>{s}</u>"
            if on("strike"):
                s = f"<s>{s}</s>"
        return s

    def para(p):
        """返回 (类型, html)：类型 h1-h6 / li / p。"""
        pr = p.find(W + "pPr")
        style, numbered = "", False
        if pr is not None:
            ps = pr.find(W + "pStyle")
            if ps is not None:
                style = styles.get(ps.get(W + "val"), ps.get(W + "val") or "").lower()
            numbered = pr.find(W + "numPr") is not None
        body = runs(p)
        m = re.match(r"(?:heading|标题)\s*(\d)", style)
        if style == "title":
            return "h1", body
        if m:
            return f"h{min(6, max(1, int(m.group(1))))}", body
        if numbered or "list" in style:
            return "li", body
        return "p", body

    def block(children):
        out, items = [], []

        def flush():
            if items:
                out.append("<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>")
                items.clear()

        for el in children:
            if el.tag == W + "p":
                t, body = para(el)
                if t == "li":
                    items.append(body)
                    continue
                flush()
                out.append(f"<{t}>{body or '<br>'}</{t}>" if t != "p" or body else "<p><br></p>")
            elif el.tag == W + "tbl":
                flush()
                rows = []
                for tr in el.findall(W + "tr"):
                    cells = []
                    for tc in tr.findall(W + "tc"):
                        span = tc.find(f"{W}tcPr/{W}gridSpan")
                        cs = f' colspan="{span.get(W + "val")}"' if span is not None else ""
                        cells.append(f"<td{cs}>{block(list(tc))}</td>")
                    rows.append("<tr>" + "".join(cells) + "</tr>")
                out.append("<table>" + "".join(rows) + "</table>")
            elif el.tag == W + "sdt":
                c = el.find(W + "sdtContent")
                if c is not None:
                    flush()
                    out.append(block(list(c)))
        flush()
        return "".join(out)

    body = doc.find(W + "body")
    return block(list(body) if body is not None else [])
