"""Settings, read once from the environment."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field


def _secret(name: str) -> str:
    """Read NAME, or the file named by NAME_FILE (Docker/Podman secrets).

    The file wins if both are set. Trailing newlines are stripped.
    """
    path = os.environ.get(f"{name}_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().rstrip("\r\n")
        except OSError:
            raise SystemExit(f"easysmart-mcp: cannot read {name}_FILE") from None
    return os.environ.get(name, "")


def parse_labels(text: str) -> dict[int, str]:
    """Parse "1=uplink,2=server" into {1: "uplink", 2: "server"}.

    Labels live in the environment, not the repo, so home details stay private.
    """
    labels: dict[int, str] = {}
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        port, sep, label = item.partition("=")
        if not sep or not port.strip().isdigit() or not label.strip():
            raise SystemExit("easysmart-mcp: SWITCH_PORT_LABELS must look like 1=uplink,2=server")
        labels[int(port.strip())] = label.strip()[:64]
    return labels


@dataclass(frozen=True)
class Config:
    host: str
    username: str
    password: str = field(repr=False)
    token: str = field(repr=False)
    bind: str
    port: int
    allow_nets: tuple = ()
    labels: dict = field(default_factory=dict)
    timeout: float = 10.0
    problems: tuple = ()

    @classmethod
    def from_env(cls) -> Config:
        # Never echo setting values in errors: a password typed into the wrong
        # prompt would otherwise end up in the logs.
        host = os.environ.get("SWITCH_HOST", "192.168.0.1").strip()
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            raise SystemExit("easysmart-mcp: SWITCH_HOST must be the switch's IPv4 address") from None
        allow = os.environ.get("MCP_ALLOW_NETS", f"127.0.0.0/8,::1/128,{host}/24")
        try:
            allow_nets = tuple(
                ipaddress.ip_network(n.strip(), strict=False) for n in allow.split(",") if n.strip()
            )
            port = int(os.environ.get("MCP_PORT", "8080"))
            timeout = float(os.environ.get("SWITCH_TIMEOUT", "10"))
        except ValueError:
            raise SystemExit("easysmart-mcp: MCP_ALLOW_NETS, MCP_PORT or SWITCH_TIMEOUT is not valid") from None
        cfg = cls(
            host=host,
            username=os.environ.get("SWITCH_USERNAME", "admin"),
            password=_secret("SWITCH_PASSWORD"),
            token=_secret("MCP_TOKEN"),
            bind=os.environ.get("MCP_BIND", "127.0.0.1"),
            port=port,
            allow_nets=allow_nets,
            labels=parse_labels(os.environ.get("SWITCH_PORT_LABELS", "")),
            timeout=timeout,
        )
        problems = []
        if not cfg.password:
            problems.append("SWITCH_PASSWORD (or SWITCH_PASSWORD_FILE) is not set")
        if len(cfg.token) < 32:
            problems.append("MCP_TOKEN (or MCP_TOKEN_FILE) is not set or shorter than 32 characters")
        return cls(**{**cfg.__dict__, "problems": tuple(problems)})
