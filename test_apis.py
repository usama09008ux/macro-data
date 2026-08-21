"""
test_apis.py
------------
Sirf ye jaanchne ke liye ke Finnhub aur FMP ke MUFT tier par
kaun se endpoints asal mein kaam karte hain.

Kyun zaroori hai:
    Dono kehte hain "free tier available", magar dono ke paas kuch
    endpoints sirf paid subscription par hain. Documentation se
    saaf pata nahi chalta. ForexFactory ke `nextweek` ke sath yahi
    hua tha — bina jaanche laga diya aur wo kaam nahi kiya.

    Is liye pehle jaancho, phir banao.

Sirf DO cheezein dekhni hain (baqi sab aap ke paas pehle se hai):
    1. Economic calendar  — khaas kar AGLA hafta
    2. Spot forex rates   — XAUUSD, EURUSD, GBPUSD, USDJPY, DXY

Chalane ka tareeqa:  python test_apis.py
"""

import json
import os
import sys
from datetime import date, timedelta

import requests

TIMEOUT = 25

FINNHUB = os.environ.get("FINNHUB_API_KEY", "").strip()
FMP = os.environ.get("FMP_API_KEY", "").strip()

today = date.today()
wk_from = today.isoformat()
wk_to = (today + timedelta(days=14)).isoformat()


def show(name, ok, detail, sample=None):
    mark = "OK  " if ok else "NAHI"
    print(f"  {mark}  {name:<34} {detail}")
    if ok and sample:
        print(f"        namoona: {sample}")


def probe(name, url, params, checker):
    """
    Ek endpoint jaanchta hai.
    checker(data) -> (ok, detail, sample)
    """
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
    except Exception as e:
        show(name, False, f"{type(e).__name__}")
        return False

    if r.status_code == 401:
        show(name, False, "401 — key qabool nahi hui")
        return False
    if r.status_code == 403:
        show(name, False, "403 — ye endpoint MUFT tier par nahi hai")
        return False
    if r.status_code == 429:
        show(name, False, "429 — had paar ho gayi")
        return False
    if r.status_code != 200:
        show(name, False, f"HTTP {r.status_code} — {r.text[:90]}")
        return False

    try:
        data = r.json()
    except Exception:
        show(name, False, f"JSON nahi mila — {r.text[:90]}")
        return False

    # FMP paid endpoints par 200 ke sath error message bhejta hai
    if isinstance(data, dict):
        for k in ("Error Message", "error", "message"):
            if k in data:
                show(name, False, f"{str(data[k])[:90]}")
                return False

    try:
        ok, detail, sample = checker(data)
    except Exception as e:
        show(name, False, f"shakl samajh nahi aayi: {type(e).__name__}")
        return False

    show(name, ok, detail, sample)
    return ok


# ==========================================================
# Finnhub
# ==========================================================

def check_finnhub():
    print()
    print("=" * 70)
    print("FINNHUB   (muft tier: ~60 calls/minute)")
    print("=" * 70)
    if not FINNHUB:
        print("  FINNHUB_API_KEY nahi mili — chhod raha hoon")
        return {}

    res = {}

    def cal_check(d):
        ev = d.get("economicCalendar", d if isinstance(d, list) else [])
        if not ev:
            return False, "khali jawab aaya", None
        fut = [e for e in ev if str(e.get("time", ""))[:10] > today.isoformat()]
        e0 = ev[0]
        s = (f"{e0.get('time','?')} {e0.get('country','?')} "
             f"{str(e0.get('event','?'))[:34]} "
             f"est={e0.get('estimate')} prev={e0.get('prev')}")
        return True, f"{len(ev)} events, {len(fut)} aage ke", s

    res["calendar"] = probe(
        "Economic calendar", "https://finnhub.io/api/v1/calendar/economic",
        {"token": FINNHUB, "from": wk_from, "to": wk_to}, cal_check)

    def fx_check(d):
        q = d.get("quote", d)
        n = len(q) if isinstance(q, dict) else 0
        if not n:
            return False, "khali", None
        keys = [k for k in list(q)[:6]]
        return True, f"{n} rates", ", ".join(f"{k}={q[k]}" for k in keys[:4])

    res["forex"] = probe(
        "Forex rates (spot)", "https://finnhub.io/api/v1/forex/rates",
        {"token": FINNHUB, "base": "USD"}, fx_check)

    return res


# ==========================================================
# FMP
# ==========================================================

def check_fmp():
    print()
    print("=" * 70)
    print("FMP   (muft tier: 250 calls/day)")
    print("=" * 70)
    if not FMP:
        print("  FMP_API_KEY nahi mili — chhod raha hoon")
        return {}

    res = {}

    def cal_check(d):
        if not isinstance(d, list) or not d:
            return False, "khali jawab aaya", None
        fut = [e for e in d if str(e.get("date", ""))[:10] > today.isoformat()]
        e0 = d[0]
        s = (f"{e0.get('date','?')} {e0.get('country','?')} "
             f"{str(e0.get('event','?'))[:34]} "
             f"est={e0.get('estimate')} prev={e0.get('previous')}")
        return True, f"{len(d)} events, {len(fut)} aage ke", s

    res["calendar"] = probe(
        "Economic calendar",
        "https://financialmodelingprep.com/stable/economic-calendar",
        {"apikey": FMP, "from": wk_from, "to": wk_to}, cal_check)

    def fx_check(d):
        rows = d if isinstance(d, list) else [d]
        if not rows:
            return False, "khali", None
        r0 = rows[0]
        return True, f"{len(rows)} pairs", f"{r0}"[:100]

    res["forex"] = probe(
        "Forex quote (EURUSD)",
        "https://financialmodelingprep.com/stable/quote",
        {"apikey": FMP, "symbol": "EURUSD"}, fx_check)

    res["gold"] = probe(
        "Gold quote (XAUUSD)",
        "https://financialmodelingprep.com/stable/quote",
        {"apikey": FMP, "symbol": "XAUUSD"}, fx_check)

    return res


# ==========================================================
# Main
# ==========================================================

def main():
    if not FINNHUB and not FMP:
        print("Koi key nahi mili.")
        print("Repo -> Settings -> Secrets and variables -> Actions")
        print("mein FINNHUB_API_KEY aur FMP_API_KEY banayein.")
        return 1

    f = check_finnhub()
    m = check_fmp()

    print()
    print("=" * 70)
    print("FAISLA")
    print("=" * 70)

    cal = []
    if f.get("calendar"):
        cal.append("Finnhub")
    if m.get("calendar"):
        cal.append("FMP")
    if cal:
        print(f"  Calendar mil raha hai: {', '.join(cal)}")
        print("  -> ForexFactory ke nextweek ka backup ban sakta hai")
    else:
        print("  Calendar kisi se nahi mila — muft tier par nahi hai.")
        print("  -> ForexFactory hi rahega; nextweek ka koi aur hal dhoondna hoga")

    fx = []
    if f.get("forex"):
        fx.append("Finnhub")
    if m.get("forex") or m.get("gold"):
        fx.append("FMP")
    if fx:
        print(f"  Spot prices mil rahe hain: {', '.join(fx)}")
        print("  -> rozana snapshot ban sakta hai (abhi macro-data mein")
        print("     ek bhi qeemat nahi hai)")
    else:
        print("  Spot prices kisi se nahi mile.")

    if not cal and not fx:
        print()
        print("  Dono cheezein nahi milin — in par waqt zaya na karein.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
