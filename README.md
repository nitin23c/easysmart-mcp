# easysmart-mcp

A read-only [MCP](https://modelcontextprotocol.io) server for **TP-Link Easy
Smart** switches (the TL-SG105E, TL-SG108E, TL-SG116E family). It lets an AI
assistant such as Claude Code read the switch through the switch's own web UI.

- See every port's link state and speed, speed setting, flow control and LAG
  group, with good and bad packet counts.
- Read the VLAN setup: 802.1Q VLANs with tagged and untagged ports, PVIDs,
  port-based VLANs and MTU VLAN.
- Read other settings: IP, mirroring, LAGs, loop prevention, IGMP snooping,
  QoS, bandwidth limits and storm control.
- Audit the switch for bad cables, half-duplex or slow links, labelled ports
  without link, VLAN mistakes and more.

**It never changes the switch.** Version 0.1 has no write tools at all.

Everything stays on your network. The server talks only to the switch's
address and needs no TP-Link cloud account. It uses only Python's standard
library: no third-party code runs next to the switch password.

> **Not affiliated with TP-Link.** The web UI is undocumented. Firmware
> updates can change it. Please open an issue with your model and firmware,
> whether it works or not.

## How it works

```
Claude Code ──HTTP + bearer token──▶ easysmart-mcp (small LXC/VM) ──HTTP login──▶ Easy Smart switch
```

- Each tool call logs in to the switch, fetches the pages it needs, and logs
  out. Calls run one at a time.
- The web UI keeps its data in JavaScript variables inside each page. The
  server parses those literals with a small parser of its own. Nothing from
  the switch is ever evaluated.
- Only an allowlist of display pages can be fetched. On the firmware tested so far
  the `.cgi` URLs change settings even on a plain GET, so the server never
  requests one, apart from the login form (`logon.cgi`).
- After a rejected password it waits a minute before trying again, so a
  wrong password can't hammer the switch.
- The MCP side is the standard library's HTTP server speaking MCP's
  [streamable HTTP transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
  with plain JSON responses: no SSE and no sessions.

## Tools

| Tool | Does |
|---|---|
| `switch_info` | Model, hardware and firmware version, MAC, IP, netmask, gateway, DHCP on or off, port count, loop prevention |
| `switch_ports` | Per port: label, enabled, link (`down`, `100M full`, `1000M full`, ...), speed setting, flow control, LAG group, TX and RX good and bad packets. `port` filter |
| `switch_vlans` | 802.1Q VLANs with tagged and untagged member ports, each port's PVID, port-based VLANs, MTU VLAN |
| `switch_config` | Raw values of the settings pages: `ip`, `lag`, `mirror`, `loop_prevention`, `igmp`, `qos`, `bandwidth`, `storm`, `vlan_mtu`, `vlan_port`, `vlan_8021q`, `vlan_pvid` by default. `sections` picks some, and also takes `system`, `ports` and `stats` for their raw values. Pages a model doesn't serve are listed as unavailable |
| `switch_audit` | Flags bad packet counters, half duplex, links below gigabit, fixed speeds, labelled ports with no link, disabled ports, ports outside every 802.1Q VLAN, PVIDs that don't match an untagged VLAN, loop prevention off, port mirroring on |

Packet counters count since the switch booted or they were last cleared in
the web UI.

**Port labels.** The switch has no port descriptions, so you can name ports
in the server's settings: `SWITCH_PORT_LABELS=1=uplink,2=server,5=nas`. They
show up in every tool's output, and the audit warns when a labelled port has
no link. Labels stay in the server's environment file, not in any config the
switch or this repo holds.

## Install

Run it on an always-on machine on the same LAN as the switch. There are two
ways: [Docker](#docker), or a plain [systemd service](#systemd) on a small
LXC or VM.

### Docker

The image is built from this repo: Python 3.14 slim with the package copied
in, nothing from PyPI. It runs as an unprivileged user on a read-only
filesystem with no capabilities. The password and token are Docker secret
files, and your own values go in `.env`, which git ignores.

```sh
git clone https://github.com/nitin23c/easysmart-mcp.git
cd easysmart-mcp
cp env.example .env          # set SWITCH_HOST, MCP_ALLOW_NETS, and optionally labels and port
mkdir -p secrets && chmod 700 secrets
docker compose build
docker run --rm easysmart-mcp:local --new-token > secrets/mcp_token
(umask 077; printf 'Switch password: '; stty -echo; read -r p; stty echo; echo
 printf '%s' "$p" > secrets/switch_password)
chmod 644 secrets/*          # the container user must read them; the 700 folder keeps others out
docker compose run --rm --no-deps easysmart-mcp --check   # logs in once, prints the model
docker compose up -d
docker compose ps            # STATUS shows "healthy"
```

Clients on the LAN reach the container with their own address. Requests from
the Docker host itself arrive from Docker's bridge network, which is why
`env.example` and `compose.yaml` add `172.16.0.0/12` to `MCP_ALLOW_NETS`.
On Docker Desktop (macOS, Windows) they arrive from `192.168.65.0/24`
instead, so add that too if you call the server from the same machine.
The endpoint is `http://<docker-host>:<MCP_HOST_PORT>/mcp`. If port 8080 is
taken on the host, set `MCP_HOST_PORT` in `.env`, for example to 9090.

The health check (`--healthcheck`) only checks that the server refuses an
unauthenticated request. It needs no secrets and never contacts the switch.
To update: `git pull && docker compose up -d --build`.

### systemd

It needs Python 3.11 or newer (Debian 12 and 13 ship one) and nothing from
PyPI. On a Debian or Ubuntu host, as root:

```sh
git clone https://github.com/nitin23c/easysmart-mcp.git
cd easysmart-mcp
./deploy/install.sh                # venv, this package, system user, systemd unit
easysmart-mcp-setup                # switch address and password (hidden), makes a token
easysmart-mcp-setup --check        # logs in once, prints the model and firmware
systemctl start easysmart-mcp
journalctl -u easysmart-mcp -n 5   # "INFO easysmart_mcp easysmart-mcp 0.1.0 on 0.0.0.0:8080, ..."
```

If port 8080 is taken, pick another port on the first setup, for example
`MCP_PORT=9090 easysmart-mcp-setup`. `deploy/install.sh` is safe to
run again to update.

### The switch password

Easy Smart switches have one admin account, `admin` by default. The password
can only use letters, digits and `_`, up to 16 characters.
`easysmart-mcp-setup` asks for it without echoing it and stores it only in
`/etc/easysmart-mcp.env` (mode 600, root). systemd reads that file before
dropping to the unprivileged `easysmart-mcp` user. Run the setup again after
changing the password. `--new-token` replaces the token, and `--print-token`
prints it.

## Configuration

All settings are environment variables: in `/etc/easysmart-mcp.env` for
systemd, or in `.env` for Docker. With Docker, only the variables that
[`compose.yaml`](compose.yaml) passes reach the container: `SWITCH_HOST`,
`SWITCH_USERNAME`, `SWITCH_PORT_LABELS`, `SWITCH_TIMEOUT`, `MCP_ALLOW_NETS`
and `MCP_HOST_PORT`. The image fixes `MCP_BIND` and `MCP_PORT`. Each
secret can instead be read from a file by adding `_FILE` to its name
(`SWITCH_PASSWORD_FILE`, `MCP_TOKEN_FILE`).

| Variable | Default | Meaning |
|---|---|---|
| `SWITCH_HOST` | `192.168.0.1` | Switch address (the factory default) |
| `SWITCH_USERNAME` | `admin` | Web UI user |
| `SWITCH_PASSWORD` | (required) | Web UI password |
| `SWITCH_PORT_LABELS` | (none) | Port names, e.g. `1=uplink,2=server` |
| `SWITCH_TIMEOUT` | `10` | Seconds per switch request |
| `MCP_TOKEN` | (required) | Bearer token, at least 32 characters |
| `MCP_BIND` | `127.0.0.1` | Listen address. `easysmart-mcp-setup` and the Docker image use `0.0.0.0` |
| `MCP_HOST_PORT` | `8080` | Docker only: the port on the Docker host |
| `MCP_PORT` | `8080` | Listen port. The endpoint is `/mcp` |
| `MCP_ALLOW_NETS` | `127.0.0.0/8`, `::1/128` and `SWITCH_HOST`/24 (Docker: see `env.example`) | Comma-separated CIDRs allowed to connect |

## Connect Claude Code

Store the token in the macOS Keychain (paste it at the hidden prompt):

```sh
security add-generic-password -a easysmart-mcp -s easysmart-mcp-token -w
```

Add the server for your user, with a
[`headersHelper`](https://docs.anthropic.com/en/docs/claude-code/mcp) that
reads the token from the Keychain, so the token isn't stored in any config
file:

```sh
claude mcp add-json --scope user switch '{
  "type": "http",
  "url": "http://<server-ip>:<port>/mcp",
  "headersHelper": "/path/to/easysmart-mcp/examples/mcp-headers.sh"
}'
```

Every tool only reads, so
[`examples/claude-settings.json`](examples/claude-settings.json) allows them
all without a prompt. On Linux, replace the helper with one that reads the
token from your own secret store.

## Security

Read this before installing it.

- **The switch login is not encrypted.** Easy Smart switches only serve plain
  HTTP, and the login form sends the password in clear text. Anyone who can
  watch traffic between the server and the switch can capture it. Each tool
  call logs in once. Use a password you use nowhere else.
- **The server holds the switch password.** Anyone with root on the host
  running it, or on its hypervisor, can read it. Give it its own small
  container, or one shared only with similar services.
- **Read-only by design.** Only display pages on an allowlist are fetched,
  never a `.cgi` URL other than the login. A test checks every request the
  tools make.
- **One session at a time.** The switch tracks web logins, and a login from
  the server may end a browser session that's open in the web UI at the same
  time, or the other way round. A dropped session is reported as an error,
  not retried.
- **Access control:** a source-address allowlist (`MCP_ALLOW_NETS`), then a
  constant-time bearer token check. Any request carrying an `Origin` header
  is refused, so a web page in your browser can't reach the server through
  DNS rebinding. Bodies are capped at 64 KB, JSON-RPC batches are refused,
  and a client that doesn't finish its request within 15 seconds is dropped.
- **Docker can hide client addresses.** With Docker's userland proxy, or on
  Docker Desktop, every client can appear to come from the bridge gateway in
  `172.16.0.0/12`. Then the address allowlist no longer filters and only the
  token protects the server. Use host firewall rules if that matters to you.
- **Proxy settings are ignored** for the switch connection, so an
  `http_proxy` in the environment can't receive the clear-text login.
- **Untrusted page data is bounded.** The page parser is linear, nesting is
  capped, and table sizes the switch reports are clamped, so a spoofed or
  broken switch can't make the server hang or run out of memory.
- **Plain HTTP to the server too.** The token travels unencrypted across the
  LAN. If your LAN isn't trusted, put the server behind a TLS reverse proxy
  and keep `MCP_BIND=127.0.0.1`. **Never forward the port to the internet.**
- The systemd unit runs as an unprivileged user with no capabilities, a
  read-only system, private `/tmp`, and other hardening.

Report security problems privately through GitHub's *Report a vulnerability*
button. Please don't open a public issue.

## Command line

`python -m easysmart_mcp` (or `easysmart-mcp`) starts the server. Other
options: `--check` logs in once and prints the switch info, `--new-token`
prints a random token, `--healthcheck` exits 0 if the local server is
answering, and `--version` prints the version.

## Development

```sh
PYTHONPATH=src python3 -m unittest -v     # or: uv run pytest
```

The tests run against a fake switch web UI on localhost and never touch a
real switch.

## Licence

MIT. See [LICENSE](LICENSE).
