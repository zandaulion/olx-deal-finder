#!/usr/bin/env bash
# Load an existing OLX deal-finder dataset into the container volume.
#
#   ./import-data.sh <dir-containing-olxdeals.db>
#
# Files inside the volume are owned by the container's user as mapped through
# subuid, not by you, so they are copied in with `podman cp` rather than
# written directly -- that gets the ownership right without guessing at it.
set -euo pipefail

SRC="${1:?usage: import-data.sh <dir with olxdeals.db, searches.yaml, vapid_key.pem>}"
UNIT=olx-deals.service
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

[ -f "$SRC/olxdeals.db" ] || { echo "no olxdeals.db in $SRC" >&2; exit 1; }

echo "stopping $UNIT"
systemctl --user stop "$UNIT"

# The container must exist to copy into it, but must not be writing.
podman create --replace --name olx-import -v olx-deals-data:/data \
  localhost/olx-deals:latest true >/dev/null

for f in olxdeals.db searches.yaml vapid_key.pem; do
  if [ -f "$SRC/$f" ]; then
    podman cp "$SRC/$f" olx-import:/data/"$f"
    echo "  imported $f ($(stat -c%s "$SRC/$f") bytes)"
  else
    echo "  skipped  $f (not present)"
  fi
done

# vapid_key.pem is a private key; keep it that way.
podman run --rm -v olx-deals-data:/data --user 0 localhost/olx-deals:latest \
  sh -c 'chown -R 10002:10002 /data && chmod 600 /data/vapid_key.pem 2>/dev/null; true'
podman rm -f olx-import >/dev/null

echo "starting $UNIT"
systemctl --user start "$UNIT"
sleep 4
systemctl --user is-active "$UNIT"
