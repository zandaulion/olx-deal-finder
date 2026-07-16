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

## Always-on (systemd user services)

Reference unit files are in [`deploy/systemd/`](deploy/systemd). They run an **hourly sync**
and keep the **dashboard** up across logout/reboot. Adjust the paths to your checkout, then:

```bash
cp deploy/systemd/*.service deploy/systemd/*.timer ~/.config/systemd/user/
loginctl enable-linger "$USER"          # run without being logged in
systemctl --user daemon-reload
systemctl --user enable --now olx-dashboard.service olx-sync.timer
```

## Layout

```
olxdeals/
  fetcher.py     OLX API client (paginated, polite) + SearchSpec
  store.py       SQLite persistence, diffing, price history, exclusions
  scorer.py      currency normalise, junk filter, deal/suspicious scoring
  discover.py    find category ids & model keys from real listings
  config.py      read/write searches.yaml
  dashboard.py   zero-dependency web UI (Deals / Drops / Manage)
run.py           one sync cycle (used by the timer)
searches.yaml    your tracked searches
```

## Notes

- The `EUR→RON` rate is a constant in `scorer.py` — update it occasionally.
- The local `olxdeals.db` is git-ignored; it's rebuilt by running the sync.
