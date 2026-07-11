#!/usr/bin/env bash
# paperless-ngx-reconsume-plugin — bare-metal / LXC installer
#
# Copies the plugin to PLUGIN_DIR, registers it in paperless.conf and adds
# PYTHONPATH drop-ins to the paperless systemd units. Idempotent.
#
# Usage:  sudo ./install-lxc.sh
# Env:    PAPERLESS_CONF   (default /opt/paperless/paperless.conf)
#         PLUGIN_DIR       (default /opt/paperless/plugins)
#         SERVICES         (default "webserver consumer scheduler task-queue")

set -euo pipefail

PAPERLESS_CONF="${PAPERLESS_CONF:-/opt/paperless/paperless.conf}"
PLUGIN_DIR="${PLUGIN_DIR:-/opt/paperless/plugins}"
SERVICES="${SERVICES:-webserver consumer scheduler task-queue}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

[ -f "$PAPERLESS_CONF" ] || { echo "ERROR: $PAPERLESS_CONF not found (set PAPERLESS_CONF)"; exit 1; }

echo "==> Installing plugin to $PLUGIN_DIR/reconsume"
mkdir -p "$PLUGIN_DIR"
cp -r "$SRC_DIR/reconsume" "$PLUGIN_DIR/"

echo "==> Registering PAPERLESS_APPS in $PAPERLESS_CONF"
if grep -q "^PAPERLESS_APPS=" "$PAPERLESS_CONF"; then
  if ! grep -q "^PAPERLESS_APPS=.*reconsume" "$PAPERLESS_CONF"; then
    sed -i 's/^PAPERLESS_APPS=\(.*\)$/PAPERLESS_APPS=\1,reconsume/' "$PAPERLESS_CONF"
  fi
else
  printf '\n# paperless-ngx-reconsume-plugin\nPAPERLESS_APPS=reconsume\n' >> "$PAPERLESS_CONF"
fi

echo "==> Adding PYTHONPATH drop-ins"
for s in $SERVICES; do
  unit="paperless-$s.service"
  systemctl cat "$unit" >/dev/null 2>&1 || { echo "  ! $unit not found, skipping"; continue; }
  mkdir -p "/etc/systemd/system/$unit.d"
  printf '[Service]\nEnvironment=PYTHONPATH=%s\n' "$PLUGIN_DIR" \
    > "/etc/systemd/system/$unit.d/reconsume.conf"
  echo "  + $unit"
done

systemctl daemon-reload

echo "==> Restarting services"
for s in $SERVICES; do
  systemctl restart "paperless-$s.service" 2>/dev/null && echo "  ✓ paperless-$s" || true
done

echo
echo "Done. Verify with:"
echo "  journalctl -u paperless-task-queue -n 100 | grep reconsume"
echo "You should see 'reconsume.tasks.full_consume_steps' in the celery task list."
