"""
fetch_fred.py  —  Phase 2
-------------------------
FRED se macro data uthata hai — yields, real yields, breakevens,
inflation, labour, growth.

Din mein do baar chalta hai (news wali script har 5 minute chalti
hai, magar FRED ka data itni jaldi nahi badalta).

HAR SERIES PAR TEEN HISAAB — akela number bekaar hai:
    1. Aaj ki sath (level)
    2. Tabdeeli   (daily: 1d/5d/20d, monthly: MoM/YoY)
    3. Muqam      (apni 3-saal ki history mein percentile)

Ye wahi "law of comparison" hai jo Excel ki COMPARE sheet mein
chal raha hai.

OUTPUT:
    output/<trading-day>/macro.md
    output/<trading-day>/macro.json

News ke bar-aks ye jama nahi hota — har run par naya likha jata
hai, kyunke ye ek snapshot hai, khabron ka dhair nahi.
"""

import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
import yaml

CONFIG_FILE = "config.yaml"
OUT_DIR = "output"
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"

PKT = timezone(timedelta(hours=5))
DAY_START_HOUR = 3


# ==========================================================
# Trading day — bilkul wahi hisaab jo fetch_news.py mein hai
# ==========================================================

def to_pkt(dt_utc):
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(PKT)


def trading_day(dt_pkt):
    if dt_pkt.hour < DAY_START_HOUR:
        return (dt_pkt - timedelta(days=1)).date()
    return dt_pkt.date()


def day_dir(day):
    return os.path.join(OUT_DIR, day.isoformat())


# ==========================================================
# FRED
# ==========================================================

def get_series(series_id, api_key, start_date, timeout):
    """
    Ek series ki saari observations uthata hai.
    Wapas: [(date, value), ...] purani se nayi tarteeb mein.
    FRED gum-shuda qeemat "." se zahir karta hai — wo hata dete hain.
    """
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": start_date,
        "sort_order": "asc",
    }
    r = requests.get(FRED_URL, params=params, timeout=timeout)

    if r.status_code == 400:
        raise RuntimeError("series ka naam ghalat ho sakta hai (HTTP 400)")
    if r.status_code in (401, 403):
        raise RuntimeError("API key qabool nahi hui — Secret check karein")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")

    obs = r.json().get("observations", [])
    out = []
    for o in obs:
        v = o.get("value", ".")
        if v in (".", "", None):
            continue
        try:
            out.append((o["date"], float(v)))
        except (ValueError, TypeError):
            continue
    return out


def pct_rank(values, x):
    """3-saal ki history mein aaj ka muqam, 0 se 100 tak."""
    if len(values) < 20:
        return None
    below = sum(1 for v in values if v < x)
    return round(100.0 * below / len(values))


def analyse(obs, kind):
    """
    Ek series par teenon hisaab lagata hai.
    """
    if not obs:
        return None

    dates = [d for d, _ in obs]
    vals = [v for _, v in obs]
    latest = vals[-1]

    res = {
        "latest": latest,
        "latest_date": dates[-1],
        "n_obs": len(vals),
        "changes": {},
        "percentile": pct_rank(vals, latest),
    }

    def back(n):
        """n qadam peechay ki qeemat."""
        return vals[-1 - n] if len(vals) > n else None

    if kind == "daily":
        for label, n in (("1d", 1), ("5d", 5), ("20d", 20)):
            prev = back(n)
            if prev is not None:
                res["changes"][label] = round(latest - prev, 4)

    elif kind == "weekly":
        for label, n in (("1w", 1), ("4w", 4)):
            prev = back(n)
            if prev is not None:
                res["changes"][label] = round(latest - prev, 2)

    elif kind == "monthly":
        prev = back(1)
        if prev not in (None, 0):
            res["changes"]["MoM %"] = round(100.0 * (latest - prev) / abs(prev), 2)
        yr = back(12)
        if yr not in (None, 0):
            res["changes"]["YoY %"] = round(100.0 * (latest - yr) / abs(yr), 2)

    elif kind == "quarterly":
        prev = back(1)
        if prev not in (None, 0):
            res["changes"]["QoQ %"] = round(100.0 * (latest - prev) / abs(prev), 2)
        yr = back(4)
        if yr not in (None, 0):
            res["changes"]["YoY %"] = round(100.0 * (latest - yr) / abs(yr), 2)

    return res


# ==========================================================
# Likhna
# ==========================================================

def fmt(v, unit=""):
    if v is None:
        return "-"
    if abs(v) >= 1000:
        return f"{v:,.0f}{unit}"
    return f"{v:,.2f}{unit}"


def arrow(v):
    if v is None:
        return ""
    if v > 0:
        return f"+{v}"
    return str(v)


def build_markdown(day, groups, results, now_pkt, failures):
    L = []
    L.append(f"# Macro Pack — Trading Day {day.strftime('%d %b %Y')}")
    L.append("")
    L.append(f"- Banaya gaya: **{now_pkt.strftime('%d %b %Y %H:%M')} PKT**")
    ok = len(results)
    L.append(f"- Series: {ok}/{ok + len(failures)} mili")
    L.append("")
    L.append("*Har number ke sath uski tabdeeli aur uska muqam bhi hai. "
             "Percentile = pichhle 3 saal mein aaj kahan khare hain — "
             "0 matlab sab se neeche, 100 matlab sab se ooper.*")
    L.append("")

    for gkey, g in groups.items():
        rows = [(sd, results[sd["id"]]) for sd in g["series"]
                if sd["id"] in results]
        if not rows:
            continue

        L.append("---")
        L.append("")
        L.append(f"## {g['label']}")
        L.append("")

        change_keys = []
        for _, r in rows:
            for k in r["changes"]:
                if k not in change_keys:
                    change_keys.append(k)

        head = "| Series | Aaj | " + " | ".join(change_keys) + \
               " | %ile (3y) | Tareekh |"
        sep = "|---" * (3 + len(change_keys) + 1) + "|"
        L.append(head)
        L.append(sep)

        for sd, r in rows:
            cells = [arrow(r["changes"].get(k)) for k in change_keys]
            pct = r["percentile"]
            L.append(
                f"| {sd['name']} <br>`{sd['id']}` "
                f"| **{fmt(r['latest'], sd.get('unit',''))}** "
                f"| " + " | ".join(cells) +
                f" | {pct if pct is not None else '-'} "
                f"| {r['latest_date']} |"
            )
        L.append("")

    if failures:
        L.append("---")
        L.append("")
        L.append("## Jo nahi mili")
        L.append("")
        for sid, why in failures.items():
            L.append(f"- `{sid}` — {why}")
        L.append("")

    return "\n".join(L) + "\n"


# ==========================================================
# Main
# ==========================================================

def main():
    api_key = os.environ.get("FRED_API_KEY", "").strip()
    if not api_key:
        print("RUKAWAT: FRED_API_KEY nahi mili.")
        print("Repo -> Settings -> Secrets and variables -> Actions")
        print("mein FRED_API_KEY ke naam se secret banayein.")
        return 1

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    groups = cfg["fred_groups"]
    fs = cfg.get("fred_settings", {})
    years = fs.get("history_years", 3)
    pause = fs.get("sleep_between_calls", 0.3)
    timeout = fs.get("timeout", 25)

    now_utc = datetime.now(timezone.utc)
    now_pkt = to_pkt(now_utc)
    today_td = trading_day(now_pkt)
    start_date = (now_pkt.date() - timedelta(days=365 * years + 10)).isoformat()

    print("=" * 62)
    print(f"FETCH FRED   {now_pkt.strftime('%d %b %Y %H:%M')} PKT")
    print(f"Trading day: {today_td}")
    print("=" * 62)

    results, failures = {}, {}

    for gkey, g in groups.items():
        print(f"\n  {g['label']}")
        print("  " + "-" * 56)
        for sd in g["series"]:
            sid, kind = sd["id"], sd["kind"]
            try:
                obs = get_series(sid, api_key, start_date, timeout)
                r = analyse(obs, kind)
                if r is None:
                    raise RuntimeError("koi observation nahi")
                results[sid] = r
                ch = ", ".join(f"{k} {arrow(v)}"
                               for k, v in r["changes"].items())
                print(f"  OK    {sid:<14} {fmt(r['latest']):>12}   {ch}")
            except Exception as e:
                failures[sid] = str(e)
                print(f"  FAIL  {sid:<14} {e}")
            time.sleep(pause)

    d = day_dir(today_td)
    os.makedirs(d, exist_ok=True)

    with open(os.path.join(d, "macro.md"), "w", encoding="utf-8") as f:
        f.write(build_markdown(today_td, groups, results, now_pkt, failures))

    with open(os.path.join(d, "macro.json"), "w", encoding="utf-8") as f:
        json.dump({
            "generated_pkt": now_pkt.strftime("%d %b %Y %H:%M"),
            "trading_day": today_td.isoformat(),
            "series": results,
            "failures": failures,
        }, f, ensure_ascii=False, indent=2)

    with open(os.path.join(OUT_DIR, "last_run_macro.txt"), "w",
              encoding="utf-8") as f:
        f.write(f"macro | TD {today_td.strftime('%d %b %Y')} | "
                f"{now_pkt.strftime('%d %b %Y %H:%M')} PKT | "
                f"{len(results)} series\n")

    print()
    print(f"Mili: {len(results)}   Nahi mili: {len(failures)}")
    print(f"Likh diya: {d}/macro.md")

    # Kuch series fail hona normal hai — script khud nakaam nahi.
    return 0


if __name__ == "__main__":
    sys.exit(main())
