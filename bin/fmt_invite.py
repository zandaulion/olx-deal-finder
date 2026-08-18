"""Render a freshly created invite for the terminal. Reads JSON on stdin."""
import json
import sys

d = json.load(sys.stdin)
print()
print(f"  link:  {d['url'] or '(set PUBLIC_BASE_URL to render a link)'}")
print(f"  code:  {d['code']}")
print(f"  registers one device, expires in {d['expires_in_days']} days")
print()
print("  On iPhone send the code rather than the link: the app has to be")
print("  added to the Home Screen first, and the installed app keeps its own")
print("  storage, so a code redeemed in Safari does not register it.")
