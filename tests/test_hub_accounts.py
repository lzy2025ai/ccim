"""服务器总控的账号（#154）：迁移老账号、账号名 + 密码、动态码可选、停用和到期，按电脑开权限，电脑按账号隔开；不读真实登记表。"""
import http.client
import json
import os
import secrets
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import unquote

from ccim import hubserver, peers, registry


def code(secret, step=0):
    return hubserver.totp_at(secret, int(time.time()) // 30 + step)


class HubAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch.object(registry, "HOME", self.tmp.name),
                  patch.object(registry, "REG", os.path.join(self.tmp.name, "registry.json"))):
            p.start()
            self.addCleanup(p.stop)
        self.hub = hubserver.Hub("https://hub.example")
        hubserver._save("settings.json", {"accounts": True})

    def owner(self):
        t = self.hub.setup_token()
        d = self.hub.setup_begin(t)
        self.hub.setup_finish(t, "owner-password", code(d["secret"]))
        return d["secret"]

    def member(self, name, **f):
        r = self.hub.account_save(None, dict({"name": name}, **f))
        return r["id"], r["password"]

    def add_node(self, mid, account=None):
        self.hub.peers.nodes[mid] = dict({"name": mid, "key": "k"}, **({"account": account} if account else {}))

    def serve(self, aid):
        tok = self.hub.new_session(aid)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), hubserver._handler(self.hub))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

        def req(method, path, machine=None, body=None, extra=None):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            h = dict({"X-CCIM": "1", "Cookie": f"ccim_hub={tok}", "Host": "127.0.0.1"}, **(extra or {}))
            if machine:
                h["X-Machine"] = machine
            c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
            r = c.getresponse()
            out = r.status, json.loads(r.read() or b"{}")
            c.close()
            return out
        return req

    def test_old_single_account_migrates_to_owner(self):
        salt, h = hubserver.hash_password("old-password")
        hubserver._save("auth.json", {"password": {"salt": salt, "hash": h}, "totp": "JBSWY3DPEHPK3PXP", "totp_last": 0})
        hubserver._save("sessions.json", {"x" * 64: time.time() + 100})
        self.assertEqual(self.hub.accounts()[hubserver.OWNER]["name"], "admin")
        self.assertIsNone(self.hub.setup_token())
        self.assertEqual(self.hub.session_account("unused"), None)
        with self.assertRaises(hubserver.NeedCode):            # 主人要动态码
            self.hub.login("1.1.1.1", "", "old-password", "")
        tok = self.hub.login("1.1.1.1", "", "old-password", code("JBSWY3DPEHPK3PXP"))   # 账号名留空也算主人
        self.assertEqual(self.hub.session_account(tok), hubserver.OWNER)

    def test_member_name_and_generated_password(self):
        osec = self.owner()
        aid, pw = self.member("家人")
        self.assertRegex(pw, r"^[A-Za-z2-9]{4}-[A-Za-z2-9]{4}-[A-Za-z2-9]{4}$")
        self.assertEqual(self.hub.session_account(self.hub.login("1.1.1.1", "家人", pw, "")), aid)
        self.assertEqual(self.hub.session_account(self.hub.login("1.1.1.2", "admin", "owner-password", code(osec, 1))),
                         hubserver.OWNER)
        with self.assertRaises(PermissionError):
            self.hub.login("1.1.1.3", "家人", "wrong-password", "")
        with self.assertRaises(ValueError):
            self.member("家人")
        _, pw2 = self.member("朋友", password="my-own-pass")
        self.assertEqual(pw2, "my-own-pass")
        with self.assertRaises(ValueError):
            self.member("太短", password="short")

    def test_disable_expire_and_reset(self):
        self.owner()
        aid, pw = self.member("朋友")
        tok = self.hub.login("1.1.1.1", "朋友", pw, "")
        self.hub.account_save(aid, {"disabled": True})
        self.assertIsNone(self.hub.session_account(tok))
        with self.assertRaisesRegex(PermissionError, "停用或到期"):
            self.hub.login("1.1.1.1", "朋友", pw, "")
        self.hub.account_save(aid, {"disabled": False, "expires": time.time() - 10})
        with self.assertRaises(PermissionError):
            self.hub.login("1.1.1.1", "朋友", pw, "")
        self.assertEqual(self.hub.account_list()["accounts"][0]["state"], "expired")
        self.hub.account_save(aid, {"expires": 0})
        tok = self.hub.login("1.1.1.1", "朋友", pw, "")
        r = self.hub.account_save(aid, {"password": "brand-new-pw"})   # 改了密码：已登录的要重新登录
        self.assertEqual(r["password"], "brand-new-pw")
        self.assertIsNone(self.hub.session_account(tok))

    def test_member_totp_optional(self):
        self.owner()
        aid, pw = self.member("家人")
        r = self.hub.account_save(aid, {"totp": True})
        token = r["bind"]["link"].split("token=")[1]
        d = self.hub.setup_begin(token)
        self.assertTrue(d["bind"])
        self.hub.setup_finish(token, "", code(d["secret"]))       # 只绑验证器，不改密码
        with self.assertRaises(hubserver.NeedCode):
            self.hub.login("1.1.1.1", "家人", pw, "")
        self.hub.login("1.1.1.1", "家人", pw, code(d["secret"], 1))
        self.hub.account_save(aid, {"totp": False})
        self.hub.login("1.1.1.1", "家人", pw, "")

    def test_machines_split_by_account(self):
        self.owner()
        aid, _ = self.member("家人")
        self.add_node("m-own")
        self.add_node("m-fam", aid)
        p = self.hub.peers
        self.assertEqual({m["id"] for m in p.machines(hubserver.OWNER)}, {"m-own"})
        self.assertEqual({m["id"] for m in p.machines(aid)}, {"m-fam"})
        self.assertFalse(p.same_account("m-own", "m-fam"))
        self.assertTrue(self.hub.visible(aid, {"_m": "m-fam"}))
        self.assertFalse(self.hub.visible(aid, {"_m": "m-own"}))
        self.assertFalse(self.hub.visible(hubserver.OWNER, {"_m": "m-fam"}))

    def test_pair_code_belongs_to_account(self):
        self.owner()
        aid, _ = self.member("家人")
        p = self.hub.peers
        with patch.object(p, "serve"):
            c = p.new_code(aid)["code"]
            p.pair({"code": c, "id": "m-new", "name": "新电脑"})
            self.assertEqual(p.owner_of("m-new"), aid)
            c2 = p.new_code(hubserver.OWNER)["code"]
            with self.assertRaises(PermissionError):           # 别的账号不能把它抢过去
                p.pair({"code": c2, "id": "m-new", "name": "新电脑"})
        self.hub.account_delete(aid)
        self.assertNotIn("m-new", p.nodes)

    def test_http_forward_only_to_own_machines(self):
        self.owner()
        aid, _ = self.member("家人")
        self.add_node("m-own")
        self.add_node("m-fam", aid)
        req = self.serve(aid)
        self.assertEqual(req("GET", "/api/chat?id=x", "m-own")[0], 404)       # 别人的电脑：没有这台
        self.assertEqual(req("GET", "/api/chat?id=x", "m-fam")[0], 503)       # 自己的：只是离线
        st = req("GET", "/api/state")[1]
        self.assertEqual([m["id"] for m in st["machines"]], ["m-fam"])
        self.assertFalse(st["account"]["owner"])
        self.assertEqual(req("GET", "/api/accounts")[0], 403)
        self.assertEqual(req("POST", "/api/accounts/save", body={"fields": {"name": "x"}})[0], 403)
        self.assertEqual(req("POST", "/api/peers/remove", body={"id": "m-own"})[0], 400)
        self.assertIn("m-own", self.hub.peers.nodes)

    def test_share_machine(self):
        self.owner()
        aid, _ = self.member("家人")
        self.add_node("m-own")
        self.add_node("m-other")
        p = self.hub.peers
        with self.assertRaises(ValueError):                  # 只能共享主人自己的电脑
            self.hub.account_save(aid, {"shares": {"m-nope": {"own": True}}})
        self.hub.account_save(aid, {"shares": {"m-own": {"agents": ["ag1"], "own": False, "codex": True, "dir": "~/x"}}})
        ms = p.machines(aid)
        self.assertEqual([(m["id"], m.get("shared")) for m in ms], [("m-own", True)])
        self.assertTrue(p.guest_of("m-own", aid))
        self.assertFalse(p.guest_of("m-own", hubserver.OWNER))
        self.assertEqual(p.machines(aid, own=True), [])
        self.assertEqual(self.hub.account_list()["accounts"][0]["shares"],
                         {"m-own": {"agents": ["ag1"], "own": False, "codex": True, "dir": "~/x"}})
        h = p.guest_headers(aid, "m-own")
        self.assertEqual(h["X-Account"], aid)
        self.assertEqual(json.loads(unquote(h["X-Account-Share"]))["agents"], ["ag1"])
        # 推送：访客只收自己的，主人收不到访客的
        self.assertTrue(self.hub.visible(aid, {"_m": "m-own", "_a": aid, "chat": "w-1"}))
        self.assertFalse(self.hub.visible(aid, {"_m": "m-own", "chat": "w-2"}))
        self.assertFalse(self.hub.visible(aid, {"_m": "m-own", "type": "schedule", "schedule": "s1"}))
        self.assertTrue(self.hub.visible(aid, {"_m": "m-own", "type": "skills", "chat": None}))
        self.assertFalse(self.hub.visible(hubserver.OWNER, {"_m": "m-own", "_a": aid, "chat": "w-1"}))
        self.assertTrue(self.hub.visible(hubserver.OWNER, {"_m": "m-own", "chat": "w-2"}))
        self.assertFalse(self.hub.visible(aid, {"_m": "m-other", "_a": aid}))
        # 转交时带上访客身份；那台电脑还是旧版本（不认访客）时不转
        seen, old = [], [True]

        def fwd(mid, method, path, body=b"", headers=None):
            seen.append(headers)
            st = {"agents": [{"id": "x"}]} if old[0] else {"agents": [], "guest": {}}
            return 200, "application/json", json.dumps(st if path.startswith("/api/state") else {}).encode()
        p.forward = fwd
        req = self.serve(aid)
        self.assertEqual(req("GET", "/api/chat?id=w-1", "m-own", extra={"X-Account": "owner"})[0], 409)
        p.online = lambda mid: True
        self.assertEqual(self.hub.state(aid)["agents"], [])  # 旧版本给的整份 state 也不显示
        old[0] = False
        p.guest_ok.clear()
        self.assertEqual(req("GET", "/api/chat?id=w-1", "m-own", extra={"X-Account": "owner"})[0], 200)
        self.assertEqual(seen[-1]["X-Account"], aid)
        self.hub.account_save(aid, {"shares": {"m-own": None}})
        self.assertEqual(p.machines(aid), [])
        self.hub.account_save(aid, {"shares": {"m-own": {"own": True}}})
        self.hub.account_delete(aid)
        self.assertNotIn("shares", p.nodes["m-own"])

    def test_owner_request_cannot_claim_account(self):
        """浏览器自己带的 X-Account 不往下传。"""
        self.owner()
        self.add_node("m-own")
        seen = []
        self.hub.peers.forward = lambda mid, method, path, body=b"", headers=None: (seen.append(headers), (200, "application/json", b"{}"))[1]
        req = self.serve(hubserver.OWNER)
        req("GET", "/api/chat?id=w-1", "m-own", extra={"X-Account": "a1", "X-Account-Share": "{}"})
        self.assertNotIn("X-Account", seen[-1])
        self.assertNotIn("X-Account-Share", seen[-1])


class AccountsOffTests(HubAccountTests):
    """多账号关着（默认）：和以前一样只有主人；账号管理、共享都没有。"""

    def test_off(self):
        osec = self.owner()
        aid, pw = self.member("家人")
        self.add_node("m-own")
        self.hub.account_save(aid, {"shares": {"m-own": {"own": True}}})
        member_tok = self.hub.login("1.1.1.1", "家人", pw, "")
        hubserver._save("settings.json", {})
        self.assertFalse(self.hub.accounts_on())
        self.assertIsNone(self.hub.session_account(member_tok))       # 已经登录的也不算了
        with self.assertRaises(PermissionError):
            self.hub.login("1.1.1.2", "家人", pw, "")
        tok = self.hub.login("1.1.1.3", "", "owner-password", code(osec, 1))
        self.assertEqual(self.hub.session_account(tok), hubserver.OWNER)
        self.assertFalse(self.hub.peers.guest_of("m-own", aid))
        self.assertNotIn("shares", self.hub.peers.machines(hubserver.OWNER)[0])
        req = self.serve(hubserver.OWNER)
        self.assertEqual(req("GET", "/api/accounts")[0], 403)
        self.assertEqual(req("POST", "/api/accounts/save", body={"fields": {"name": "x"}})[0], 403)
        self.hub.peers.online = lambda mid: False
        self.assertFalse(self.hub.state(hubserver.OWNER)["account"]["multi"])
        srv = ThreadingHTTPServer(("127.0.0.1", 0), hubserver._handler(self.hub))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        c.request("GET", "/", headers={"Host": "127.0.0.1"})
        page = c.getresponse().read().decode()
        c.close()
        self.assertIn('<div id="nmbox" hidden>', page)


class PeerCallTests(unittest.TestCase):
    """电脑之间经服务器调用：不同账号的电脑互相调不到。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch.object(registry, "HOME", self.tmp.name),
                  patch.object(registry, "REG", os.path.join(self.tmp.name, "registry.json"))):
            p.start()
            self.addCleanup(p.stop)

    def test_call_across_accounts_refused(self):
        pf = type("PF", (), {})()
        from ccim.platform import Events
        pf.channel = type("C", (), {"events": Events()})()
        p = peers.Peers(pf, server=True)
        p.nodes = {"a": {"name": "a", "key": "ka"}, "b": {"name": "b", "key": "kb", "account": "a1"},
                   "c": {"name": "c", "key": "kc"}}
        srv = ThreadingHTTPServer(("127.0.0.1", 0), peers._handler(p))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        called = []
        p.forward = lambda *a, **k: (called.append(a[0]), (200, "application/json", b"{}"))[1]

        def call(src, key, target):
            ts, nonce, body = str(int(time.time())), secrets.token_hex(8), b""
            sig = peers._sign(key, "POST", "/peer/call", ts, nonce, peers._digest(body))
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            c.request("POST", "/peer/call", body=body, headers={"X-Node": src, "X-Ts": ts, "X-Nonce": nonce, "X-Sig": sig,
                                                               "X-Target": target, "X-Method": "GET", "X-Path": "/api/state"})
            r = c.getresponse()
            r.read()
            c.close()
            return r.status

        self.assertEqual(call("a", "ka", "b"), 503)
        self.assertEqual(call("b", "kb", "a"), 503)
        self.assertEqual(call("a", "ka", "c"), 200)
        self.assertEqual(called, ["c"])
        self.assertEqual({m["id"] for m in p.machines(p.owner_of("b"))}, {"b"})


if __name__ == "__main__":
    unittest.main()
