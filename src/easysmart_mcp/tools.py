"""The MCP tools. All of them only read the switch."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from .config import Config
from .switch import PAGES, Session, Switch, SwitchError

log = logging.getLogger("easysmart_mcp.tools")

# Link and speed codes used by the web UI's port pages.
SPEEDS = {
    0: "down",
    1: "auto",
    2: "10M half",
    3: "10M full",
    4: "100M half",
    5: "100M full",
    6: "1000M full",
}
FAST = {6}  # gigabit; extend for 2.5G/10G models once their codes are known


class ToolError(Exception):
    """A problem reported back to the model as a tool result, not a crash."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    writes: bool
    run: Callable[[Any, dict], Any]


class Context:
    """What a tool needs: the switch and the port labels from the config."""

    def __init__(self, cfg: Config, switch: Switch) -> None:
        self.cfg = cfg
        self.switch = switch

    def label(self, port: int) -> str:
        return self.cfg.labels.get(port, "")


# ---------------------------------------------------------------- helpers


def _first(value: Any) -> Any:
    """The web UI wraps many single values in a one-item array."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(_first(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _speed(code: Any) -> str:
    c = _int(code, -1)
    return SPEEDS.get(c, f"code {c}")


def _members(mask: Any, count: int) -> list[int]:
    m = _int(mask)
    return [p for p in range(1, count + 1) if m & (1 << (p - 1))]


MAX_ITEMS = 4096  # more than any VLAN or port table; bounds what the switch can make us allocate


def _list(d: dict, key: str, count: int) -> list[Any]:
    count = max(0, min(count, MAX_ITEMS))
    v = d.get(key)
    if not isinstance(v, list):
        return [None] * count
    return (v + [None] * count)[:count]


def port_count(s: Session) -> int:
    for section in ("ports", "stats"):
        d = s.try_page(section) or {}
        for key in ("max_port_num", "portNum", "port_num"):
            n = _int(d.get(key), 0)
            if 0 < n <= 64:
                return n
    raise SwitchError("could not tell how many ports the switch has")


def _check_port(args: dict, count: int) -> int | None:
    port = args.get("port")
    if port is None:
        return None
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= count:
        raise ToolError(f"port must be a number from 1 to {count}")
    return port


# ------------------------------------------------------------------ reads


def _system(s: Session) -> dict[str, Any]:
    d = s.page("system")
    info = d.get("info_ds") if isinstance(d.get("info_ds"), dict) else {}
    out = {
        "model": _first(info.get("descriStr")),
        "hardware": _first(info.get("hardwareStr")),
        "firmware": _first(info.get("firmwareStr")),
        "mac": _first(info.get("macStr")),
        "ip": _first(info.get("ipStr")),
        "netmask": _first(info.get("netmaskStr")),
        "gateway": _first(info.get("gatewayStr")),
    }
    if not any(out.values()):
        out["raw"] = d
    return out


def info(ctx: Context, _args: dict) -> Any:
    def run(s: Session) -> Any:
        out = _system(s)
        out["ports"] = port_count(s)
        ip = (s.try_page("ip") or {}).get("ip_ds")
        if isinstance(ip, dict) and "state" in ip:
            out["dhcp"] = bool(_int(ip.get("state")))
        lp = s.try_page("loop_prevention") or {}
        if "lpEn" in lp:
            out["loop_prevention"] = bool(_int(lp["lpEn"]))
        return out

    return ctx.switch.call(run)


def _port_rows(ctx: Context, s: Session) -> list[dict[str, Any]]:
    n = port_count(s)
    cfg = s.page("ports").get("all_info") or {}
    st = (s.try_page("stats") or {}).get("all_info") or {}
    state = _list(cfg, "state", n)
    lag = _list(cfg, "trunk_info", n)
    spd_cfg, spd_act = _list(cfg, "spd_cfg", n), _list(cfg, "spd_act", n)
    fc_cfg, fc_act = _list(cfg, "fc_cfg", n), _list(cfg, "fc_act", n)
    link = _list(st, "link_status", n)
    pkts = st.get("pkts") if isinstance(st.get("pkts"), list) else []
    rows = []
    for i in range(n):
        p = i + 1
        actual = spd_act[i] if spd_act[i] is not None else link[i]
        row: dict[str, Any] = {
            "port": p,
            "label": ctx.label(p),
            "enabled": bool(_int(state[i], 1)),
            "link": _speed(actual),
            "speed_setting": _speed(spd_cfg[i]),
            "flow_control": {"setting": bool(_int(fc_cfg[i])), "active": bool(_int(fc_act[i]))},
            "lag": _int(lag[i]) or None,
        }
        if len(pkts) >= 4 * n:
            row.update({
                "tx_good": _int(pkts[4 * i]), "tx_bad": _int(pkts[4 * i + 1]),
                "rx_good": _int(pkts[4 * i + 2]), "rx_bad": _int(pkts[4 * i + 3]),
            })
        rows.append(row)
    return rows


def ports(ctx: Context, args: dict) -> Any:
    def run(s: Session) -> Any:
        rows = _port_rows(ctx, s)
        port = _check_port(args, len(rows))
        if port is not None:
            rows = [r for r in rows if r["port"] == port]
        return {
            "ports": rows,
            "note": "Packet counters count since the switch booted or they were last cleared.",
        }

    return ctx.switch.call(run)


def _vlans(s: Session, n: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    q = (s.try_page("vlan_8021q") or {}).get("qvlan_ds")
    if isinstance(q, dict):
        vids = q.get("vids") if isinstance(q.get("vids"), list) else []
        count = _int(q.get("count"), len(vids)) or len(vids)
        count = max(0, min(count, len(vids), MAX_ITEMS))
        names = _list(q, "names", count)
        tag, untag = _list(q, "tagMbrs", count), _list(q, "untagMbrs", count)
        out["dot1q"] = {
            "enabled": bool(_int(q.get("state"))),
            "vlans": [
                {"vid": _int(vids[i]) if i < len(vids) else None, "name": names[i] or "",
                 "tagged": _members(tag[i], n), "untagged": _members(untag[i], n)}
                for i in range(count)
            ],
        }
    pv = (s.try_page("vlan_pvid") or {}).get("pvid_ds")
    if isinstance(pv, dict) and isinstance(pv.get("pvids"), list):
        out.setdefault("dot1q", {})["pvids"] = {
            p: _int(v) for p, v in zip(range(1, n + 1), pv["pvids"])
        }
    b = (s.try_page("vlan_port") or {}).get("pvlan_ds")
    if isinstance(b, dict):
        vids = b.get("vids") if isinstance(b.get("vids"), list) else []
        count = _int(b.get("count"), len(vids)) or len(vids)
        count = max(0, min(count, len(vids), MAX_ITEMS))
        mbrs = _list(b, "mbrs", count)
        out["port_based"] = {
            "enabled": bool(_int(b.get("state"))),
            "vlans": [{"vid": _int(vids[i]) if i < len(vids) else None, "ports": _members(mbrs[i], n)}
                      for i in range(count)],
        }
    m = (s.try_page("vlan_mtu") or {}).get("mtu_ds")
    if isinstance(m, dict):
        out["mtu_vlan"] = {"enabled": bool(_int(m.get("state"))), "uplink_port": _int(m.get("uplinkPort")) or None}
    return out


def vlans(ctx: Context, _args: dict) -> Any:
    def run(s: Session) -> Any:
        n = port_count(s)
        out = _vlans(s, n)
        if not out:
            raise ToolError("this switch did not return any VLAN pages in a known format")
        out["labels"] = {p: ctx.label(p) for p in range(1, n + 1) if ctx.label(p)}
        return out

    return ctx.switch.call(run)


CONFIG_SECTIONS = ("ip", "lag", "mirror", "loop_prevention", "igmp", "qos", "bandwidth", "storm",
                   "vlan_mtu", "vlan_port", "vlan_8021q", "vlan_pvid")


def config(ctx: Context, args: dict) -> Any:
    want = args.get("sections") or list(CONFIG_SECTIONS)
    if not isinstance(want, list) or not all(isinstance(x, str) for x in want):
        raise ToolError("sections must be a list of names")
    bad = [x for x in want if x not in PAGES]
    if bad:
        raise ToolError(f"unknown sections {bad}; known: {sorted(PAGES)}")

    def run(s: Session) -> Any:
        out: dict[str, Any] = {"sections": {}, "unavailable": []}
        for name in want:
            d = s.try_page(name)
            if d is None:
                out["unavailable"].append(name)
            else:
                out["sections"][name] = d
        out["note"] = ("Values are the web UI's own variables. Port lists are usually per port from 1; "
                       "VLAN member fields are bitmasks with bit 0 for port 1.")
        return out

    return ctx.switch.call(run)


def _shared_bad(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Three or more ports whose RX bad counts are within 10% of each other."""
    bad = [(r["port"], r.get("rx_bad", 0)) for r in rows if r["enabled"] and r.get("rx_bad", 0) > 0]
    best: list[tuple[int, int]] = []
    for _, base in bad:
        group = [(p, n) for p, n in bad if base <= n <= base * 1.1]
        if len(group) > len(best):
            best = group
    if len(best) < 3:
        return None
    counts = [n for _, n in best]
    low, high = min(counts), max(counts)
    return {"ports": sorted(p for p, _ in best), "about": str(low) if low == high else f"{low}-{high}"}


def audit(ctx: Context, _args: dict) -> Any:
    def run(s: Session) -> Any:
        rows = _port_rows(ctx, s)
        n = len(rows)
        problems: list[dict[str, Any]] = []

        def add(level: str, what: str, port: int | None = None) -> None:
            item: dict[str, Any] = {"level": level, "issue": what}
            if port is not None:
                item["port"] = port
                if ctx.label(port):
                    item["label"] = ctx.label(port)
            problems.append(item)

        shared = _shared_bad(rows)
        if shared:
            add("warning", f"ports {', '.join(map(str, shared['ports']))} all show "
                           f"{'about ' if '-' in shared['about'] else ''}{shared['about']} RX bad packets: a shared cause (a loop or broadcast storm, "
                           "a power or link event, one sending device) is likelier than a bad cable on each. "
                           "Clear the counters in the web UI and check again later")
        for r in rows:
            p, link = r["port"], r["link"]
            if not r["enabled"]:
                add("warning" if r["label"] else "info", "port is disabled", p)
                continue
            rx_bad = 0 if shared and p in shared["ports"] else r.get("rx_bad", 0)
            if r.get("tx_bad") or rx_bad:
                good = r.get("rx_good", 0) + r.get("tx_good", 0)
                share = f", {100 * (rx_bad + r.get('tx_bad', 0)) / good:.2g}% of good" if good else ""
                add("warning", f"bad packets: rx {rx_bad}, tx {r.get('tx_bad', 0)}{share} "
                               "(often a bad cable or connector)", p)
            if link == "down":
                if r["label"]:
                    add("warning", "labelled port has no link", p)
                continue
            if "half" in link:
                add("warning", f"link is {link}: duplex mismatch or bad cable", p)
            elif not any(SPEEDS[c] == link for c in FAST):
                add("info", f"link is only {link}: a slower device, or a cable with a bad pair", p)
            if r["speed_setting"] not in ("auto", "down"):
                add("info", f"speed is fixed at {r['speed_setting']}, not auto", p)

        lp = s.try_page("loop_prevention") or {}
        if "lpEn" in lp and not _int(lp["lpEn"]):
            add("info", "loop prevention is off")
        mirror = s.try_page("mirror") or {}
        if _int(mirror.get("MirrEn")):
            add("info", f"port mirroring is on, to port {_int(mirror.get('MirrPort')) or '?'}")

        v = _vlans(s, n).get("dot1q") or {}
        if v.get("enabled") and v.get("vlans"):
            pvids = v.get("pvids") or {}
            for p in range(1, n + 1):
                member = [x["vid"] for x in v["vlans"] if p in x["tagged"] or p in x["untagged"]]
                untagged = [x["vid"] for x in v["vlans"] if p in x["untagged"]]
                if not member:
                    add("warning", "port is in no 802.1Q VLAN", p)
                elif p in pvids and pvids[p] not in untagged:
                    add("warning", f"PVID {pvids[p]} is not a VLAN this port is untagged in", p)

        return {
            "ok": not any(x["level"] == "warning" for x in problems),
            "problems": problems,
            "checked": ["port state", "link speed and duplex", "bad packet counters (and the same count on many ports)", "fixed speeds",
                        "labelled ports without link", "loop prevention", "port mirroring",
                        "802.1Q membership and PVIDs"],
            "note": "Packet counters count since the switch booted or they were last cleared.",
        }

    return ctx.switch.call(run)


# ---------------------------------------------------------------- catalog

_NONE: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

ALL_TOOLS: tuple[Tool, ...] = (
    Tool("switch_info",
         "Switch model, hardware and firmware version, MAC, IP settings, port count, DHCP and loop "
         "prevention state.",
         _NONE, False, info),
    Tool("switch_ports",
         "Per port: label, enabled, link state and speed, speed setting, flow control, LAG group, and "
         "good and bad packet counts.",
         {"type": "object", "additionalProperties": False,
          "properties": {"port": {"type": "integer", "minimum": 1, "description": "Only this port"}}},
         False, ports),
    Tool("switch_vlans",
         "VLAN setup: 802.1Q VLANs with tagged and untagged member ports and each port's PVID, "
         "port-based VLANs, and MTU VLAN.",
         _NONE, False, vlans),
    Tool("switch_config",
         "Read settings pages as the web UI's raw values: " + ", ".join(CONFIG_SECTIONS)
         + ". Pages this model doesn't serve are listed as unavailable.",
         {"type": "object", "additionalProperties": False,
          "properties": {"sections": {"type": "array", "items": {"type": "string", "enum": sorted(PAGES)},
                                      "description": "Limit to these sections (default all settings pages)"}}},
         False, config),
    Tool("switch_audit",
         "Check the switch for problems: bad packet counters, half duplex or slow links, labelled ports "
         "without link, disabled ports, ports outside every VLAN, PVID mismatches, loop prevention off, "
         "port mirroring on.",
         _NONE, False, audit),
)


def enabled_tools(_cfg: Config) -> dict[str, Tool]:
    return {t.name: t for t in ALL_TOOLS}
