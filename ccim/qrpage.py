"""扫码：在终端里运行就把二维码打在终端里；不在终端里（agent 替用户跑命令、Claude 桌面端里）用户看不到输出，
就生成一个带二维码的网页在浏览器里打开。"""
import html, os, subprocess, sys

from . import registry


def page(url, heading, note, app, folder=None):
    """在浏览器里打开二维码页面。app：用哪个手机 App 扫（飞书、微信）。"""
    import qrcode
    import qrcode.image.svg
    svg = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=12, border=2).to_string(encoding="unicode")
    doc = f"""<!doctype html><meta charset="utf-8"><title>{html.escape(heading)}</title>
<style>body{{font:16px -apple-system,"PingFang SC",sans-serif;display:flex;flex-direction:column;align-items:center;
margin-top:8vh;color:#1f2329}} h1{{font-size:22px;margin:0 0 8px}} p{{color:#646a73;margin:4px}}
.qr{{width:300px;margin:24px}} .qr svg{{width:100%;height:auto}} a{{color:#3370ff;font-size:13px}}</style>
<h1>{html.escape(heading)}</h1>
<p>{html.escape(note)}</p>
<div class="qr">{svg}</div>
<p>扫完这个页面就可以关掉。</p>
<p><a href="{html.escape(url)}">扫不了的话，在手机{app}里打开这个链接</a></p>"""
    folder = folder or registry.HOME
    os.makedirs(folder, exist_ok=True)
    f = os.path.join(folder, "pair.html")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write(doc)
    subprocess.run(["open", f], capture_output=True)
    print(f"二维码已在浏览器里打开：{f}\n扫不了的话，在手机{app}里打开：{url}", flush=True)


def show(url, terminal, heading, note, app, folder=None):
    """terminal(url)：在终端里怎么显示。"""
    if sys.stdout.isatty():
        terminal(url)
    else:
        page(url, heading, note, app, folder)
