"""Tests run against a fake switch web UI on localhost; nothing here talks to a real switch.

Plain unittest, so they run with ``python -m unittest`` or ``pytest``.
"""

from __future__ import annotations

import http.client
import json
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from easysmart_mcp import switch as sw
from easysmart_mcp.config import Config, parse_labels
from easysmart_mcp.server import McpApp, make_handler
from easysmart_mcp.switch import HttpSession, Switch, parse_vars
from easysmart_mcp.tools import Context

TOKEN = "t" * 64
PASSWORD = "pw_123"

LOGIN_PAGE = """<script>
var logonInfo = new Array(
%d,
0,0);
var g_Lan = 393;
</script><form id="form0" method="POST" action="/logon.cgi"></form>"""

PAGES = {
    "SystemInfoRpm.htm": """<script>
var info_ds = {
descriStr:[
"TL-SG108E"
],
macStr:[
"02:00:00:00:00:01"
],
ipStr:[
"192.0.2.4"
],
netmaskStr:[
"255.255.255.0"
],
gatewayStr:[
"192.0.2.1"
],
firmwareStr:[
"1.0.0 Build 20200101 Rel.00001"
],
hardwareStr:[
"TL-SG108E 6.0"
]
};
var tip = "";
</script>""",
    "IpSettingRpm.htm": "<script>var ip_ds = {state:1, ipStr:['192.0.2.4'], netmaskStr:['255.255.255.0']};</script>",
    "LoopPreventionRpm.htm": "<script>var lpEn = 0;</script>",
    "PortSettingRpm.htm": """<script>
var max_port_num = 5;
var port_middle_num  = 16;
var all_info = {
state:[1,1,1,1,0,0,0],
trunk_info:[0,0,0,0,0,0,0],
spd_cfg:[1,1,1,5,1,0,0],
spd_act:[6,4,0,5,0,0,0],
fc_cfg:[0,0,0,0,0,0,0],
fc_act:[0,0,0,0,0,0,0]
};
// a comment with var fake = {
</script>""",
    "PortStatisticsRpm.htm": """<script>
var max_port_num = 5;
var all_info = {
state:[1,1,1,1,0,0,0],
link_status:[6,4,0,5,0,0,0],
pkts:[100,0,200,0, 10,0,20,3, 0,0,0,0, 5,0,6,0, 0,0,0,0, 0,0]
};
</script>""",
    "Vlan8021QRpm.htm": """<script>
var qvlan_ds = {
state:1,
count:2,
vids:[1,20],
names:['Default','iot'],
tagMbrs:[0,1],
untagMbrs:[0x1D,0x2]
};
</script>""",
    "Vlan8021QPvidRpm.htm": "<script>var pvid_ds = {state:1, portNum:5, pvids:[1,1,1,1,1]};</script>",
    "VlanPortBasicRpm.htm": "<script>var pvlan_ds = {state:0, count:1, vids:[1], mbrs:[31]};</script>",
    "PortMirrorRpm.htm": "<script>var MirrEn = 0; var MirrPort = 0; var mirr_info = {ingress:[0,0,0,0,0], egress:[0,0,0,0,0]};</script>",
}


class FakeSwitch:
    def __init__(self) -> None:
        self.paths: list[str] = []
        self.methods: list[str] = []
        self.logins = 0
        self.logouts = 0
        self.logged_in = False
        self.redirect_login = False


def make_fake(state: FakeSwitch) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def _reply(self, body: str, code: int = 200, headers: dict | None = None) -> None:
            data = body.encode()
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:  # noqa: N802
            state.paths.append(self.path)
            state.methods.append("POST")
            length = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode())
            if self.path != "/logon.cgi":
                return self._reply("no", 404)
            state.logins += 1
            if form.get("password") != [PASSWORD]:
                return self._reply(LOGIN_PAGE % 1)
            state.logged_in = True
            cookie = {"Set-Cookie": "H_P_SSID=abc; path=/"}
            if state.redirect_login:
                return self._reply("", 302, {**cookie, "Location": "/"})
            return self._reply(LOGIN_PAGE % 0, headers=cookie)

        def do_GET(self) -> None:  # noqa: N802
            state.paths.append(self.path)
            state.methods.append("GET")
            name = self.path.lstrip("/")
            if name == "Logout.htm":
                state.logouts += 1
                state.logged_in = False
                return self._reply("bye")
            if not state.logged_in or "H_P_SSID=abc" not in (self.headers.get("Cookie") or ""):
                return self._reply(LOGIN_PAGE % 0)
            if name in PAGES:
                return self._reply(PAGES[name])
            return self._reply("not found", 404)

    return Handler


def cfg(port: int = 0, password: str = PASSWORD, labels: str = "1=uplink,3=nas") -> Config:
    return Config(host="127.0.0.1", username="admin", password=password, token=TOKEN, bind="127.0.0.1",
                  port=port, allow_nets=(__import__("ipaddress").ip_network("127.0.0.0/8"),),
                  labels=parse_labels(labels))


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeSwitch()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_fake(self.fake))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        port = self.srv.server_address[1]
        sw.LOGIN_BACKOFF = 60.0
        self.factory = lambda host, timeout: HttpSession(f"127.0.0.1:{port}", timeout)

    def tearDown(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()

    def app(self, **kw: object) -> McpApp:
        c = cfg(**kw)  # type: ignore[arg-type]
        return McpApp(c, Context(c, Switch(c.host, c.username, c.password, 5, http_factory=self.factory)))

    def call(self, app: McpApp, name: str, args: dict | None = None) -> tuple[object, bool]:
        r = app.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": name, "arguments": args or {}}})
        res = r["result"]
        text = res["content"][0]["text"]
        return (text if res["isError"] else json.loads(text)), res["isError"]


class ParserTests(unittest.TestCase):
    def test_literals(self) -> None:
        v = parse_vars("""var a = {x:[1,-2,0x1F,1.5], 'y': "q\\"s", z:new Array(1,2,), w:true, n:null};
                          var b = 'text'; var c = function(){}; var d = 7;""")
        self.assertEqual(v["a"], {"x": [1, -2, 31, 1.5], "y": 'q"s', "z": [1, 2], "w": True, "n": None})
        self.assertEqual(v["b"], "text")
        self.assertEqual(v["d"], 7)
        self.assertNotIn("c", v)  # code, not data: never evaluated or returned

    def test_only_top_level_vars(self) -> None:
        page = """<style>a{color:red}</style><script>var data = {x:1};
            function f() { for (var i = 0; i < 3; i++) { var t = "inner"; } }
            var after = [2]; var ref = data;</script>"""
        self.assertEqual(parse_vars(page), {"data": {"x": 1}, "after": [2]})

    def test_garbage_is_skipped(self) -> None:
        self.assertEqual(parse_vars("var a = [1,2; var b = 3;"), {"b": 3})

    def test_depth_limit(self) -> None:
        self.assertEqual(parse_vars("var a = " + "[" * 100 + "]" * 100 + ";"), {})

    def test_unclosed_script_tags_are_fast(self) -> None:
        import time

        page = "<script>" * 60000  # about 480 KB, under the page limit
        start = time.monotonic()
        parse_vars(page)
        self.assertLess(time.monotonic() - start, 2.0)

    def test_secrets_not_in_repr(self) -> None:
        text = repr(cfg(password="pw_secret_value"))
        self.assertNotIn("pw_secret_value", text)
        self.assertNotIn(TOKEN, text)

    def test_labels(self) -> None:
        self.assertEqual(parse_labels("1=uplink, 2 = nas box ,"), {1: "uplink", 2: "nas box"})
        with self.assertRaises(SystemExit):
            parse_labels("uplink")


class ToolTests(Base):
    def test_info(self) -> None:
        out, err = self.call(self.app(), "switch_info")
        self.assertFalse(err)
        self.assertEqual(out["model"], "TL-SG108E")
        self.assertEqual(out["firmware"], "1.0.0 Build 20200101 Rel.00001")
        self.assertEqual(out["ports"], 5)
        self.assertTrue(out["dhcp"])
        self.assertFalse(out["loop_prevention"])
        self.assertEqual((self.fake.logins, self.fake.logouts), (1, 1))

    def test_ports(self) -> None:
        out, _ = self.call(self.app(), "switch_ports")
        rows = out["ports"]
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["label"], "uplink")
        self.assertEqual(rows[0]["link"], "1000M full")
        self.assertEqual(rows[1]["link"], "100M half")
        self.assertEqual(rows[1]["rx_bad"], 3)
        self.assertFalse(rows[4]["enabled"])
        one, _ = self.call(self.app(), "switch_ports", {"port": 2})
        self.assertEqual([r["port"] for r in one["ports"]], [2])
        msg, err = self.call(self.app(), "switch_ports", {"port": 9})
        self.assertTrue(err)
        self.assertIn("1 to 5", msg)

    def test_vlans(self) -> None:
        out, _ = self.call(self.app(), "switch_vlans")
        v = out["dot1q"]["vlans"]
        self.assertEqual(v[0], {"vid": 1, "name": "Default", "tagged": [], "untagged": [1, 3, 4, 5]})
        self.assertEqual(v[1], {"vid": 20, "name": "iot", "tagged": [1], "untagged": [2]})
        self.assertFalse(out["port_based"]["enabled"])

    def test_config_lists_unavailable(self) -> None:
        out, _ = self.call(self.app(), "switch_config", {"sections": ["mirror", "igmp"]})
        self.assertEqual(out["sections"]["mirror"]["MirrEn"], 0)
        self.assertEqual(out["unavailable"], ["igmp"])
        msg, err = self.call(self.app(), "switch_config", {"sections": ["../logon.cgi"]})
        self.assertTrue(err)

    def test_audit(self) -> None:
        out, _ = self.call(self.app(), "switch_audit")
        issues = {(p.get("port"), p["issue"].split(":")[0]) for p in out["problems"]}
        self.assertIn((2, "bad packets"), issues)
        self.assertIn((2, "link is 100M half"), issues)
        self.assertIn((3, "labelled port has no link"), issues)
        self.assertIn((4, "speed is fixed at 100M full, not auto"), issues)
        self.assertIn((2, "PVID 1 is not a VLAN this port is untagged in"), issues)
        self.assertIn((None, "loop prevention is off"), issues)
        self.assertFalse(out["ok"])

    def test_audit_shared_bad_counts(self) -> None:
        from easysmart_mcp.tools import _shared_bad

        rows = [{"port": p, "enabled": True, "rx_bad": n} for p, n in
                [(1, 1000), (2, 980), (3, 1000), (4, 50), (5, 1050), (6, 0)]]
        self.assertEqual(_shared_bad(rows), {"ports": [1, 2, 3, 5], "about": "980-1050"})
        self.assertIsNone(_shared_bad(rows[:2] + rows[3:4]))
        same = [{"port": p, "enabled": True, "rx_bad": 49} for p in (1, 2, 3)]
        self.assertEqual(_shared_bad(same), {"ports": [1, 2, 3], "about": "49"})

    def test_only_get_on_allowlisted_pages(self) -> None:
        app = self.app()
        for name in app.tools:
            self.call(app, name)
        for method, path in zip(self.fake.methods, self.fake.paths):
            name = path.lstrip("/")
            if method == "POST":
                self.assertEqual(name, "logon.cgi")
            else:
                self.assertTrue(name == "Logout.htm" or name in sw.PAGES.values(), name)
                self.assertFalse(name.endswith(".cgi"))

    def test_ignores_proxy_settings(self) -> None:
        import os
        from unittest import mock

        bogus = "http://192.0.2.1:9"  # would swallow the clear-text login if it were used
        with mock.patch.dict(os.environ, {"http_proxy": bogus, "HTTP_PROXY": bogus, "no_proxy": ""}):
            out, err = self.call(self.app(), "switch_info")
        self.assertFalse(err, out)

    def test_huge_counts_from_the_switch_are_bounded(self) -> None:
        saved = PAGES["Vlan8021QRpm.htm"]
        PAGES["Vlan8021QRpm.htm"] = ("<script>var qvlan_ds = {state:1, count:100000000, vids:[1], "
                                     "names:['a'], tagMbrs:[0], untagMbrs:[1]};</script>")
        try:
            out, err = self.call(self.app(), "switch_vlans")
        finally:
            PAGES["Vlan8021QRpm.htm"] = saved
        self.assertFalse(err, out)
        self.assertEqual(len(out["dot1q"]["vlans"]), 1)

    def test_odd_login_answer(self) -> None:
        sw.LOGIN_ERRORS  # the code path below must not raise TypeError on a list
        s = Switch("127.0.0.1", "admin", PASSWORD, 1,
                   http_factory=lambda h, t: type("H", (), {
                       "request": lambda self, path, data=None:
                           "<script>var logonInfo = new Array([1],0);</script>"})())
        with self.assertRaises(sw.LoginError):
            s.call(lambda sess: None)

    def test_session_refuses_unknown_pages(self) -> None:
        from easysmart_mcp.switch import Session, SwitchError

        with self.assertRaises(SwitchError):
            Session(HttpSession("127.0.0.1:1", 1)).page("../logon.cgi")
        msg, err = self.call(self.app(), "switch_config", {"sections": ["ports_set.cgi"]})
        self.assertTrue(err)
        self.assertIn("unknown sections", msg)
        self.assertNotIn("/ports_set.cgi", self.fake.paths)

    def test_redirect_after_login(self) -> None:
        self.fake.redirect_login = True
        out, err = self.call(self.app(), "switch_info")
        self.assertFalse(err, out)


class LoginTests(Base):
    def test_wrong_password_backs_off_and_is_redacted(self) -> None:
        app = self.app(password="wrong_pw")
        msg, err = self.call(app, "switch_info")
        self.assertTrue(err)
        self.assertIn("password is wrong", msg)
        msg, err = self.call(app, "switch_info")
        self.assertIn("waiting a minute", msg)
        self.assertEqual(self.fake.logins, 1)
        self.assertNotIn("wrong_pw", msg)

    def test_password_is_redacted_from_errors(self) -> None:
        app = self.app()
        app.tools["switch_info"] = app.tools["switch_info"].__class__(
            "switch_info", "", {}, False,
            lambda ctx, args: (_ for _ in ()).throw(RuntimeError(f"boom {PASSWORD} boom")))
        msg, err = self.call(app, "switch_info")
        self.assertTrue(err)
        self.assertNotIn(PASSWORD, msg)
        self.assertIn("<redacted>", msg)

    def test_dropped_session(self) -> None:
        app = self.app()
        orig = app.ctx.switch._login

        def login_then_drop(http: object) -> None:
            orig(http)
            self.fake.logged_in = False

        app.ctx.switch._login = login_then_drop  # type: ignore[method-assign]
        msg, err = self.call(app, "switch_info")
        self.assertTrue(err)
        self.assertIn("dropped the session", msg)
        self.assertEqual(self.fake.logouts, 1)

    def test_unreachable(self) -> None:
        import socket

        with socket.socket() as s:  # a port nothing listens on
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        c = cfg()
        dead = Switch(c.host, c.username, c.password, 1,
                      http_factory=lambda h, t: HttpSession(f"127.0.0.1:{port}", t))
        msg, err = self.call(McpApp(c, Context(c, dead)), "switch_info")
        self.assertTrue(err)
        self.assertIn("cannot reach", msg)


class HttpTests(Base):
    def test_mcp_over_http(self) -> None:
        app = self.app()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]

        def post(body: object, headers: dict) -> tuple[int, object]:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/mcp", body=json.dumps(body), headers={"Content-Type": "application/json", **headers})
            r = conn.getresponse()
            data = r.read()
            return r.status, (json.loads(data) if data else None)

        def post_raw(data: bytes, headers: dict) -> int:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/mcp", body=data, headers={"Content-Type": "application/json", **headers})
            r = conn.getresponse()
            r.read()
            return r.status

        try:
            auth = {"Authorization": f"Bearer {TOKEN}"}
            self.assertEqual(post({}, {})[0], 401)
            self.assertEqual(post({}, {"Authorization": "Bearer " + "x" * 64})[0], 401)
            self.assertEqual(post({}, {"Authorization": TOKEN})[0], 401)
            self.assertEqual(post({}, {**auth, "Origin": "http://evil"})[0], 403)
            code, body = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-06-18"}}, auth)
            self.assertEqual(code, 200)
            self.assertEqual(body["result"]["serverInfo"]["name"], "easysmart-mcp")
            code, body = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, auth)
            tools = body["result"]["tools"]
            self.assertEqual({t["name"] for t in tools},
                             {"switch_info", "switch_ports", "switch_vlans", "switch_config", "switch_audit"})
            self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in tools))
            self.assertEqual(post({"jsonrpc": "2.0", "method": "notifications/initialized"}, auth)[0], 202)
            code, body = post([{"jsonrpc": "2.0", "id": 3, "method": "ping"}], auth)
            self.assertEqual(code, 400)
            self.assertEqual(post_raw(b"[" * 60000, auth), 400)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
