#!/usr/bin/env bash
# Invite and device management. Talks to the tailnet-only Caddy surface,
# which is what authorises admin -- there is no password, being on the
# tailnet is the credential. Will not work from off the tailnet.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Where the tailnet-only admin surface lives. Kept in site.env rather than
# here, so the repository carries no address of the machine it runs on.
[[ -f "$ROOT/site.env" ]] && { set -a; . "$ROOT/site.env"; set +a; }
API="${API:-${ADMIN_API:-}}"
: "${API:?set ADMIN_API in site.env (see site.env.example)}"
j() { python3 -m json.tool 2>/dev/null || cat; }
get() { curl -fsS --max-time 15 "$API$1"; }
post() { curl -fsS --max-time 15 -X POST "$API$1" \
         -H 'Content-Type: application/json' -d "${2:-{\}}"; }

usage() {
  cat <<'USAGE'
usage: ./admin.sh <command>

  invite [label]        create an invite (prints the link and the code)
  invites               list invites, with codes for the ones still usable
  unvite <id>           revoke a single unused invite
  prune                 delete every used and expired invite
  devices               list registered devices
  revoke <id>           lock a device out
  forget <id>           delete a device (its push subscription goes too)
  prune-devices         delete every revoked device
  unrevoke <id>         let it back in
  name <id> <label>     label a device

Each invite registers one device. A second phone needs a second invite.
Codes are stored hashed once redeemed, so a spent invite shows no code --
issue a new one instead of trying to recover it.
USAGE
}

case "${1:-}" in
  invite)
    post /api/admin/invites "{\"label\":\"${2:-}\"}" \
      | python3 "$ROOT/bin/fmt_invite.py" ;;
  invites)  get /api/admin/invites | python3 "$ROOT/bin/fmt_invites.py" ;;
  prune)    post /api/admin/invites/prune | j ;;
  unvite)   post "/api/admin/invites/${2:?id}/revoke" | j ;;
  devices)  get /api/admin/devices | python3 "$ROOT/bin/fmt_devices.py" ;;
  revoke)   post "/api/admin/devices/${2:?id}/revoke" '{"revoked":true}' | j ;;
  forget)   curl -fsS --max-time 15 -X DELETE "$API/api/admin/devices/${2:?id}" | j ;;
  prune-devices) post /api/admin/devices/prune | j ;;
  unrevoke) post "/api/admin/devices/${2:?id}/revoke" '{"revoked":false}' | j ;;
  name)     post "/api/admin/devices/${2:?id}/label" "{\"label\":\"${3:?label}\"}" | j ;;
  *) usage; exit 1 ;;
esac
