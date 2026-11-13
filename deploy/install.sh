#!/bin/sh
# install.sh: install or update easysmart-mcp on a Debian or Ubuntu host (a
# small LXC or VM is ideal). Run as root from a checkout of this repo:
#
#   sudo ./deploy/install.sh
#
# The server uses only Python's standard library, so nothing is downloaded
# from PyPI. Installs: python3-venv (apt, if missing), a venv in
# /opt/easysmart-mcp with this package copied in, an easysmart-mcp system user
# and the systemd unit. Does not start the service.
set -eu
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
SRC=$(cd "$(dirname "$0")/.." && pwd)
DEST=/opt/easysmart-mcp
export DEBIAN_FRONTEND=noninteractive

if ! python3 -c 'import venv' 2>/dev/null || ! python3 -m venv --help >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq python3-venv >/dev/null
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || {
  echo "needs Python 3.11 or newer" >&2; exit 1; }
id easysmart-mcp >/dev/null 2>&1 || \
  useradd --system --home-dir "$DEST" --shell /usr/sbin/nologin easysmart-mcp

mkdir -p "$DEST"
[ -x "$DEST/venv/bin/python" ] || python3 -m venv --without-pip "$DEST/venv"
SITE=$("$DEST/venv/bin/python" -I -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
rm -rf "$SITE/easysmart_mcp"
cp -R "$SRC/src/easysmart_mcp" "$SITE/easysmart_mcp"
find "$SITE/easysmart_mcp" -name __pycache__ -prune -exec rm -rf {} +

install -m 0755 "$SRC/deploy/easysmart-mcp-setup" /usr/local/sbin/easysmart-mcp-setup
install -m 0644 "$SRC/deploy/easysmart-mcp.service" /etc/systemd/system/easysmart-mcp.service
chown -R root:root "$DEST"
chmod -R go-w,o+rX "$DEST"
systemctl daemon-reload
systemctl enable easysmart-mcp >/dev/null 2>&1

"$DEST/venv/bin/python" -I -m easysmart_mcp --version
if [ -f /etc/easysmart-mcp.env ]; then
  echo "Installed. Restart to apply: systemctl restart easysmart-mcp"
else
  echo "Installed. Next: easysmart-mcp-setup, then systemctl start easysmart-mcp"
fi
