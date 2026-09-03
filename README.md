# olx-deal-finder

Track products on [OLX.ro](https://www.olx.ro) and surface genuinely good deals — on a
self-hosted, mobile-friendly dashboard you can reach from your phone.

It periodically pulls listings for the searches you configure (e.g. *used iPhone 14*,
*Samsung Galaxy Z Fold 6*), diffs them against a local database to detect new listings
and price changes, scores each one against the market, and shows the results — including
price-drop history and per-listing exclusions.

## Features

- **Open OLX.ro JSON API** — no scraping, no auth, no API key. Polite paginated pulls.
- **Deal scoring** — normalises mixed EUR/RON prices, filters junk (broken / parts /
  accessory listings), and flags a listing as a deal only when it's genuinely below the
  clean market price for its *model + condition* (below Q1, ≥20% under median, above a
  price floor so wrong-model/scam listings don't masquerade as bargains).
- **Price history + Drops** — every price change is recorded; each card shows a sparkline,
  and a dedicated **Drops** view lists listings that got cheaper since first seen.
- **Per-search tabs** — each search is tracked and viewed independently.
- **Manage from the browser** — add/edit/delete searches, no file editing. Includes a
  live **model-key & category finder** so you don't have to hunt through OLX URLs.
- **Manual exclusion** — hide a mislabeled/unwanted listing (✕ button or long-press);
  it's removed from the stats and stays hidden on future syncs, restorable from Manage.
- **Triage by swipe** — swipe a card left to mark it seen, right to favourite, or use
  the buttons. Unread cards carry an accent rail down the left edge; seen cards drop the
  rail, fade the photo and lighten the title. `Hide seen` clears them from the view
  entirely once a search has been worked through.
- **Runs itself** — hourly sync + always-on dashboard via systemd user services.

## How OLX data works (useful to know)

- Base endpoint: `https://www.olx.ro/api/v1/offers/`
- Structured filters: `filter_enum_model[0]=iphone_14`, `filter_enum_state[0]=used`,
  `filter_float_price:from` / `:to`, `region_id`.
- **Phone categories are per-brand** (Apple = 948, Samsung = 956, …). Because a model key
  is already brand-specific, **use `category_id=0` (all categories)** to avoid a mismatched
  category hiding your results. The app defaults to this.

## Setup

```bash
pip install -r requirements.txt

# 1) define what to track (or use the dashboard's Manage tab)
#    see searches.yaml for the format

# 2) run one sync cycle
python run.py

# 3) launch the dashboard
python -m olxdeals.dashboard --host 0.0.0.0 --port 8000
# open http://<this-machine>:8000/
```

Binding to `0.0.0.0` makes it reachable over a LAN or [Tailscale](https://tailscale.com)
from your phone. Anyone who can reach the port can edit searches and trigger syncs — keep
it on a trusted network.

## Finding a model key / category id

```bash
python -m olxdeals.discover "iphone 15 pro"
```

Prints the category ids and model keys that actually appear in real listings, with counts.
The dashboard's Manage tab has the same finder as tappable chips.

## Always-on (rootless podman)

[`deploy/`](deploy) runs the dashboard as a rootless container with an hourly
sync, surviving logout and reboot. No root, no system-wide Python.

```bash
podman build -t olx-deals:latest -f deploy/Containerfile .
loginctl enable-linger "$USER"                       # run without being logged in

install -d -m 700 ~/.config/olx-deals
cat > ~/.config/olx-deals/olx.env <<'ENV'
PUBLIC_BASE_URL=https://olx.yourdomain.com
NTFY_URL=https://ntfy.sh/pick-something-unguessable
GEMINI_API_KEY=
ENV
chmod 600 ~/.config/olx-deals/olx.env

cp deploy/quadlet/olx-deals.container ~/.config/containers/systemd/
cp deploy/quadlet/olx-sync.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user start olx-deals.service
systemctl --user enable --now olx-sync.timer
```

The dashboard listens on `127.0.0.1:8092`; put a reverse proxy in front of it
if you want it reachable from elsewhere.

Everything mutable lives in the named volume `olx-deals-data`: `olxdeals.db`,
`searches.yaml` (the Manage tab writes to it) and `vapid_key.pem`. **The VAPID
key must survive rebuilds** — regenerating it silently invalidates every push
subscription already granted, with no error anywhere.

### Device & Invite Authentication

Access is managed through single-use device invites (via the shared
`pwa-invite-console` or the `./admin.sh` CLI):

* No usernames or passwords. Each invite binds exactly one device token in an
  `HttpOnly` cookie.
* Manage invites and devices via the tailnet-only `pwa-invite-console` PWA or
  via `./admin.sh invite "My Phone"`.
* The env file is read by *rootless podman itself*, as your user — so it must
  be owned by you, not root. Root ownership just makes it unreadable.
* `NTFY_URL` lives in the same env file — see [Notifications](#notifications).

### Moving an existing install

`deploy/import-data.sh` loads a previous dataset into the volume:

```bash
tar czf olx-data.tgz olxdeals.db searches.yaml vapid_key.pem   # on the old host
./deploy/import-data.sh <dir-with-those-files>                 # on the new one
```

It stops the service, copies through `podman cp` so ownership lands right, and
starts it again. Bring `vapid_key.pem` — see above.

## Notifications

Two channels, carrying different things:

* **Web push** → new deals, to the PWA. This is the product.
* **ntfy** → operations only. Sync failures, and the recovery that closes them.
  A healthy sync says nothing.

The sync runs hourly, so an unconditional status message meant 24 notifications
a day, of which roughly 24 said everything was fine. That trains you to swipe
the topic away, which is precisely when a real failure goes unread.

| Variable | Default | Effect |
| --- | --- | --- |
| `NTFY_URL` | `https://ntfy.sh/monitoring` | Where operational alerts go. **Change it** — the default is a public topic anyone can read and post to. |
| `NTFY_REPEAT_HOURS` | `6` | While a failure persists, re-notify at most this often. A problem lasting all day should not go quiet after its first mention. |
| `NTFY_HEARTBEAT_HOURS` | `0` (off) | Set to e.g. `24` for one liveness ping a day. Worth considering: with problems-only reporting, silence cannot distinguish "healthy" from "the timer stopped firing". |

State lives in the `meta` table (`ntfy_state`, `ntfy_last_fail`,
`ntfy_last_beat`), so a failure and its recovery are detected as transitions
across separate sync runs rather than recomputed from scratch each time.

## Layout

```
olxdeals/
  fetcher.py     OLX API client (paginated, polite) + SearchSpec
  store.py       SQLite persistence, diffing, price history, exclusions
  scorer.py      currency normalise, junk filter, deal/suspicious scoring
  discover.py    find category ids & model keys from real listings
  config.py      read/write searches.yaml
  dashboard.py   zero-dependency web UI (Deals / Drops / Manage)
  analyzer.py    optional per-listing LLM verdict (Gemini Flash 3.8)
  push.py        self-served VAPID web push
run.py           one sync cycle (used by the timer)
searches.yaml    your tracked searches
deploy/          Containerfile, quadlet units, data import
```

## Notes

- The `EUR→RON` rate is a constant in `scorer.py` — update it occasionally.
- The local `olxdeals.db` is git-ignored; it's rebuilt by running the sync.
- The ✦ analyze button calls Gemini Flash 3.8 and costs real money per listing —
  fractions of a cent per listing. `llm_analysis.cost_usd` records what each
  one cost. Leave `GEMINI_API_KEY` empty and the rest of the app works
  unchanged; only that button fails.

## Licence

[GNU GPL v3](LICENSE).
