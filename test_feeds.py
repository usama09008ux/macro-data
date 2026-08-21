"""
test_feeds.py
-------------
Sab feeds ko test karta hai aur do sawal poochta hai:

  1. Feed khulta hai ya nahi?            -> HTTP check
  2. Sab se nayi khabar kitni purani?    -> freshness check

Sirf pehla sawal kafi nahi. Kuch feeds khulte hain magar
andar ka mawaad saal purana hota hai. Wo jaal hai.

Chalane ka tareeqa:  python test_feeds.py
"""

import calendar
import io
import sys
import time
from datetime import datetime, timezone

import feedparser
import requests
import yaml

# ----------------------------------------------------------
# Settings
# ----------------------------------------------------------

CONFIG_FILE = "config.yaml"
REPORT_FILE = "feed_test_report.md"

# Kuch sites python ka default User-Agent dekh kar rok deti hain.
# Is liye normal browser ka UA bhejte hain.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

TIMEOUT = 20

# Freshness ki hadd (dinon mein)
WARN_DAYS = 2
STALE_DAYS = 14

# Groups ki fehrist yahan LIKHI HUI NAHI hai.
#
# Wajah: pehle yahan haath se naam likhe the — feeds_data,
# feeds_tier2, feeds_cme_test. Phir config badli, group ke naam
# badal gaye, aur ye file chupchap kuch bhi test karna chhod
# baithi. Ab config se khud dhoondta hai, is liye aage naya
# group banayein to wo apne aap shaamil ho jayega.
SKIP_GROUPS = {"feeds_rejected"}


# ----------------------------------------------------------
# Helpers
# ----------------------------------------------------------

def load_config():
    """config.yaml parhta hai."""
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def entry_age_days(entry, now_ts):
    """
    Ek item ki umar dinon mein. Agar tareekh na mile to None.
    feedparser tareekh ko UTC struct_time mein deta hai.
    """
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                item_ts = calendar.timegm(parsed)
                return (now_ts - item_ts) / 86400.0
            except Exception:
                continue
    return None


def test_one_feed(name, url):
    """
    Ek feed test karta hai.
    Wapas deta hai: dict jis mein verdict aur tafseel ho.
    """
    result = {
        "name": name,
        "url": url,
        "status": "",
        "http": "",
        "items": 0,
        "age_days": None,
        "newest_title": "",
        "note": "",
    }

    # --- Sawal 1: khulta hai? ---
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        result["http"] = str(r.status_code)
    except requests.exceptions.Timeout:
        result["status"] = "FAIL"
        result["note"] = "timeout"
        return result
    except Exception as e:
        result["status"] = "FAIL"
        result["note"] = type(e).__name__
        return result

    if r.status_code != 200:
        result["status"] = "FAIL"
        result["note"] = f"HTTP {r.status_code}"
        return result

    # --- Parse ---
    try:
        parsed = feedparser.parse(io.BytesIO(r.content))
    except Exception as e:
        result["status"] = "FAIL"
        result["note"] = f"parse error: {type(e).__name__}"
        return result

    entries = parsed.get("entries", [])
    result["items"] = len(entries)

    if not entries:
        result["status"] = "FAIL"
        result["note"] = "koi item nahi (shayad RSS hi nahi hai)"
        return result

    # --- Sawal 2: kitna taza? ---
    now_ts = time.time()
    ages = []
    for e in entries:
        a = entry_age_days(e, now_ts)
        if a is not None:
            ages.append(a)

    if not ages:
        result["status"] = "WARN"
        result["note"] = "items hain magar tareekh nahi mili"
        result["newest_title"] = entries[0].get("title", "")[:70]
        return result

    newest = min(ages)
    result["age_days"] = round(newest, 1)

    # Sab se nayi item ka unwaan
    for e in entries:
        a = entry_age_days(e, now_ts)
        if a is not None and abs(a - newest) < 1e-9:
            result["newest_title"] = (e.get("title", "") or "")[:70]
            break

    if newest > STALE_DAYS:
        result["status"] = "STALE"
        result["note"] = "khulta hai magar mawaad purana — pack mein mat lo"
    elif newest > WARN_DAYS:
        result["status"] = "WARN"
        result["note"] = "thora sust"
    else:
        result["status"] = "OK"

    return result


# ----------------------------------------------------------
# Main
# ----------------------------------------------------------

def main():
    cfg = load_config()

    # config se saare feed groups khud dhoondo
    groups = [k for k in cfg
              if k.startswith("feeds_")
              and k not in SKIP_GROUPS
              and isinstance(cfg[k], list)]

    started = datetime.now(timezone.utc)
    print("=" * 78)
    print("FEED TEST")
    print("Waqt (UTC):", started.strftime("%Y-%m-%d %H:%M"))
    total = sum(len(cfg[g] or []) for g in groups)
    print(f"Groups: {len(groups)}   Feeds: {total}")
    print("=" * 78)

    all_results = []

    for group in groups:
        feeds = cfg.get(group) or []
        if not feeds:
            continue

        print()
        print("-" * 78)
        print(f"  {group}   ({len(feeds)} feeds)")
        print("-" * 78)
        print(f"{'STATUS':<7} {'NAME':<22} {'ITEMS':>5} {'UMAR(din)':>10}  NOTE")
        print("-" * 78)

        for feed in feeds:
            name = feed.get("name", "?")
            url = feed.get("url", "")

            res = test_one_feed(name, url)
            res["group"] = group
            all_results.append(res)

            age = res["age_days"]
            age_str = f"{age:.1f}" if age is not None else "-"
            note = res["note"] or ""

            print(f"{res['status']:<7} {name:<22} {res['items']:>5} {age_str:>10}  {note}")

            # Sites ko aaram do
            time.sleep(0.5)

    # ------------------------------------------------------
    # Khulasa
    # ------------------------------------------------------
    counts = {}
    for r in all_results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    print()
    print("=" * 78)
    print("KHULASA")
    print("=" * 78)
    for status in ("OK", "WARN", "STALE", "FAIL"):
        if status in counts:
            print(f"  {status:<7} {counts[status]}")
    print(f"  {'KUL':<7} {len(all_results)}")

    ok_names = [r["name"] for r in all_results if r["status"] == "OK"]
    bad = [r for r in all_results if r["status"] in ("STALE", "FAIL")]

    print()
    print("Istemal ke qabil:", ", ".join(ok_names) if ok_names else "(koi nahi)")
    if bad:
        print()
        print("Hatane wale:")
        for r in bad:
            print(f"  - {r['name']:<22} {r['status']:<6} {r['note']}")

    # ------------------------------------------------------
    # Report file
    # ------------------------------------------------------
    lines = []
    lines.append("# Feed Test Report")
    lines.append("")
    lines.append(f"Waqt (UTC): {started.strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append("| Group | Feed | Status | Items | Umar (din) | Sab se nayi khabar |")
    lines.append("|---|---|---|---|---|---|")
    for r in all_results:
        age = r["age_days"]
        age_str = f"{age:.1f}" if age is not None else "-"
        title = (r["newest_title"] or r["note"] or "").replace("|", "/")
        lines.append(
            f"| {r['group'].replace('feeds_','')} | {r['name']} | "
            f"{r['status']} | {r['items']} | {age_str} | {title} |"
        )
    lines.append("")
    lines.append("## Khulasa")
    lines.append("")
    for status in ("OK", "WARN", "STALE", "FAIL"):
        if status in counts:
            lines.append(f"- **{status}**: {counts[status]}")

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print()
    print(f"Report likh di gayi: {REPORT_FILE}")

    # Script hamesha kamyabi se khatam ho — kuch feeds fail hona
    # normal hai, ye khud script ki kharabi nahi.
    return 0


if __name__ == "__main__":
    sys.exit(main())
