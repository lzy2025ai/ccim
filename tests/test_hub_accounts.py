"""服务器总控的账号（#154）：迁移老账号、邀请、按密码认账号、停用，电脑按账号隔开；不读真实登记表。"""
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

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

    def setup_account(self, token, pw):
        d = self.hub.setup_begin(token)
        return self.hub.setup_finish(token, pw, code(d["secret"])), d["secret"]

    def owner(self):
        tok, secret = self.setup_account(self.hub.setup_token(), "owner-password")
        return secret

    def invite(self, name):
        r = self.hub.account_invite(name)
        token = r["link"].split("token=")[1]
        _, secret = self.setup_account(token, f"{name}-password")
        return r["id"], secret

    def add_node(self, mid, account=None):
        self.hub.peers.nodes[mid] = dict({"name": mid, "key": "k"}, **({"account": account} if account else {}))

    def test_old_single_account_migrates_to_owner(self):
        salt, h = hubserver.hash_password("old-password")
        hubserver._save("auth.json", {"password": {"salt": salt, "hash": h}, "totp": "JBSWY3DPEHPK3PXP", "totp_last": 0})
        hubserver._save("sessions.json", {"x" * 64: time.time() + 100})
        self.assertIn(hubserver.OWNER, self.hub.accounts())
        self.assertIsNone(self.hub.setup_token())
        tok = self.hub.login("1.1.1.1", "old-password", code("JBSWY3DPEHPK3PXP"))
        self.assertEqual(self.hub.session_account(tok), hubserver.OWNER)

    def test_invite_and_login_by_password(self):
        osec = self.owner()
        aid, asec = self.invite("家人")
        t1 = self.hub.login("1.1.1.1", "家人-password", code(asec, 1))
        self.assertEqual(self.hub.session_account(t1), aid)
        t2 = self.hub.login("1.1.1.2", "owner-password", code(osec, 1))
        self.assertEqual(self.hub.session_account(t2), hubserver.OWNER)
        with self.assertRaises(PermissionError):             # 别人的动态码不行
            self.hub.login("1.1.1.3", "家人-password", code(osec))
        with self.assertRaises(ValueError):
            self.hub.account_invite("家人")

    def test_invite_link_once_and_disable(self):
        self.owner()
        r = self.hub.account_invite("朋友")
        token = r["link"].split("token=")[1]
        tok, sec = self.setup_account(token, "friend-password")
        with self.assertRaises(PermissionError):
            self.hub.setup_begin(token)
        self.hub.account_update(r["id"], {"disabled": True})
        self.assertIsNone(self.hub.session_account(tok))
        with self.assertRaises(PermissionError):
            self.hub.login("1.1.1.1", "friend-password", code(sec, 1))
        self.hub.account_update(r["id"], {"disabled": False})
        self.hub.account_update(r["id"], {"expires_days": 0})
        self.assertIsNone(self.hub.session_account(tok))     # 停用时登录已经作废
        rl = self.hub.account_update(r["id"], {"relink": True})
        self.assertIn("token=", rl["link"])
        self.assertEqual(self.hub.account_list()["accounts"][0]["state"], "invited")

    def test_machines_split_by_account(self):
        self.owner()
        aid, _ = self.invite("家人")
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
        aid, _ = self.invite("家人")
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
        aid, _ = self.invite("家人")
        self.add_node("m-own")
        self.add_node("m-fam", aid)
        tok = self.hub.new_session(aid)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), hubserver._handler(self.hub))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

        def req(method, path, machine=None, body=None):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            h = {"X-CCIM": "1", "Cookie": f"ccim_hub={tok}", "Host": "127.0.0.1"}
            if machine:
                h["X-Machine"] = machine
            c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
            r = c.getresponse()
            out = r.status, json.loads(r.read() or b"{}")
            c.close()
            return out

        self.assertEqual(req("GET", "/api/chat?id=x", "m-own")[0], 404)       # 别人的电脑：没有这台
        self.assertEqual(req("GET", "/api/chat?id=x", "m-fam")[0], 503)       # 自己的：只是离线
        st = req("GET", "/api/state")[1]
        self.assertEqual([m["id"] for m in st["machines"]], ["m-fam"])
        self.assertFalse(st["account"]["owner"])
        self.assertEqual(req("GET", "/api/accounts")[0], 403)
        self.assertEqual(req("POST", "/api/accounts/invite", body={"name": "x"})[0], 403)
        self.assertEqual(req("POST", "/api/peers/remove", body={"id": "m-own"})[0], 400)
        self.assertIn("m-own", self.hub.peers.nodes)

    def test_share_machine(self):
        self.owner()
        aid, _ = self.invite("家人")
        self.add_node("m-own")
        self.add_node("m-other")
        p = self.hub.peers
        with self.assertRaises(ValueError):                  # 只能共享主人自己的电脑
            self.hub.account_update(aid, {"share": {"machine": "m-nope", "on": True}})
        self.hub.account_update(aid, {"share": {"machine": "m-own", "on": True}})
        ms = p.machines(aid)
        self.assertEqual([(m["id"], m.get("shared")) for m in ms], [("m-own", True)])
        self.assertTrue(p.guest_of("m-own", aid))
        self.assertFalse(p.guest_of("m-own", hubserver.OWNER))
        self.assertEqual(p.machines(aid, own=True), [])
        self.assertEqual(self.hub.account_list()["accounts"][0]["shared"], ["m-own"])
        self.assertEqual(p.guest_headers(aid)["X-Account"], aid)
        # 推送：访客只收自己的，主人收不到访客的
        self.assertTrue(self.hub.visible(aid, {"_m": "m-own", "_a": aid, "chat": "w-1"}))
        self.assertFalse(self.hub.visible(aid, {"_m": "m-own", "chat": "w-2"}))
        self.assertFalse(self.hub.visible(aid, {"_m": "m-own", "type": "schedule", "schedule": "s1"}))
        self.assertTrue(self.hub.visible(aid, {"_m": "m-own", "type": "skills", "chat": None}))
        self.assertFalse(self.hub.visible(hubserver.OWNER, {"_m": "m-own", "_a": aid, "chat": "w-1"}))
        self.assertTrue(self.hub.visible(hubserver.OWNER, {"_m": "m-own", "chat": "w-2"}))
        self.assertFalse(self.hub.visible(aid, {"_m": "m-other", "_a": aid}))
        # 转交时带上访客身份
        seen = []
        p.forward = lambda mid, method, path, body=b"", headers=None: (seen.append(headers), (200, "application/json", b"{}"))[1]
        tok = self.hub.new_session(aid)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), hubserver._handler(self.hub))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        c.request("GET", "/api/chat?id=w-1", headers={"X-CCIM": "1", "Cookie": f"ccim_hub={tok}", "Host": "127.0.0.1",
                                                       "X-Machine": "m-own", "X-Account": "owner"})
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual(r.status, 200)
        self.assertEqual(seen[-1]["X-Account"], aid)
        self.hub.account_update(aid, {"share": {"machine": "m-own", "on": False}})
        self.assertEqual(p.machines(aid), [])
        self.hub.account_update(aid, {"share": {"machine": "m-own", "on": True}})
        self.hub.account_delete(aid)
        self.assertNotIn("shared", p.nodes["m-own"])

    def test_owner_request_cannot_claim_account(self):
        """浏览器自己带的 X-Account 不往下传。"""
        self.owner()
        self.add_node("m-own")
        seen = []
        self.hub.peers.forward = lambda mid, method, path, body=b"", headers=None: (seen.append(headers), (200, "application/json", b"{}"))[1]
        tok = self.hub.new_session(hubserver.OWNER)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), hubserver._handler(self.hub))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        c.request("GET", "/api/chat?id=w-1", headers={"X-CCIM": "1", "Cookie": f"ccim_hub={tok}", "Host": "127.0.0.1",
                                                       "X-Machine": "m-own", "X-Account": "a1"})
        c.getresponse().read()
        c.close()
        self.assertNotIn("X-Account", seen[-1])


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
            import secrets
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
