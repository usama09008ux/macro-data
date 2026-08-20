"""
fetch_news.py
-------------
Sab feeds se khabrein le kar ek pack banata hai.

Kaam ki tarteeb:
  1. Har feed kholo, freshness check karo (apne cadence se)
  2. Sirf pichhle 24 ghante ki items lo
  3. Keywords se chaan lo (central bank + CME is se mustasna)
  4. Ek jaisi khabrein hata do
  5. Currency ka tag lagao
  6. HTML ka kachra utaaro
  7. output/news.md aur output/news.json likho

Chalane ka tareeqa:  python fetch_news.py
"""

import calendar
import difflib
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests
import yaml

CONFIG_FILE = "config.yaml"
OUT_DIR = "output"

PKT = timezone(timedelta(hours=5))          # Pakistan — DST nahi

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

FEED_GROUPS = ["feeds_tier1", "feeds_cme", "feeds_centralbank", "feeds_support"]

# Pack mein tag is tarteeb se aayenge
# Currency ke tag pehle, "rates" aur "risk" aakhir mein — warna
# ECB ki khabar RATES mein chali jati hai, EUR mein nahi.
TAG_ORDER = ["gold", "usd", "eur", "gbp", "jpy", "chf",
             "cad", "aud", "nzd", "oil", "rates", "risk"]


# ----------------------------------------------------------
# Chhote helpers
# ----------------------------------------------------------

def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def clean_html(raw):
    """HTML tags, styles aur zyada spaces utaar deta hai."""
    if not raw:
        return ""
    txt = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    txt = re.sub(r"(?s)<[^>]+>", " ", txt)
    txt = (txt.replace("&nbsp;", " ").replace("&amp;", "&")
              .replace("&lt;", "<").replace("&gt;", ">")
              .replace("&#39;", "'").replace("&quot;", '"')
              .replace("\u2019", "'").replace("\u2018", "'")
              .replace("\u201c", '"').replace("\u201d", '"'))
    txt = re.sub(r"\s+", " ", txt)
    txt = re.sub(r"\s+([.,;:!?%)])", r"\1", txt)   # " ." -> "."
    txt = re.sub(r"([(])\s+", r"\1", txt)
    return txt.strip()


def snip(text, limit):
    """Lafz ke beech se nahi kaat-ta. Jumle par kaatne ki koshish."""
    if not text or len(text) <= limit:
        return text or ""
    cut = text[:limit]
    dot = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if dot > limit * 0.5:
        return cut[:dot + 1]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 0 else cut).rstrip(" ,;:-") + " ..."


def norm_title(t):
    """Dedupe ke liye title ko saada karta hai."""
    return re.sub(r"[^a-z0-9 ]", "", (t or "").lower()).strip()


def item_age_days(entry, now_ts):
    for key in ("published_parsed", "updated_parsed"):
        p = entry.get(key)
        if p:
            try:
                return (now_ts - calendar.timegm(p)) / 86400.0
            except Exception:
                continue
    return None


def item_dt_pkt(entry):
    for key in ("published_parsed", "updated_parsed"):
        p = entry.get(key)
        if p:
            try:
                ts = calendar.timegm(p)
                return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(PKT)
            except Exception:
                continue
    return None


def find_tags(text, keywords):
    """Dekhta hai konse currency/asset ke keywords mile."""
    low = " " + (text or "").lower() + " "
    hits = []
    for tag, words in keywords.items():
        for w in words:
            if w.lower() in low:
                hits.append(tag)
                break
    return hits


# ----------------------------------------------------------
# Ek feed uthana
# ----------------------------------------------------------

def fetch_feed(feed, cfg, now_ts):
    """
    Ek feed se items uthata hai.
    Wapas: (status_dict, items_list)
    """
    name = feed["name"]
    url = feed["url"]
    cadence = feed.get("cadence_days", 1)
    warn_mult = cfg["settings"].get("warn_multiplier", 1.5)
    stale_mult = cfg["settings"].get("stale_multiplier", 3)
    timeout = cfg["settings"].get("request_timeout", 20)

    status = {"name": name, "status": "", "items": 0,
              "newest_age_days": None, "note": ""}

    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
    except Exception as e:
        status["status"] = "FAIL"
        status["note"] = type(e).__name__
        return status, []

    if r.status_code != 200:
        status["status"] = "FAIL"
        status["note"] = f"HTTP {r.status_code}"
        return status, []

    try:
        parsed = feedparser.parse(io.BytesIO(r.content))
    except Exception as e:
        status["status"] = "FAIL"
        status["note"] = f"parse: {type(e).__name__}"
        return status, []

    entries = parsed.get("entries", [])
    if not entries:
        status["status"] = "FAIL"
        status["note"] = "koi item nahi"
        return status, []

    ages = [a for a in (item_age_days(e, now_ts) for e in entries) if a is not None]
    if not ages:
        status["status"] = "WARN"
        status["note"] = "tareekh nahi mili"
        newest = None
    else:
        newest = min(ages)
        status["newest_age_days"] = round(newest, 1)
        if newest > cadence * stale_mult:
            status["status"] = "STALE"
            status["note"] = f"cadence {cadence}d, magar {newest:.0f}d purana"
            return status, []          # STALE feed ki items nahi lenge
        elif newest > cadence * warn_mult:
            status["status"] = "WARN"
            status["note"] = "thora sust"
        else:
            status["status"] = "OK"

    # ---- items nikalo ----
    max_age_h = cfg["settings"].get("max_age_hours", 24)
    max_chars = cfg["cleanup"].get("max_chars_per_item", 1200)

    out = []
    for e in entries:
        age = item_age_days(e, now_ts)
        if age is None:
            continue                    # tareekh ke baghair kuch nahi lete
        if age * 24 > max_age_h:
            continue

        title = clean_html(e.get("title", ""))
        if not title:
            continue

        body = e.get("summary", "") or ""
        if not body and e.get("content"):
            try:
                body = e["content"][0].get("value", "")
            except Exception:
                body = ""
        body = snip(clean_html(body), max_chars)

        dt = item_dt_pkt(e)
        out.append({
            "title": title,
            "body": body,
            "link": e.get("link", ""),
            "source": name,
            "group": feed["_group"],
            "weight": feed.get("weight", 5),
            "feed_tag": feed.get("tag"),
            "force_keywords": feed.get("keyword_filter", False),
            "age_hours": round(age * 24, 1),
            "when_pkt": dt.strftime("%d %b %H:%M") if dt else "",
            "sort_ts": calendar.timegm(
                e.get("published_parsed") or e.get("updated_parsed")),
        })

    status["items"] = len(out)
    return status, out


# ----------------------------------------------------------
# Chaan-boor
# ----------------------------------------------------------

def filter_and_tag(items, cfg):
    keywords = cfg["keywords"]
    exempt = set(cfg.get("keyword_exempt_groups", []))

    kept = []
    for it in items:
        text = it["title"] + " " + it["body"]
        tags = find_tags(text, keywords)

        # Feed ka apna tag (CME wale feeds ka)
        if it.get("feed_tag") and it["feed_tag"] not in tags:
            tags.insert(0, it["feed_tag"])

        # Kuch feeds group mein to mustasna hain, magar mila jula
        # mawaad dete hain (jaise CME ka daily commentary — us mein
        # cattle aur soybeans bhi aate hain). Un par filter lagta hai.
        is_exempt = it["group"] in exempt and not it.get("force_keywords")

        if is_exempt:
            # Central bank aur CME — har cheez le lo
            if not tags:
                tags = ["rates"]
        else:
            if not tags:
                continue                # koi keyword nahi mila -> chhod do

        it["tags"] = tags
        kept.append(it)
    return kept


def dedupe(items, cfg):
    """Ek jaisi khabrein hata deta hai. Zyada weight wali rehti hai."""
    thresh = cfg["cleanup"].get("dedupe_title_similarity", 0.85)
    items = sorted(items, key=lambda x: (-x["weight"], -x["sort_ts"]))

    kept, seen = [], []
    for it in items:
        n = norm_title(it["title"])
        if not n:
            continue
        dup = False
        for s in seen:
            if difflib.SequenceMatcher(None, n, s).ratio() >= thresh:
                dup = True
                break
        if not dup:
            seen.append(n)
            kept.append(it)
    return kept


# ----------------------------------------------------------
# Pack likhna
# ----------------------------------------------------------

def build_markdown(items, statuses, cfg, started):
    L = []
    now_pkt = started.astimezone(PKT)

    L.append("# News Pack")
    L.append("")
    L.append(f"- Banaya gaya: **{now_pkt.strftime('%d %b %Y, %H:%M')} PKT** "
             f"({started.strftime('%H:%M')} UTC)")
    L.append(f"- Khabrein: **{len(items)}**")
    ok = sum(1 for s in statuses if s["status"] == "OK")
    L.append(f"- Feeds: {ok}/{len(statuses)} OK")
    L.append("")

    # ---- Central bank / official pehle ----
    official = [i for i in items
                if i["group"] in ("feeds_centralbank", "feeds_cme")]
    rest = [i for i in items
            if i["group"] not in ("feeds_centralbank", "feeds_cme")]

    if official:
        L.append("---")
        L.append("")
        L.append("## Sarkari / Exchange")
        L.append("")
        for it in sorted(official, key=lambda x: -x["sort_ts"]):
            L.append(f"**{it['title']}**")
            L.append(f"`{it['when_pkt']} PKT` · {it['source']}")
            if it["body"]:
                L.append("")
                L.append(snip(it["body"], 600))
            L.append("")

    # ---- Baqi, tag ke hisaab se ----
    if rest:
        L.append("---")
        L.append("")
        L.append("## Khabrein")
        L.append("")
        used = set()

        # Jo khabar 4 ya zyada cheezon ko chhoo rahi ho, wo kisi ek
        # currency ki nahi hoti — wo session wrap hoti hai. Use alag
        # rakho, warna wo GOLD ya USD ka section kha jati hai.
        broad = [i for i in rest if len(i["tags"]) >= 4]
        if broad:
            L.append("### MARKET WRAP")
            L.append("")
            for it in sorted(broad, key=lambda x: -x["sort_ts"]):
                used.add(id(it))
                L.append(f"**{it['title']}**")
                L.append(f"`{it['when_pkt']} PKT` · {it['source']}")
                if it["body"]:
                    L.append("")
                    L.append(snip(it["body"], 450))
                L.append("")
        for tag in TAG_ORDER:
            group = [i for i in rest
                     if tag in i["tags"] and id(i) not in used]
            if not group:
                continue
            L.append(f"### {tag.upper()}")
            L.append("")
            for it in sorted(group, key=lambda x: -x["sort_ts"]):
                used.add(id(it))
                L.append(f"**{it['title']}**")
                L.append(f"`{it['when_pkt']} PKT` · {it['source']}")
                if it["body"]:
                    L.append("")
                    L.append(snip(it["body"], 450))
                L.append("")

    # ---- Data quality ----
    L.append("---")
    L.append("")
    L.append("## Data quality")
    L.append("")
    L.append("| Feed | Status | Items | Sab se nayi (din) | Note |")
    L.append("|---|---|---|---|---|")
    for s in statuses:
        age = s["newest_age_days"]
        L.append(f"| {s['name']} | {s['status']} | {s['items']} | "
                 f"{age if age is not None else '-'} | {s['note']} |")

    bad = [s for s in statuses if s["status"] in ("FAIL", "STALE")]
    if bad:
        L.append("")
        L.append("**Jo feeds nahi aaye:**")
        for s in bad:
            L.append(f"- {s['name']} — {s['status']}, {s['note']}")

    return "\n".join(L) + "\n"


# ----------------------------------------------------------
# Main
# ----------------------------------------------------------

def main():
    cfg = load_config()
    started = datetime.now(timezone.utc)
    now_ts = time.time()

    os.makedirs(OUT_DIR, exist_ok=True)

    print("=" * 70)
    print("FETCH NEWS —", started.strftime("%Y-%m-%d %H:%M"), "UTC")
    print("=" * 70)

    all_items, statuses = [], []

    for group in FEED_GROUPS:
        for feed in (cfg.get(group) or []):
            feed["_group"] = group
            st, items = fetch_feed(feed, cfg, now_ts)
            statuses.append(st)
            all_items.extend(items)
            print(f"  {st['status']:<6} {feed['name']:<22} "
                  f"{st['items']:>3} items   {st['note']}")
            time.sleep(0.4)

    print()
    print(f"Kul uthai gayi: {len(all_items)}")

    items = filter_and_tag(all_items, cfg)
    print(f"Keywords ke baad: {len(items)}")

    items = dedupe(items, cfg)
    print(f"Dedupe ke baad:   {len(items)}")

    cap = cfg["settings"].get("max_items_in_pack", 25)
    # Sarkari/CME wali kabhi nahi kategi
    official = [i for i in items
                if i["group"] in ("feeds_centralbank", "feeds_cme")]
    rest = [i for i in items
            if i["group"] not in ("feeds_centralbank", "feeds_cme")]
    rest = sorted(rest, key=lambda x: (-x["weight"], -x["sort_ts"]))[:cap]
    items = official + rest
    print(f"Pack mein:        {len(items)}  "
          f"({len(official)} sarkari + {len(rest)} baqi)")

    md = build_markdown(items, statuses, cfg, started)
    with open(os.path.join(OUT_DIR, "news.md"), "w", encoding="utf-8") as f:
        f.write(md)

    payload = {
        "generated_utc": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_pkt": started.astimezone(PKT).strftime("%Y-%m-%d %H:%M"),
        "item_count": len(items),
        "feeds": statuses,
        "items": [{k: v for k, v in i.items() if k != "sort_ts"}
                  for i in items],
    }
    with open(os.path.join(OUT_DIR, "news.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    with open(os.path.join(OUT_DIR, "run_log.txt"), "a", encoding="utf-8") as f:
        ok = sum(1 for s in statuses if s["status"] == "OK")
        f.write(f"{started.strftime('%Y-%m-%d %H:%M')}Z  "
                f"feeds_ok={ok}/{len(statuses)}  items={len(items)}\n")

    print()
    print("Likh diya: output/news.md, output/news.json, output/run_log.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
