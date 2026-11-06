#!/usr/bin/env python3
"""
DTEK KREM schedule harvester for one queue (default 3.1). Runs on GitHub Actions every 10 min.

Reads https://www.dtek-krem.com.ua/ua/shutdowns, extracts `DisconSchedule.fact` (today + tomorrow,
per queue, per hour: yes | no | first | second | maybe | mfirst | msecond) and the "emergency outages"
notice, then pushes changes to the Worker:
  POST {SERVER_URL}/schedule   (X-Admin-Key)  — schedule.json contract, see web/README.md
  POST {SERVER_URL}/emergency  (X-Admin-Key)  — {"emergency": bool}

Stdlib only. If the plain HTTP request is blocked (Incapsula), falls back to Playwright when installed.

  python3 dtek_harvester.py --dry-run     # fetch + parse + print, no POST
  python3 dtek_harvester.py               # normal run (posts only changes, state in state.json)
  python3 dtek_harvester.py --force       # post regardless of state (GitHub Actions: no state between runs)
  python3 dtek_harvester.py --file page.html --dry-run
"""
import argparse, gzip, json, os, re, sys, urllib.error, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
TZ = ZoneInfo("Europe/Kyiv")
SOURCE_NAME = "ДТЕК Київські регіональні електромережі"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


# ---------------- config ----------------
def load_env():
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    return {
        "url": os.environ.get("DTEK_URL", "https://www.dtek-krem.com.ua/ua/shutdowns"),
        "group": os.environ.get("GROUP", "3.1"),
        "server": os.environ.get("SERVER_URL", "").rstrip("/"),
        "admin_key": os.environ.get("ADMIN_KEY", ""),
        "keep_days": int(os.environ.get("KEEP_DAYS", "7")),
        "state": Path(os.environ.get("STATE_FILE", HERE / "state.json")),
    }


# ---------------- fetch ----------------
def fetch_plain(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "uk-UA,uk;q=0.9", "Accept-Encoding": "gzip",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", "replace")


def fetch_browser(url):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("blocked by the site and Playwright is not installed "
                           "(pip install playwright && python -m playwright install chromium)")
    with sync_playwright() as p:
        try:   # full Chromium in new headless mode is harder for anti-bot to spot than the headless shell
            b = p.chromium.launch(channel="chromium")
        except Exception:
            b = p.chromium.launch()
        pg = b.new_page(user_agent=UA, locale="uk-UA")
        pg.goto(url, wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_function("typeof DisconSchedule !== 'undefined' && DisconSchedule.fact", timeout=30000)
        html = pg.content()
        b.close()
        return html


def fetch(url):
    """Plain HTTP first (fast, no deps); browser only if the schedule isn't in the response.
    PREFER_BROWSER=1 skips the plain attempt (the site blocks it as of 11.2026)."""
    if os.environ.get("PREFER_BROWSER") == "1":
        return fetch_browser(url), "browser"
    try:
        html = fetch_plain(url)
        if "DisconSchedule.fact" in html:
            return html, "http"
        why = "no DisconSchedule.fact in response (anti-bot page?)"
    except urllib.error.HTTPError as e:
        why = f"HTTP {e.code}"
    print(f"plain fetch failed: {why} → trying headless browser", file=sys.stderr)
    return fetch_browser(url), "browser"


# ---------------- parse ----------------
OFF = {"no": [(0, 60)], "first": [(0, 30)], "second": [(30, 60)]}
MAYBE = {"maybe": [(0, 60)], "mfirst": [(0, 30)], "msecond": [(30, 60)]}


def extract_fact(html):
    i = html.find("DisconSchedule.fact")
    if i < 0:
        raise ValueError("DisconSchedule.fact not found")
    j = html.index("{", i)
    obj, _ = json.JSONDecoder().raw_decode(html, j)
    return obj


def hm(m):
    return "24:00" if m >= 1440 else f"{m // 60:02d}:{m % 60:02d}"


def merge(slots):
    """[(start_min, end_min)] → merged, sorted [["HH:MM","HH:MM"]]."""
    out = []
    for a, b in sorted(slots):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [[hm(a), hm(b)] for a, b in out]


def day_intervals(hours, table):
    """hours = {"1": "no", ..., "24": "yes"}; key "1" is 00:00–01:00."""
    slots = []
    for k, v in hours.items():
        h = int(k) - 1
        for a, b in table.get(v, []):
            slots.append((h * 60 + a, h * 60 + b))
    return merge(slots)


def parse(html, group):
    """→ {days: {date: intervals}, maybe: {date: intervals}, updated_at, unknown_values}"""
    fact = extract_fact(html)
    gkey = f"GPV{group}"
    days, maybe, unknown = {}, {}, set()
    for ts, groups in fact.get("data", {}).items():
        date = datetime.fromtimestamp(int(ts), TZ).strftime("%Y-%m-%d")
        hours = groups.get(gkey)
        if not isinstance(hours, dict) or len(hours) != 24:
            days[date] = None                     # DTEK has not published this day for the queue
            continue
        unknown |= {v for v in hours.values() if v not in OFF and v not in MAYBE and v != "yes"}
        days[date] = day_intervals(hours, OFF)
        mb = day_intervals(hours, MAYBE)
        if mb:
            maybe[date] = mb
    if not days:
        raise ValueError(f"no days for {gkey} in DisconSchedule.fact")
    upd = None
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4}) (\d{2}):(\d{2})", str(fact.get("update", "")))
    if m:
        d, mo, y, H, M = map(int, m.groups())
        upd = datetime(y, mo, d, H, M, tzinfo=TZ).isoformat()
    return {"days": days, "maybe": maybe, "updated_at": upd, "unknown_values": sorted(unknown)}


def notice_text(html):
    m = re.search(r'class="m-attention__text"[^>]*>(.*?)</div>', html, re.S)
    if not m:
        return ""
    t = re.sub(r"<[^>]+>", " ", m.group(1))
    return re.sub(r"\s+", " ", t).strip()


def is_emergency(text):
    t = text.lower()
    active = re.search(r"екстрен\w*\s+(?:знеструмлен|відключен)", t) and "не діють" in t
    cancelled = re.search(r"скасова|відмінен|завершен|припинен", t)
    return bool(active and not cancelled)


# ---------------- output ----------------
def build_doc(parsed, cfg, prev_doc):
    """Merge fresh days into previously sent ones (keep_days back), so yesterday stays visible."""
    days = dict((prev_doc or {}).get("days", {}))
    days.update(parsed["days"])
    cutoff = (datetime.now(TZ) - timedelta(days=cfg["keep_days"])).strftime("%Y-%m-%d")
    days = {k: v for k, v in sorted(days.items()) if k >= cutoff}
    return {
        "group": cfg["group"],
        "source": SOURCE_NAME,
        "source_url": cfg["url"],
        "updated_at": parsed["updated_at"] or datetime.now(TZ).isoformat(timespec="seconds"),
        "days": days,
        "maybe": parsed["maybe"],
    }


def post(cfg, path, payload):
    req = urllib.request.Request(cfg["server"] + path, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Admin-Key": cfg["admin_key"],
                                          "User-Agent": "svitlo-harvester/1.0 (+https://svitlo.stream)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print, don't POST")
    ap.add_argument("--file", help="parse a saved HTML file instead of fetching")
    ap.add_argument("--force", action="store_true", help="POST even if nothing changed")
    a = ap.parse_args()
    cfg = load_env()

    if a.file:
        html, via = Path(a.file).read_text(encoding="utf-8"), "file"
    else:
        html, via = fetch(cfg["url"])
    parsed = parse(html, cfg["group"])
    notice = notice_text(html)
    emergency = is_emergency(notice)

    state = json.loads(cfg["state"].read_text()) if cfg["state"].exists() else {}
    doc = build_doc(parsed, cfg, state.get("doc"))
    now = datetime.now(TZ).strftime("%d.%m %H:%M")
    summary = "; ".join(f"{k}: " + (", ".join(f"{x}–{y}" for x, y in v) if v else ("немає" if v == [] else "не опубліковано"))
                        for k, v in parsed["days"].items())
    print(f"[{now}] via {via} · {cfg['group']} · {summary} · emergency={emergency}")
    if parsed["unknown_values"]:
        print(f"  ⚠ unknown values in DTEK data: {parsed['unknown_values']} — treated as power ON", file=sys.stderr)
    if notice:
        print(f"  notice: {notice[:160]}")

    if a.dry_run:
        print(json.dumps(doc, ensure_ascii=False, indent=1))
        return 0
    if not (cfg["server"] and cfg["admin_key"]):
        print("SERVER_URL / ADMIN_KEY not set (harvester/.env)", file=sys.stderr)
        return 2

    sent = []
    # compare without updated_at: DTEK bumps it on every regeneration even when nothing changed
    strip = lambda d: {k: v for k, v in (d or {}).items() if k != "updated_at"}
    if a.force or strip(doc) != strip(state.get("doc")):
        post(cfg, "/schedule", doc); sent.append("schedule")
        state["doc"] = doc
    if a.force or emergency != state.get("emergency"):
        post(cfg, "/emergency", {"emergency": emergency}); sent.append(f"emergency={emergency}")
        state["emergency"] = emergency
    cfg["state"].write_text(json.dumps(state, ensure_ascii=False, indent=1))
    print("  sent: " + (", ".join(sent) if sent else "nothing changed"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # one line in the log; launchd will retry next interval
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
