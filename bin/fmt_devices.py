"""Render the admin device list for the terminal. Reads JSON on stdin."""
import json
import sys

devices = json.load(sys.stdin).get("devices", [])
if not devices:
    print("  no devices")
    raise SystemExit(0)

for d in devices:
    state = "REVOKED" if d["revoked"] else "active"
    push = "" if d.get("has_push") else "  (no notifications)"
    seen = d["last_seen"][:16] if d["last_seen"] else "never"
    print(f"  #{d['id']:<3} {str(d['label'] or '-'):<18} {state:<8} "
          f"last seen {seen}{push}")
