#!/bin/sh
# Runs BarManager as PUID:PGID (Unraid: 99:100 = nobody:users) and makes sure
# the appdata in /config belongs to that user.
set -e

PUID="${PUID:-99}"
PGID="${PGID:-100}"
mkdir -p /config/images

if [ "$(id -u)" = "0" ]; then
    chown "$PUID:$PGID" /config /config/images
    find /config -maxdepth 1 -name 'barmanager.db*' -exec chown "$PUID:$PGID" {} +
    find /config/images \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown "$PUID:$PGID" {} +
    PORT="${BM_PORT:-80}"
    LOW="$(cat /proc/sys/net/ipv4/ip_unprivileged_port_start 2>/dev/null || echo 1024)"
    if [ "$PORT" -lt "$LOW" ]; then
        echo "poort $PORT is alleen als root te openen in deze container; BarManager draait als root" >&2
        exec "$@"
    fi
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$@"
fi
exec "$@"
