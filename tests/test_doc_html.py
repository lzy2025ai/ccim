"""HTML 预览（#248）：内嵌本地图片、样式表时不能改动 <script> 里的代码。"""
import os
import tempfile
import unittest

from ccim import docs


class InlineHtmlTests(unittest.TestCase):
    def test_script_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "a.png"), "wb").write(b"x")
            open(os.path.join(d, "j.js"), "w").write("1")
            code = """class B{static getDataURL(t){return t.toDataURL("x")}}; var s=' src="a.png"';"""
            html = f"""<style>.x{{background:url(a.png)}} .y{{background:url(no.png)}}</style>
<img src="a.png"><script src="j.js"></script><script>{code}</script>"""
            p = os.path.join(d, "t.html")
            open(p, "w").write(html)
            out = docs.view(p)["html"]
            self.assertIn(f"<script>{code}</script>", out)
            self.assertIn('<script src="data:', out)
            self.assertIn('<img src="data:', out)
            self.assertIn('url("data:', out)
            self.assertIn("url(no.png)", out)


if __name__ == "__main__":
    unittest.main()
