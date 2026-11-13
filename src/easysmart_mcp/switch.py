"""Talk to an Easy Smart switch's web UI, and read the data out of its pages.

The web UI keeps its data in JavaScript variables inside each page, for
example ``var info_ds = {descriStr:["TL-SG108E"], ...};``. This module fetches
an allowlisted page and parses those literals with a small parser. Nothing
from the switch is ever evaluated.

Only pages named in ``PAGES`` can be fetched, and only with GET. On the
firmware tested so far the ``.cgi`` URLs change settings even when fetched with GET, so the
client refuses any path that is not in the allowlist.
"""

from __future__ import annotations

import http.client
import http.cookiejar
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

log = logging.getLogger("easysmart_mcp.switch")

MAX_PAGE = 512 * 1024
LOGIN_BACKOFF = 60.0  # seconds to refuse new logins after a rejected password

# Read-only pages of the web UI, by section name. Adding a page here means it
# can be fetched, so only ever add pages that display settings.
PAGES: dict[str, str] = {
    "system": "SystemInfoRpm.htm",
    "ip": "IpSettingRpm.htm",
    "ports": "PortSettingRpm.htm",
    "stats": "PortStatisticsRpm.htm",
    "lag": "PortTrunkRpm.htm",
    "mirror": "PortMirrorRpm.htm",
    "loop_prevention": "LoopPreventionRpm.htm",
    "igmp": "IgmpSnoopingRpm.htm",
    "vlan_mtu": "VlanMtuRpm.htm",
    "vlan_port": "VlanPortBasicRpm.htm",
    "vlan_8021q": "Vlan8021QRpm.htm",
    "vlan_pvid": "Vlan8021QPvidRpm.htm",
    "qos": "QosBasicRpm.htm",
    "bandwidth": "QosBandWidthControlRpm.htm",
    "storm": "QosStormControlRpm.htm",
}
_ALLOWED = frozenset(PAGES.values())

LOGIN_ERRORS = {
    1: "the user name or password is wrong",
    2: "this user is not allowed to log in",
    3: "too many users are logged in",
    4: "too many users are logged in (the switch allows 16)",
    5: "the session timed out",
    6: "the switch wants its password changed in the web UI first",
}


class SwitchError(Exception):
    """The switch refused, or answered with something unexpected."""


class LoginError(SwitchError):
    pass


class NotServed(SwitchError):
    """This model doesn't serve the page (a 404, or the switch just closes the connection)."""


class Redirected(SwitchError):
    """The switch answered with a redirect (it does this instead of a login page on some firmware)."""


# ------------------------------------------------------------- JS literals


class _Word(str):
    """A bare identifier in the page's code: a reference, not a value."""


class _Parser:
    """Parses one JavaScript literal: object, array, new Array(), string, number,
    true/false/null. Object keys may be bare identifiers or quoted."""

    def __init__(self, text: str, pos: int) -> None:
        self.s = text
        self.i = pos

    def ws(self) -> None:
        s, n = self.s, len(self.s)
        while self.i < n:
            c = s[self.i]
            if c.isspace():
                self.i += 1
            elif s.startswith("//", self.i):
                end = s.find("\n", self.i)
                self.i = n if end < 0 else end + 1
            elif s.startswith("/*", self.i):
                end = s.find("*/", self.i + 2)
                self.i = n if end < 0 else end + 2
            else:
                break

    def fail(self, what: str) -> SwitchError:
        return SwitchError(f"cannot parse page data ({what} at {self.i})")

    def value(self, depth: int = 0) -> Any:
        if depth > 32:
            raise self.fail("too deep")
        self.ws()
        if self.i >= len(self.s):
            raise self.fail("end of text")
        c = self.s[self.i]
        if c == "{":
            return self.obj(depth)
        if c == "[":
            self.i += 1
            return self.items("]", depth)
        if c in "\"'":
            return self.string()
        if c == "-" or c.isdigit() or c == ".":
            return self.number()
        m = re.compile(r"new\s+Array\s*\(").match(self.s, self.i)
        if m:
            self.i = m.end()
            return self.items(")", depth)
        m = re.compile(r"[A-Za-z_$][\w$]*").match(self.s, self.i)
        if m:
            self.i = m.end()
            word = m.group()
            consts = {"true": True, "false": False, "null": None, "undefined": None}
            return consts[word] if word in consts else _Word(word)
        raise self.fail(f"unexpected {c!r}")

    def items(self, close: str, depth: int) -> list[Any]:
        out: list[Any] = []
        while True:
            self.ws()
            if self.s.startswith(close, self.i):
                self.i += 1
                return out
            out.append(self.value(depth + 1))
            self.ws()
            if self.s.startswith(",", self.i):
                self.i += 1
            elif not self.s.startswith(close, self.i):
                raise self.fail(f"expected , or {close}")

    def obj(self, depth: int) -> dict[str, Any]:
        self.i += 1
        out: dict[str, Any] = {}
        while True:
            self.ws()
            if self.s.startswith("}", self.i):
                self.i += 1
                return out
            if self.s[self.i] in "\"'":
                key = self.string()
            else:
                m = re.compile(r"[\w$]+").match(self.s, self.i)
                if not m:
                    raise self.fail("expected key")
                key = m.group()
                self.i = m.end()
            self.ws()
            if not self.s.startswith(":", self.i):
                raise self.fail("expected :")
            self.i += 1
            out[key] = self.value(depth + 1)
            self.ws()
            if self.s.startswith(",", self.i):
                self.i += 1
            elif not self.s.startswith("}", self.i):
                raise self.fail("expected , or }")

    def string(self) -> str:
        q = self.s[self.i]
        self.i += 1
        out = []
        while self.i < len(self.s):
            c = self.s[self.i]
            if c == q:
                self.i += 1
                return "".join(out)
            if c == "\\" and self.i + 1 < len(self.s):
                nxt = self.s[self.i + 1]
                out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                self.i += 2
                continue
            out.append(c)
            self.i += 1
        raise self.fail("unterminated string")

    def number(self) -> int | float:
        m = re.compile(r"-?(0[xX][0-9a-fA-F]+|\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+)").match(self.s, self.i)
        if not m:
            raise self.fail("bad number")
        self.i = m.end()
        t = m.group()
        if "x" in t.lower():
            return int(t, 16)
        if any(ch in t for ch in ".eE"):
            return float(t)
        return int(t)


_VAR = re.compile(r"\bvar\s+([A-Za-z_$][\w$]*)\s*=\s*")
MAX_SCRIPTS = 64


def _scripts(page: str) -> list[str]:
    """The text inside each <script> block, found in one linear pass (a regex
    with .*? would be quadratic on a page full of unclosed <script> tags)."""
    low = page.lower()
    out: list[str] = []
    pos = 0
    while len(out) < MAX_SCRIPTS:
        start = low.find("<script", pos)
        if start < 0:
            break
        body = low.find(">", start)
        if body < 0:
            break
        end = low.find("</script", body)
        if end < 0:
            out.append(page[body + 1:])
            break
        out.append(page[body + 1:end])
        pos = end + 8
    return out


def _top_level(script: str) -> set[int]:
    """Offsets in the script that are outside every {...} block, so ``var``s
    inside the page's own functions (loop counters and the like) are ignored."""
    top: set[int] = set()
    depth, i, n = 0, 0, len(script)
    while i < n:
        c = script[i]
        if c in "\"'":
            j = i + 1
            while j < n and script[j] != c:
                j += 2 if script[j] == "\\" else 1
            i = j + 1
            continue
        if script.startswith("//", i):
            j = script.find("\n", i)
            i = n if j < 0 else j
            continue
        if script.startswith("/*", i):
            j = script.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        elif depth == 0:
            top.add(i)
        i += 1
    return top


def parse_vars(page: str) -> dict[str, Any]:
    """Every top-level ``var name = <literal>`` in the page's scripts that parses."""
    scripts = _scripts(page) or [page]
    out: dict[str, Any] = {}
    for script in scripts:
        top = _top_level(script)
        for m in _VAR.finditer(script):
            if m.start() not in top:
                continue
            try:
                value = _Parser(script, m.end()).value()
            except SwitchError:
                continue
            if isinstance(value, _Word):
                continue  # var x = someName; is code, not data
            out[m.group(1)] = value
    return out


def is_login_page(page_vars: dict[str, Any], page: str) -> bool:
    return "logonInfo" in page_vars or 'action="/logon.cgi"' in page


# ------------------------------------------------------------------ client


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        return None


class HttpSession:
    """One login session: a cookie jar and the switch's address."""

    def __init__(self, host: str, timeout: float) -> None:
        self.base = f"http://{host}/"
        self.timeout = timeout
        self.opener = urllib.request.build_opener(
            # No proxies: the login is clear text and must go straight to the switch,
            # even if http_proxy is set in the environment.
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), _NoRedirect(),
        )

    def request(self, path: str, data: dict[str, str] | None = None) -> str:
        req = urllib.request.Request(
            self.base + path,
            data=None if data is None else urllib.parse.urlencode(data).encode(),
            headers={"Referer": self.base, "User-Agent": "easysmart-mcp"},
        )
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                body = resp.read(MAX_PAGE + 1)
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise Redirected(f"{path}: redirected") from None
            if exc.code == 404:
                raise NotServed(f"{path}: HTTP 404") from None
            raise SwitchError(f"{path}: HTTP {exc.code}") from None
        except http.client.RemoteDisconnected:
            raise NotServed(f"{path}: the switch closed the connection") from None
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, http.client.RemoteDisconnected):
                raise NotServed(f"{path}: the switch closed the connection") from None
            raise SwitchError(f"cannot reach the switch: {reason}") from None
        if len(body) > MAX_PAGE:
            raise SwitchError(f"{path}: page is larger than {MAX_PAGE} bytes")
        return body.decode("utf-8", "replace")


class Session:
    """A logged-in view of the switch, handed to tool functions."""

    def __init__(self, http: HttpSession) -> None:
        self._http = http
        self._cache: dict[str, dict[str, Any]] = {}

    def page(self, section: str) -> dict[str, Any]:
        """The parsed variables of one section's page (cached for this session)."""
        if section not in PAGES:
            raise SwitchError(f"unknown section {section!r}")
        if section not in self._cache:
            path = PAGES[section]
            if path not in _ALLOWED:  # never reached; kept so -O can't drop the check
                raise SwitchError(f"page {path!r} is not on the allowlist")
            try:
                text = self._http.request(path)
            except Redirected:
                raise LoginError("the switch dropped the session (another login may have replaced it)") from None
            found = parse_vars(text)
            if is_login_page(found, text):
                raise LoginError("the switch dropped the session (another login may have replaced it)")
            self._cache[section] = found
        return self._cache[section]

    def try_page(self, section: str) -> dict[str, Any] | None:
        """Like page(), but None when this model doesn't serve the page."""
        try:
            return self.page(section)
        except NotServed:
            return None


class Switch:
    """Logs in for each call and out again afterwards. Calls run one at a time."""

    def __init__(self, host: str, username: str, password: str, timeout: float = 10.0,
                 http_factory: Callable[[str, float], Any] | None = None) -> None:
        self.host = host
        self._user = username
        self._password = password
        self._timeout = timeout
        self._factory = http_factory or HttpSession
        self._lock = threading.Lock()
        self._refused_until = 0.0

    def redact(self, text: str) -> str:
        return text.replace(self._password, "<redacted>") if self._password else text

    def call(self, fn: Callable[[Session], Any]) -> Any:
        with self._lock:
            if time.monotonic() < self._refused_until:
                raise LoginError("the last login was refused; waiting a minute before trying again")
            http = self._factory(self.host, self._timeout)
            self._login(http)
            try:
                return fn(Session(http))
            finally:
                try:
                    http.request("Logout.htm")
                except SwitchError as exc:
                    log.info("logout failed: %s", exc)

    def _login(self, http: Any) -> None:
        try:
            text = http.request("logon.cgi", {
                "username": self._user, "password": self._password, "cpassword": "", "logon": "Login",
            })
        except Redirected:
            return  # some firmware redirects to the main page after a good login
        info = parse_vars(text).get("logonInfo")
        code = info[0] if isinstance(info, list) and info else 0
        if not isinstance(code, int) or isinstance(code, bool):
            raise LoginError("login refused: the switch sent an unexpected answer")
        if code:
            if code in (1, 2, 6):
                self._refused_until = time.monotonic() + LOGIN_BACKOFF
            raise LoginError(f"login refused: {LOGIN_ERRORS.get(code, f'error {code}')}")
