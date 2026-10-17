#!/usr/bin/env bash
# Isolated, read-only Sportradar goal feed observer on Ubuntu 24.04.
# Run: curl -fsSLo /tmp/barca-latency-install.sh URL && sudo bash /tmp/barca-latency-install.sh
set -Eeuo pipefail
if [[ "$EUID" -ne 0 ]]; then
  echo "Run as root via sudo bash /tmp/barca-latency-install.sh"; exit 1
fi
BASE="https://raw.githubusercontent.com/elmorshedy-del/Football-Bot/experiment/sportradar-barca-goal-latency-2026-10-17/experiments/goal-feed-latency"
APP="/opt/barca-goal-latency"
mkdir -p "$APP"
if ! command -v node >/dev/null 2>&1; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nodejs ca-certificates curl
fi
NODE_VERSION="$(node -p 'process.versions.node.split(".")[0]')"
if (( NODE_VERSION < 18 )); then
  echo "Node.js >=18 required (installed: $NODE_VERSION)"; exit 1
fi
curl --fail --silent --show-error --location --retry 3 \
  "$BASE/push-observer.mjs" -o "$APP/push-observer.mjs"
node --check "$APP/push-observer.mjs"
chmod 0644 "$APP/push-observer.mjs"
if ! id -u barcaobserver >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin barcaobserver
fi
if [[ ! -f /etc/barca-goal-latency.env ]]; then
  echo "Enter the Sportradar API key (input hidden; never committed or logged):"
  if [[ ! -r /dev/tty ]]; then echo "Interactive console required"; exit 2; fi
  IFS= read -r -s API_KEY < /dev/tty
  echo
  if [[ -z "$API_KEY" ]]; then echo "Empty API key; refusing to start"; exit 3; fi
  umask 077
  printf 'SPORTRADAR_API_KEY=%s\nSPORTRADAR_ACCESS_LEVEL=trial\nMAX_MINUTES=165\nPOLL_MS=10000\n' \
    "$API_KEY" > /etc/barca-goal-latency.env
  unset API_KEY
  chmod 600 /etc/barca-goal-latency.env
fi
cat >/etc/systemd/system/barca-goal-latency.service <<'EOF'
[Unit]
Description=Read-only Barcelona Sportradar goal-feed latency observer
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=barcaobserver
Group=barcaobserver
WorkingDirectory=/opt/barca-goal-latency
EnvironmentFile=/etc/barca-goal-latency.env
ExecStart=/usr/bin/node /opt/barca-goal-latency/push-observer.mjs
Restart=on-failure
RestartSec=5
StandardOutput=append:/var/log/barca-goal-latency.jsonl
StandardError=journal
NoNewPrivileges=true
ProtectHome=true
ProtectSystem=strict
PrivateTmp=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true

[Install]
WantedBy=multi-user.target
EOF
touch /var/log/barca-goal-latency.jsonl
chmod 0644 /var/log/barca-goal-latency.jsonl
systemctl daemon-reload
systemctl enable --now barca-goal-latency.service
echo "===== Observer status ====="
systemctl show -p ActiveState -p SubState barca-goal-latency.service
echo "===== Last collector messages ====="
sleep 2
tail -n 15 /var/log/barca-goal-latency.jsonl || true
echo "===== After the match ====="
echo "sudo tail -n 150 /var/log/barca-goal-latency.jsonl"
echo "sudo journalctl -u barca-goal-latency.service -n 100 --no-pager"
echo "Stop: sudo systemctl disable --now barca-goal-latency.service"
