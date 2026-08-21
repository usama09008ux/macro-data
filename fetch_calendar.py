"""
fetch_calendar.py  —  Phase 4
-----------------------------
Do kaam karta hai:

  1. CALENDAR  — aage kya aa raha hai (ForexFactory ka muft feed)
  2. SURPRISE  — jo aa chuka, wo forecast se kitna hata

DO ALAG ZARIYE KYUN:
    ForexFactory ke weekly feed mein sirf forecast aur previous
    hota hai — "actual" hai hi nahi. Maine khud khol kar dekha.
    Magar actual pehle se news pack mein maujood hai, kyunke
    investinglive unwaan mein hi likh deta hai:
        "US initial jobless claims 206K vs 210K expected"
    To calendar aage ka batata hai, aur surprise peechay ka.

WAQT:
    Feed ka waqt GMT/UTC mein hai (Eastern nahi).
    Jaancha: "Unemployment Claims 12:30pm" = 8:30am ET = 12:30 UTC
             "FOMC Meeting Minutes 6:00pm"  = 2:00pm ET = 18:00 UTC
    Is liye PKT = feed ka waqt + 5 ghante.

RATE LIMIT — AHEM:
    ForexFactory hafte wali file par 5 minute mein sirf 2 download
    deta hai. Had paar ho to XML ki jagah "Request Denied" ka HTML
    aata hai. Is liye:
      * ye script din mein sirf chand baar chalti hai (har 5 min NAHI)
      * har download cache mein mehfooz hota hai
      * Request Denied aaye to purana cache istemal hota hai
"""

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests
import yaml

CONFIG_FILE = "config.yaml"
OUT_DIR = "output"
CACHE_DIR = os.path.join(OUT_DIR, "cache")

PKT = timezone(timedelta(hours=5))
DAY_START_HOUR = 3

FF_URLS = {
    "thisweek": "https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
    "nextweek": "https://nfs.faireconomy.media/ff_calendar_nextweek.xml",
}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
}

IMPACT_RANK = {"High": 3, "Medium": 2, "Low": 1, "Holiday": 0}

# Ye currencies aap trade karte hain — inhein alag se numaya karenge
MY_CCY = {"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"}


# ==========================================================
# Trading day — wahi hisaab jo baqi scripts mein hai
# ==========================================================

def to_pkt(dt_utc):
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(PKT)


def trading_day(dt_pkt):
    if dt_pkt.hour < DAY_START_HOUR:
        return (dt_pkt - timedelta(days=1)).date()
    return dt_pkt.date()


def day_window_pkt(day):
    start = datetime(day.year, day.month, day.day,
                     DAY_START_HOUR, 0, 0, tzinfo=PKT)
    return start, start + timedelta(days=1) - timedelta(seconds=1)


def day_dir(day):
    return os.path.join(OUT_DIR, day.isoformat())


# ==========================================================
# Feed lana — cache ke sath
# ==========================================================

def fetch_week(name, url, timeout=25):
    """
    Ek hafte ki file lata hai. Nakaam ho to cache se kaam chalata hai.
    Wapas: (xml_text, source) jahan source = live / cache / none
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, f"ff_{name}.xml")

    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        text = r.text

        # Had paar ho jaye to XML ki jagah HTML aata hai
        looks_xml = text.lstrip().startswith("<?xml") or "<weeklyevents" in text
        if r.status_code == 200 and looks_xml:
            with open(cache_path, "w", encoding="utf-8") as f:
                f.write(text)
            return text, "live"

        if "Request Denied" in text or not looks_xml:
            print(f"  {name}: had paar ho gayi (Request Denied) — cache dekh raha hoon")
        else:
            print(f"  {name}: HTTP {r.status_code} — cache dekh raha hoon")

    except Exception as e:
        print(f"  {name}: {type(e).__name__} — cache dekh raha hoon")

    if os.path.exists(cache_path):
        age_h = (time.time() - os.path.getmtime(cache_path)) / 3600
        with open(cache_path, "r", encoding="utf-8") as f:
            print(f"  {name}: cache istemal ho raha hai ({age_h:.1f} ghante purana)")
            return f.read(), "cache"

    return None, "none"


# ==========================================================
# Parsing
# ==========================================================

def parse_events(xml_text):
    """
    XML se events nikalta hai aur har ek ka waqt PKT mein badalta hai.
    """
    out = []
    if not xml_text:
        return out
    try:
        root = ET.fromstring(xml_text.encode("utf-8", errors="replace"))
    except Exception as e:
        print(f"  parse nakaam: {e}")
        return out

    for ev in root.findall("event"):
        def g(tag):
            el = ev.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""

        date_s, time_s = g("date"), g("time")
        if not date_s:
            continue

        # Waqt kabhi "All Day" / "Tentative" hota hai
        all_day = False
        dt_utc = None
        try:
            if re.match(r"^\d{1,2}:\d{2}(am|pm)$", time_s, re.I):
                dt_utc = datetime.strptime(f"{date_s} {time_s}",
                                           "%m-%d-%Y %I:%M%p")
                dt_utc = dt_utc.replace(tzinfo=timezone.utc)
            else:
                all_day = True
                dt_utc = datetime.strptime(date_s, "%m-%d-%Y")
                dt_utc = dt_utc.replace(hour=12, tzinfo=timezone.utc)
        except Exception:
            continue

        pkt = to_pkt(dt_utc)
        out.append({
            "title": g("title"),
            "ccy": g("country"),
            "impact": g("impact") or "Low",
            "forecast": g("forecast"),
            "previous": g("previous"),
            "url": g("url"),
            "all_day": all_day,
            "utc": dt_utc.strftime("%Y-%m-%dT%H:%MZ"),
            "pkt": pkt.strftime("%d %b %H:%M"),
            "pkt_iso": pkt.isoformat(),
            "trading_day": trading_day(pkt).isoformat(),
        })
    return out


# ==========================================================
# Surprise — news pack se actual nikalna
# ==========================================================

# "206K vs 210K expected" · "+0.6% vs -0.5% expected"
SURPRISE_RE = re.compile(
    r"([+-]?[\d][\d,]*\.?\d*)\s*([KMBT%]?)\s*"
    r"vs\.?\s*"
    r"([+-]?[\d][\d,]*\.?\d*)\s*([KMBT%]?)\s*"
    r"(?:expected|expectations|est\.|estimate|forecast|consensus)",
    re.I)

UNIT_MULT = {"": 1, "%": 1, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def to_num(val, unit):
    try:
        n = float(val.replace(",", ""))
    except ValueError:
        return None
    return n * UNIT_MULT.get(unit.upper(), 1)


def extract_surprises(day):
    """
    Us trading day ki mehfooz khabron mein se "X vs Y expected"
    wale numbers nikalta hai.
    """
    path = os.path.join(day_dir(day), "news.jsonl")
    if not os.path.exists(path):
        return []

    found, seen = [], set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue

            title = rec.get("title", "")
            m = SURPRISE_RE.search(title)
            if not m:
                continue

            actual = to_num(m.group(1), m.group(2))
            forecast = to_num(m.group(3), m.group(4))
            if actual is None or forecast is None:
                continue

            key = re.sub(r"[^a-z0-9]", "", title.lower())[:50]
            if key in seen:
                continue
            seen.add(key)

            diff = actual - forecast
            unit = (m.group(2) or m.group(4) or "").upper()

            # Khaam farq usi paimane mein jis mein number aaya hai.
            # Ye hamesha samajh mein aata hai.
            if unit == "%":
                raw_diff = f"{diff:+.2f}pp"
            elif unit in ("K", "M", "B", "T"):
                raw_diff = f"{diff / UNIT_MULT[unit]:+,.1f}{unit}"
            else:
                raw_diff = f"{diff:+,.2f}"

            # Rukh palat gaya? Yaani forecast manfi tha aur actual
            # musbat (ya ulta). Ye sab se ahem cheez hoti hai.
            flip = (forecast < 0 < actual) or (actual < 0 < forecast)

            # Feesad tabhi dikhao jab wo gumraah na kare.
            # "Canada PPI +0.6% vs -0.5%" par feesad +220% banta hai —
            # jo dekhne mein bara lagta hai magar asal baat rukh ka
            # palatna hai, 220% ka izafa nahi.
            pct = None
            if not flip and abs(forecast) > 1e-9:
                p_ = 100.0 * diff / abs(forecast)
                if abs(p_) < 500:
                    pct = round(p_, 1)

            found.append({
                "title": title,
                "actual_raw": f"{m.group(1)}{m.group(2)}",
                "forecast_raw": f"{m.group(3)}{m.group(4)}",
                "beat": diff > 0,
                "raw_diff": raw_diff,
                "sign_flip": flip,
                "diff_pct": pct,
                "when_pkt": rec.get("published_pkt", ""),
                "source": rec.get("source", ""),
                "tags": rec.get("tags", []),
            })
    return found


# ==========================================================
# Likhna
# ==========================================================

def build_markdown(day, events, surprises, now_pkt, sources):
    start, end = day_window_pkt(day)
    now_iso = now_pkt.isoformat()
    end_24 = (now_pkt + timedelta(hours=24)).isoformat()
    week_end = (now_pkt + timedelta(days=7)).isoformat()

    L = []
    L.append(f"# Calendar — Trading Day {day.strftime('%d %b %Y')}")
    L.append("")
    L.append(f"- Banaya gaya: **{now_pkt.strftime('%d %b %Y %H:%M')} PKT**")
    L.append(f"- Trading day: **{start.strftime('%d %b %H:%M')} "
             f"-> {end.strftime('%d %b %H:%M')} PKT**")
    L.append(f"- Feed: {', '.join(f'{k}={v}' for k, v in sources.items())}")
    L.append("")
    L.append("*Saara waqt Pakistani (PKT) mein hai. Feed GMT deta hai, "
             "script ne +5 kar diya hai.*")
    L.append("")

    today_str = now_pkt.strftime("%d %b")

    def row(e):
        imp = {"High": "**HIGH**", "Medium": "MED", "Low": "low",
               "Holiday": "chhutti"}.get(e["impact"], e["impact"])
        if e["all_day"]:
            t = f"{e['pkt'].rsplit(' ', 1)[0]} poora din"
        else:
            day_part, clock = e["pkt"].rsplit(" ", 1)
            # Aaj ka event ho to sirf ghanta; kal ka ho to tareekh bhi.
            # Warna "04:00" dekh kar lagta hai ke subah ka hai jo
            # guzar chuka, halanke wo AGLI subah ka hota hai.
            t = clock if day_part == today_str else f"**{day_part}** {clock}"
        return (f"| {t} | {e['ccy']} | {imp} | {e['title']} "
                f"| {e['forecast'] or '-'} | {e['previous'] or '-'} |")

    # ---- 1. Aane wale 24 ghante ----
    soon = [e for e in events if now_iso <= e["pkt_iso"] <= end_24]
    soon.sort(key=lambda e: e["pkt_iso"])

    L.append("---")
    L.append("")
    L.append("## Aane wale 24 ghante")
    L.append("")
    if soon:
        L.append("| Waqt PKT | Ccy | Impact | Event | Forecast | Previous |")
        L.append("|---|---|---|---|---|---|")
        for e in soon:
            L.append(row(e))
        L.append("")
        hi = [e for e in soon if e["impact"] == "High"]
        if hi:
            L.append("**NO-TRADE windows** — in se 30 minute pehle aur "
                     "30 minute baad haath rok kar rakhen:")
            L.append("")
            for e in hi:
                L.append(f"- `{e['pkt']}` **{e['ccy']} {e['title']}**")
            L.append("")
        else:
            L.append("*Agle 24 ghante mein koi HIGH impact event nahi.*")
            L.append("")
    else:
        L.append("*Agle 24 ghante mein kuch nahi.*")
        L.append("")

    # ---- 2. Hafte ka naqsha ----
    week = [e for e in events
            if end_24 < e["pkt_iso"] <= week_end
            and IMPACT_RANK.get(e["impact"], 1) >= 2]
    week.sort(key=lambda e: e["pkt_iso"])

    L.append("---")
    L.append("")
    L.append("## Agle 7 din — sirf High aur Medium")
    L.append("")
    if week:
        L.append("| Waqt PKT | Ccy | Impact | Event | Forecast | Previous |")
        L.append("|---|---|---|---|---|---|")
        for e in week:
            L.append(row(e))
    elif sources.get("nextweek") == "none":
        L.append("**Agle hafte ki file nahi mil saki.** Is liye ye hissa "
                 "adhoora hai — is hafte ka bacha hua data hi dikh raha "
                 "hai. Khaas kar Jumeraat/Jumma ko ye khali reh sakta hai.")
    else:
        L.append("*Koi High ya Medium event nahi.*")
    L.append("")

    # ---- 3. Surprise ----
    L.append("---")
    L.append("")
    L.append("## Aaj ke surprises — actual banaam forecast")
    L.append("")
    if surprises:
        L.append("*Ye numbers khabron ke unwaan se nikale gaye hain. "
                 "Market number par nahi, forecast se farq par chalta hai.*")
        L.append("")
        L.append("| Waqt PKT | Event | Actual | Forecast | Farq | Rukh |")
        L.append("|---|---|---|---|---|---|")
        for s in sorted(surprises, key=lambda x: x["when_pkt"], reverse=True):
            if s.get("sign_flip"):
                verdict = "**RUKH PALAT GAYA**"
            elif s["beat"]:
                verdict = "upar"
            else:
                verdict = "neeche"
            d = s.get("raw_diff", "-")
            if s["diff_pct"] is not None:
                d += f" ({s['diff_pct']:+.0f}%)"
            short = re.sub(r"\s*[+-]?[\d.,]+[KMBT%]?\s*vs.*$", "",
                           s["title"], flags=re.I).strip()
            L.append(f"| {s['when_pkt']} | {short[:44]} | "
                     f"{s['actual_raw']} | {s['forecast_raw']} | {d} | {verdict} |")
        L.append("")
    else:
        L.append("*Aaj abhi tak koi actual-vs-forecast number nahi mila.*")
        L.append("")

    return "\n".join(L) + "\n"


# ==========================================================
# Main
# ==========================================================

def main():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    now_utc = datetime.now(timezone.utc)
    now_pkt = to_pkt(now_utc)
    today_td = trading_day(now_pkt)

    print("=" * 62)
    print(f"FETCH CALENDAR   {now_pkt.strftime('%d %b %Y %H:%M')} PKT")
    print(f"Trading day: {today_td}")
    print("=" * 62)

    events, sources = [], {}

    for i, (name, url) in enumerate(FF_URLS.items()):
        # nextweek roz badalta nahi. Har run par dono lene se hum
        # ForexFactory ki had (5 min mein 2) par chipke rehte hain.
        # Is liye nextweek sirf tab laate hain jab uska cache 12
        # ghante se purana ho. Baqi waqt cache se kaam chalta hai.
        if name == "nextweek":
            cp = os.path.join(CACHE_DIR, "ff_nextweek.xml")
            if os.path.exists(cp):
                age_h = (time.time() - os.path.getmtime(cp)) / 3600
                if age_h < 12:
                    with open(cp, "r", encoding="utf-8") as f:
                        xml_text = f.read()
                    got = parse_events(xml_text)
                    events.extend(got)
                    sources[name] = "cache"
                    print(f"  {name:<10} cache  {len(got)} events "
                          f"({age_h:.1f}h purana — abhi lene ki zaroorat nahi)")
                    continue

        if i:
            time.sleep(3)
        xml_text, src = fetch_week(name, url)
        sources[name] = src
        got = parse_events(xml_text)
        events.extend(got)
        print(f"  {name:<10} {src:<6} {len(got)} events")

    # ek hi event do hafton mein aa sakta hai
    uniq, seen = [], set()
    for e in events:
        k = (e["utc"], e["ccy"], e["title"])
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    events = uniq

    surprises = extract_surprises(today_td)
    print(f"\n  Kul events: {len(events)}")
    print(f"  Surprises mile: {len(surprises)}")

    hi_soon = [e for e in events
               if e["impact"] == "High"
               and now_pkt.isoformat() <= e["pkt_iso"]
               <= (now_pkt + timedelta(hours=24)).isoformat()]
    if hi_soon:
        print("\n  Agle 24 ghante ke HIGH impact events:")
        for e in hi_soon:
            print(f"    {e['pkt']} PKT  {e['ccy']:<4} {e['title']}")

    d = day_dir(today_td)
    os.makedirs(d, exist_ok=True)

    with open(os.path.join(d, "calendar.md"), "w", encoding="utf-8") as f:
        f.write(build_markdown(today_td, events, surprises, now_pkt, sources))

    with open(os.path.join(d, "calendar.json"), "w", encoding="utf-8") as f:
        json.dump({
            "generated_pkt": now_pkt.strftime("%d %b %Y %H:%M"),
            "trading_day": today_td.isoformat(),
            "feed_sources": sources,
            "events": events,
            "surprises": surprises,
        }, f, ensure_ascii=False, indent=2)

    with open(os.path.join(OUT_DIR, "last_run_calendar.txt"), "w",
              encoding="utf-8") as f:
        f.write(f"calendar | TD {today_td.strftime('%d %b %Y')} | "
                f"{now_pkt.strftime('%d %b %Y %H:%M')} PKT | "
                f"{len(events)} events, {len(surprises)} surprises\n")

    print(f"\nLikh diya: {d}/calendar.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
