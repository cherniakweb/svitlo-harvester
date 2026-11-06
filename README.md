# svitlo-harvester

Pulls the DTEK KREM (Kyiv region) outage schedule for one queue (**3.1**) and pushes it to
[svitlo.stream](https://svitlo.stream) — a private power-status page for one apartment building in Brovary.
Runs on GitHub Actions every 10 minutes (`.github/workflows/harvest.yml`).

## How it works
1. Opens `https://www.dtek-krem.com.ua/ua/shutdowns` in headless Chromium (Playwright) — the site's anti-bot
   (Imperva) rejects plain HTTP clients.
2. Reads the embedded `DisconSchedule.fact` JSON: per day (unix ts of Kyiv midnight) → queue `GPV3.1` →
   hours `"1".."24"` (`"1"` = 00:00–01:00).
3. Converts to outage intervals and posts them with `X-Admin-Key`:
   `POST /schedule` (schedule JSON) and `POST /emergency` (`{"emergency": bool}`).

| DTEK value | meaning | result |
|---|---|---|
| `yes` | power on | — |
| `no` | off the whole hour | outage |
| `first` / `second` | off the first / second 30 min | outage (half hour) |
| `maybe`, `mfirst`, `msecond` | possible outage | `maybe` (informational) |

Emergency = the site's `m-attention__text` notice says "екстрені відключення … не діють" and not "скасовано".
The server merges days, so the harvester is stateless.

## Setup
- Repository secret `ADMIN_KEY` — the Worker's admin key.
- Actions → *harvest* → **Run workflow** to test; then it runs on schedule.

## Local
```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt && venv/bin/python -m playwright install chromium
SERVER_URL=https://svitlo.stream ADMIN_KEY=… PREFER_BROWSER=1 venv/bin/python dtek_harvester.py --dry-run
python3 -m unittest discover tests     # parser tests on real page data (06–07.11.2026)
```
