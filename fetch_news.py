"""
fetch_news.py  —  v3
--------------------
Har 5 minute chalta hai. Trading day ke hisaab se data jama karta hai.

TRADING DAY:
    Pakistani waqt ke mutabiq raat 3:00 AM se agle din raat 2:59 AM.
    Naam us din ka jo SHURU mein tha.
    Misal: 21 Aug 00:35 AM PKT  ->  trading day 2026-08-20

AHEM BAAT — dobara na aane dena:
    RSS mein khabar 24 ghante parri rehti hai. Agar har run har
    khabar likhta rahe to din bhar mein ek khabar 288 baar likhi
    jayegi. Is liye har khabar ki apni pehchan (id) hai. Khabar
    sirf pehli baar mehfooz hoti hai, aur us par wo waqt bhi likha
    jata hai jab wo PEHLI BAAR nazar aayi.

KUCH PHENKA NAHI JATA:
    Har khabar mehfooz hoti hai. Filter ke bajaye TAG lagta hai.
    Jis par koi tag nahi lagta wo bhi rehti hai. Noise baad mein
    keywords behtar kar ke chaani ja sakti hai — data dobara lane
    ki zaroorat nahi paregi.

STORAGE:
    output/2026-08-20/news.jsonl   <- ek line = ek khabar
    output/2026-08-20/news.md      <- padhne ke liye
    output/2026-08-20/feeds.json   <- feeds ki sehat
    Sirf aakhri 5 trading days rehte hain, purane khud mit jate hain.

DO ALAG CHEEZEIN:
    keep_trading_days = 5  -> kitne din ka data DISK par rehta hai
    recovery_days     = 1  -> kitne din peechay tak ki NAYI khabar
                              qabool hogi (sirf aaj ka trading day)
"""

import calendar
import difflib
import hashlib
import io
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests
import yaml

CONFIG_FILE = "config.yaml"
OUT_DIR = "output"

PKT = timezone(timedelta(hours=5))       # Pakistan — DST nahi hoti
DAY_START_HOUR = 3                       # trading day 3:00 AM PKT se

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

FEED_GROUPS = ["feeds_tier1", "feeds_cme", "feeds_google",
               "feeds_centralbank", "feeds_unverified", "feeds_support"]

TAG_ORDER = ["gold", "usd", "eur", "gbp", "jpy", "chf",
             "cad", "aud", "nzd", "oil", "crypto", "rates", "risk"]


# ==========================================================
# Trading day
# ==========================================================

def to_pkt(dt_utc):
    """Kisi bhi waqt ko Pakistani waqt mein badalta hai."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(PKT)


def trading_day(dt_pkt):
    """
    Pakistani waqt se trading day nikalta hai.
    Raat 3 baje se pehle ka waqt PICHHLE din ka hissa hai.
    """
    if dt_pkt.hour < DAY_START_HOUR:
        return (dt_pkt - timedelta(days=1)).date()
    return dt_pkt.date()


def day_window_pkt(day):
    """Us trading day ka shuru aur ikhtitam — dono PKT mein."""
    start = datetime(day.year, day.month, day.day,
                     DAY_START_HOUR, 0, 0, tzinfo=PKT)
    return start, start + timedelta(days=1) - timedelta(seconds=1)


# ==========================================================
# Chhote helpers
# ==========================================================

def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def clean_html(raw):
    if not raw:
        return ""
    txt = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    txt = re.sub(r"(?s)<[^>]+>", " ", txt)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                 ("&gt;", ">"), ("&#39;", "'"), ("&quot;", '"'),
                 ("\u2019", "'"), ("\u2018", "'"),
                 ("\u201c", '"'), ("\u201d", '"')):
        txt = txt.replace(a, b)
    txt = re.sub(r"\s+", " ", txt)
    txt = re.sub(r"\s+([.,;:!?%)])", r"\1", txt)
    txt = re.sub(r"([(])\s+", r"\1", txt)
    return txt.strip()


def snip(text, limit):
    """Lafz ke beech se nahi kaat-ta."""
    if not text or len(text) <= limit:
        return text or ""
    cut = text[:limit]
    dot = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if dot > limit * 0.5:
        return cut[:dot + 1]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 0 else cut).rstrip(" ,;:-") + " ..."


def norm_title(t):
    return re.sub(r"[^a-z0-9 ]", "", (t or "").lower()).strip()


def item_id(link, title, source):
    """
    Har khabar ki apni pehchan. Isi se pata chalta hai ke ye khabar
    pehle mehfooz ho chuki hai ya nahi. Yehi cheez har 5 minute par
    khabar ko dobara likhne se rokti hai.
    """
    base = (link or "").strip() or f"{source}|{norm_title(title)}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def entry_dt_utc(entry):
    for key in ("published_parsed", "updated_parsed"):
        p = entry.get(key)
        if p:
            try:
                return datetime.fromtimestamp(calendar.timegm(p),
                                              tz=timezone.utc)
            except Exception:
                continue
    return None


# Keyword ke patterns ek baar bana kar rakh lete hain
_PATTERN_CACHE = {}


def _patterns(keywords):
    """
    Har keyword ka poora-lafz pattern banata hai.

    YE KYUN ZAROORI HAI:
        Pehle sirf harf milaate the. Nateeja ye tha ke GBP ka
        keyword "ons" (UK ka Office for National Statistics)
        "Andersons", "billions", "conditions", "consolidations"
        — har us lafz se chipak jata tha. Isi tarah "war" ->
        "toward", "rba" -> "urban", "ism" -> "optimism",
        "franc" -> "France".

        Ab poora lafz milta hai, harf nahi.
    """
    key = id(keywords)
    if key not in _PATTERN_CACHE:
        built = {}
        for tag, words in keywords.items():
            pats = []
            for w in words:
                w = str(w).lower().strip()
                if not w:
                    continue
                pats.append(re.compile(r"(?<![a-z0-9])"
                                       + re.escape(w).replace(r"\ ", r"\s+")
                                       + r"(?![a-z0-9])"))
            built[tag] = pats
        _PATTERN_CACHE[key] = built
    return _PATTERN_CACHE[key]


def find_tags(text, keywords, weights=None):
    """
    Tag laga kar dete hain, sab se mazboot pehle.

    Score = kitne alag keywords mile  x  us tag ka weight.

    Weight is liye ke har macro khabar mein dollar, yields aur
    Fed ka zikr hota hai. Bina weight ke USD har cheez kha jata:
    "Gold pulls back as US yields recover after Treasury buyback"
    mein USD ke 4 lafz hain aur gold ke 3 — magar khabar gold
    ki hai.
    """
    low = (text or "").lower()
    weights = weights or {}
    scored = []
    for tag, pats in _patterns(keywords).items():
        n = sum(1 for pat in pats if pat.search(low))
        if n:
            scored.append((n * weights.get(tag, 1.0), tag))
    scored.sort(key=lambda x: (-x[0], TAG_ORDER.index(x[1])
                               if x[1] in TAG_ORDER else 99))
    return [tag for _, tag in scored]


# ==========================================================
# Purani mehfooz khabrein
# ==========================================================

def day_dir(day):
    return os.path.join(OUT_DIR, day.isoformat())


def load_seen(days):
    """
    Pichhle chand din ki sab mehfooz khabron ki pehchan uthata hai.
    Sirf aaj ka din kaafi nahi — khabar raat 2:58 par aa sakti hai
    aur 3:01 par dobara nazar aa sakti hai.
    """
    seen = set()
    for d in days:
        path = os.path.join(day_dir(d), "news.jsonl")
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    seen.add(json.loads(line)["id"])
                except Exception:
                    continue
    return seen


def load_day_items(day):
    path = os.path.join(day_dir(day), "news.jsonl")
    out = []
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    return out


# ==========================================================
# Feed uthana
# ==========================================================

def catchup_state(day):
    """
    Naye trading day ke shuru mein pichhle din ka bacha hua data
    samet-ne ke liye. Ginti rakhta hai ke aaj kitne run ho chuke.

    Ginti kyun, waqt kyun nahi:
        GitHub ka schedule kabhi late chalta hai. Agar "3:00 se
        3:15 tak" likhte aur GitHub 3:20 par chalta, to audit
        hoti hi nahi. Ginti se — pehle teen run jab bhi hon,
        audit ho jayegi.
    """
    path = os.path.join(day_dir(day), "catchup.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f).get("runs", 0)
        except Exception:
            return 0
    return 0


def save_catchup(day, runs):
    os.makedirs(day_dir(day), exist_ok=True)
    with open(os.path.join(day_dir(day), "catchup.json"), "w",
              encoding="utf-8") as f:
        json.dump({"runs": runs}, f)


def fetch_feed(feed, cfg, now_utc):
    name, url = feed["name"], feed["url"]
    cadence = feed.get("cadence_days", 1)
    warn_m = cfg["settings"].get("warn_multiplier", 1.5)
    stale_m = cfg["settings"].get("stale_multiplier", 3)
    timeout = cfg["settings"].get("request_timeout", 20)

    st = {"name": name, "status": "", "fetched": 0,
          "newest_age_days": None, "note": ""}

    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
    except Exception as e:
        st["status"] = "FAIL"
        st["note"] = type(e).__name__
        return st, []

    if r.status_code != 200:
        st["status"] = "FAIL"
        st["note"] = f"HTTP {r.status_code}"
        return st, []

    try:
        parsed = feedparser.parse(io.BytesIO(r.content))
    except Exception as e:
        st["status"] = "FAIL"
        st["note"] = f"parse: {type(e).__name__}"
        return st, []

    entries = parsed.get("entries", [])
    if not entries:
        st["status"] = "FAIL"
        st["note"] = "koi item nahi"
        return st, []

    dts = [d for d in (entry_dt_utc(e) for e in entries) if d]
    if dts:
        newest_age = (now_utc - max(dts)).total_seconds() / 86400.0
        st["newest_age_days"] = round(newest_age, 1)
        if newest_age > cadence * stale_m:
            st["status"] = "STALE"
            st["note"] = f"cadence {cadence}d, magar {newest_age:.0f}d purana"
        elif newest_age > cadence * warn_m:
            st["status"] = "WARN"
            st["note"] = "thora sust"
        else:
            st["status"] = "OK"
    else:
        st["status"] = "WARN"
        st["note"] = "tareekh nahi mili"

    max_chars = cfg["cleanup"].get("max_chars_per_item", 1500)
    out = []
    # Kuch feeds ka waqt aage ka hota hai. instaforex ki umar
    # 21 Aug ke test mein -0.2 din (yaani ~5 ghante AAGE) aayi.
    # Bina is ke raat 11 baje ki khabar AGLE trading day ke
    # folder mein chali jati.
    max_ahead = timedelta(minutes=15)      # itni si dheel theek hai

    for e in entries:
        dt = entry_dt_utc(e)
        if dt is None:
            continue                       # bina tareekh ke kuch nahi lete

        if dt > now_utc + max_ahead:
            st["note"] = (st["note"] + " " if st["note"] else "") + \
                         "waqt aage ka tha, theek kiya"
            dt = now_utc

        title = clean_html(e.get("title", ""))
        if not title:
            continue

        body = e.get("summary", "") or ""
        if not body and e.get("content"):
            try:
                body = e["content"][0].get("value", "")
            except Exception:
                body = ""

        out.append({
            "title": title,
            "body": snip(clean_html(body), max_chars),
            "link": e.get("link", ""),
            "source": name,
            "group": feed["_group"],
            "weight": feed.get("weight", 5),
            "feed_tag": feed.get("tag"),
            "published_utc": dt,
        })

    st["fetched"] = len(out)
    return st, out


# ==========================================================
# Pack likhna
# ==========================================================

# Chhote lafz jo har unwaan mein hote hain — inhein ginti mein
# nahi lete warna har khabar har khabar se milti hui lagti hai.
_STOP = {"the", "a", "an", "of", "in", "on", "at", "to", "for", "and",
         "as", "is", "are", "was", "were", "be", "by", "with", "from",
         "after", "amid", "ahead", "its", "it", "that", "this", "says",
         "said", "new", "more", "than", "over", "up", "down"}


_NOISE_CACHE = {}


def is_noise(title, patterns):
    """
    Kya ye unwaan shor hai? Aisi khabrein pack mein nahi dikhtin,
    magar news.jsonl mein MEHFOOZ rehti hain — kuch phenka nahi
    jata, bas nazar se hata diya jata hai.
    """
    if not patterns:
        return False
    key = id(patterns)
    if key not in _NOISE_CACHE:
        _NOISE_CACHE[key] = [re.compile(p) for p in patterns]
    return any(p.search(title or "") for p in _NOISE_CACHE[key])


def _key_words(title):
    return {w for w in norm_title(title).split()
            if len(w) > 2 and w not in _STOP}


# Alag alag cheezein. Agar do unwaanon mein in mein se ALAG
# cheezein hon, to wo kabhi ek jhund nahi banengi — chahe baqi
# lafz kitne hi miltay hon.
#
# Zaroorat kyun: "Gold rallies to fresh record high" aur "Silver
# rallies to fresh three-week high" mein teen lafz mushtarik hain,
# aur bina is pehre ke ye ek samjhi jati thin. Silver ki khabar
# Gold ke neeche chhup jati — ek trader ke liye ye nuqsan hai.
_SUBJECTS = [
    {"gold", "xau", "bullion"},
    {"silver", "xag"},
    {"platinum", "palladium"},
    {"copper"},
    {"oil", "crude", "wti", "brent"},
    {"gas", "lng"},
    {"bitcoin", "btc", "crypto", "ethereum"},
    {"euro", "eur", "ecb", "eurozone"},
    {"pound", "sterling", "gbp", "boe"},
    {"yen", "jpy", "boj", "japan"},
    {"franc", "chf", "snb"},
    {"aussie", "aud", "rba", "australia"},
    {"kiwi", "nzd", "rbnz"},
    {"loonie", "cad", "boc", "canada"},
]


def _subjects(words):
    return {i for i, grp in enumerate(_SUBJECTS) if words & grp}


def cluster_items(items, thresh=0.85):
    """
    Ek hi khabar jo kai feeds se aayi ho, unhein ek jhund mein
    jama karta hai. KUCH HATAYA NAHI JATA — sab kuch rehta hai,
    bas ek sath dikhta hai.

    Faida: jab ek khabar 6 feeds mein aaye aur doosri sirf 1
    mein, to ye khud batata hai ke duniya kis cheez ko ahem
    samajh rahi hai. Pehle ye 6 alag alag entries ban kar
    chhup jata tha.

    Tareeqa: pehle lafzon ka mel dekhte hain (sasta), phir jo
    qareeb lagen un par poora muqabla (mehnga). Bina is ke har
    khabar ka har khabar se muqabla karna parta.
    """
    # Zyada weight wali pehle — wahi jhund ki sarbarah banegi
    items = sorted(items, key=lambda x: (-x.get("weight", 5),
                                         -x["published_utc"].timestamp()))
    words = [_key_words(i["title"]) for i in items]

    clusters, used = [], [False] * len(items)
    for a in range(len(items)):
        if used[a]:
            continue
        used[a] = True
        group = [items[a]]
        if not words[a]:
            clusters.append(group)
            continue

        for b in range(a + 1, len(items)):
            if used[b] or not words[b]:
                continue
            # Pehra: alag cheezon ki khabrein kabhi na juren
            sa, sb = _subjects(words[a]), _subjects(words[b])
            if sa and sb and not (sa & sb):
                continue

            # Sasta filter: do tihai se kam lafz miltay hain to chhod do
            common = len(words[a] & words[b])
            small = min(len(words[a]), len(words[b]))
            if small == 0 or common / small < 0.65:
                continue

            # Ab poora muqabla. Bar ooncha rakha hai —
            # kam jorna behtar hai, ghalat jorne se.
            r = difflib.SequenceMatcher(
                None, norm_title(items[a]["title"]),
                norm_title(items[b]["title"])).ratio()
            if r >= thresh:
                used[b] = True
                group.append(items[b])

        clusters.append(group)
    return clusters


def build_markdown(day, items, statuses, now_pkt, noise=None):
    start, end = day_window_pkt(day)
    L = []
    L.append(f"# News Pack — Trading Day {day.strftime('%d %b %Y')}")
    L.append("")
    L.append(f"- Trading day: **{start.strftime('%d %b %H:%M')} "
             f"-> {end.strftime('%d %b %H:%M')} PKT**")
    L.append(f"- Aakhri update: **{now_pkt.strftime('%d %b %H:%M')} PKT**")
    L.append(f"- Kul khabrein: **{len(items)}**")
    ok = sum(1 for s in statuses if s["status"] == "OK")
    L.append(f"- Feeds: {ok}/{len(statuses)} OK")
    L.append("")

    def block(group):
        """Ek jhund likhta hai. Sarbarah poori, baqi ek ek line mein."""
        it = group[0]
        L.append(f"**{it['title']}**")
        pub = it.get("published_pkt", "")
        first = it.get("first_seen_pkt", "")
        stamp = f"`{pub} PKT`" if pub else ""
        if first and first != pub:
            stamp += f" · pehli baar dekhi `{first}`"
        line = f"{stamp} · {it['source']}"
        if len(group) > 1:
            line += f" · **{len(group)} feeds mein**"
        L.append(line)
        if it.get("body"):
            L.append("")
            L.append(snip(it["body"], 500))
        # Baqi feeds ki apni sharh — kuch hataya nahi gaya
        for other in group[1:]:
            L.append(f"  - `{other.get('published_pkt','')}` "
                     f"*{other['source']}* — {other['title']}")
        L.append("")

    # Shor alag kar do — mehfooz hai, bas dikhta nahi
    noisy = [i for i in items if is_noise(i.get("title", ""), noise)]
    noisy_ids = {id(i) for i in noisy}
    items = [i for i in items if id(i) not in noisy_ids]

    official = [i for i in items
                if i["group"] in ("feeds_centralbank", "feeds_cme")]
    rest = [i for i in items
            if i["group"] not in ("feeds_centralbank", "feeds_cme")]

    def newest(seq):
        return sorted(seq, key=lambda x: x["published_utc"], reverse=True)

    if official:
        L.append("---")
        L.append("")
        L.append("## Sarkari / Exchange")
        L.append("")
        for g in sorted(cluster_items(official),
                        key=lambda g: g[0]["published_utc"], reverse=True):
            block(g)

    if rest:
        L.append("---")
        L.append("")
        L.append("## Khabrein")
        L.append("")
        used = set()

        broad = [i for i in rest if len(i.get("tags", [])) >= 4]
        if broad:
            L.append("### MARKET WRAP")
            L.append("")
            for g in sorted(cluster_items(broad),
                            key=lambda g: g[0]["published_utc"], reverse=True):
                for it in g:
                    used.add(id(it))
                block(g)

        for tag in TAG_ORDER:
            # Sirf un khabron ko lo jin ka PEHLA (sab se mazboot)
            # tag yehi hai. Warna har khabar pehle milne wale
            # section mein chali jati hai, sahi wale mein nahi.
            grp = [i for i in rest
                   if i.get("tags") and i["tags"][0] == tag
                   and id(i) not in used]
            if not grp:
                continue
            L.append(f"### {tag.upper()}")
            L.append("")
            for g in sorted(cluster_items(grp),
                            key=lambda g: g[0]["published_utc"], reverse=True):
                for it in g:
                    used.add(id(it))
                block(g)

        # Jin par koi tag nahi laga — phenka kuch nahi jata
        untagged = [i for i in rest if id(i) not in used]
        if untagged:
            L.append(f"### BINA TAG ({len(untagged)})")
            L.append("")
            L.append("*In par koi keyword nahi laga. Mehfooz hain taake "
                     "baad mein keywords behtar karte waqt kaam aayen.*")
            L.append("")
            for it in newest(untagged):
                L.append(f"- `{it.get('published_pkt', '')}` "
                         f"**{it['source']}** — {it['title']}")
            L.append("")

    if noisy:
        from collections import Counter
        by_src = Counter(i["source"] for i in noisy)
        L.append("---")
        L.append("")
        L.append(f"## Shor — {len(noisy)} khabrein hatai gayin")
        L.append("")
        L.append("*Ye news.jsonl mein mehfooz hain, bas yahan nahi "
                 "dikhaya gaya. Zyada tar US insider-trading filings "
                 "aur earnings transcripts.*")
        L.append("")
        L.append(", ".join(f"{k} ({v})" for k, v in by_src.most_common()))
        L.append("")

    L.append("---")
    L.append("")
    L.append("## Data quality")
    L.append("")
    L.append("*Feed mein = is run par feed ne kitni khabrein dikhayin. "
             "Aaj ki = jo aaj ke trading day ki thin. Nayi = jo is run "
             "par pehli baar mili. Purani = jo pehle mehfooz ho chuki "
             "thin. Bahar = doosre trading day ki thin.*")
    L.append("")
    L.append("| Feed | Status | Feed mein | Aaj ki | Nayi | Purani "
             "| Bahar | Sab se nayi (din) |")
    L.append("|---|---|---|---|---|---|---|---|")
    for st in statuses:
        age = st["newest_age_days"]
        L.append(
            f"| {st['name']} | {st['status']} | {st['fetched']} "
            f"| {st.get('in_window', 0)} | {st.get('new', 0)} "
            f"| {st.get('seen_before', 0)} | {st.get('out_of_window', 0)} "
            f"| {age if age is not None else '-'} |")

    bad = [s for s in statuses if s["status"] in ("FAIL", "STALE")]
    if bad:
        L.append("")
        L.append("**Jo feeds nahi aaye:**")
        for s in bad:
            L.append(f"- {s['name']} — {s['status']}, {s['note']}")

    return "\n".join(L) + "\n"


# ==========================================================
# Purane din mitana
# ==========================================================

def prune(keep_days):
    """Sirf aakhri N trading days rakhta hai."""
    if not os.path.isdir(OUT_DIR):
        return []
    dirs = [n for n in os.listdir(OUT_DIR)
            if os.path.isdir(os.path.join(OUT_DIR, n))
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", n)]
    dirs.sort(reverse=True)
    removed = []
    for n in dirs[keep_days:]:
        shutil.rmtree(os.path.join(OUT_DIR, n), ignore_errors=True)
        removed.append(n)
    return removed


# ==========================================================
# Main
# ==========================================================

def main():
    cfg = load_config()
    now_utc = datetime.now(timezone.utc)
    now_pkt = to_pkt(now_utc)
    today_td = trading_day(now_pkt)
    keep = cfg["settings"].get("keep_trading_days", 5)
    recovery = cfg["settings"].get("recovery_days", 1)

    os.makedirs(OUT_DIR, exist_ok=True)

    print("=" * 64)
    print(f"FETCH NEWS   {now_pkt.strftime('%d %b %Y %H:%M')} PKT")
    print(f"Trading day: {today_td}")
    print("=" * 64)

    # Recovery window: aam tor par sirf AAJ ka trading day.
    valid_days = [today_td - timedelta(days=i) for i in range(recovery)]

    # Magar naye trading day ke shuru mein pehle chand run pichhle
    # din ka bacha hua data bhi samet-te hain. Zaroorat is liye ke
    # CME apni commentary raat 02:00-02:20 PKT par deta hai — yaani
    # 3:00 AM ki hadd se theek pehle. Bina is ke wo har roz reh jata.
    catchup_runs = cfg["settings"].get("catchup_runs", 3)
    runs_done = catchup_state(today_td)
    doing_catchup = runs_done < catchup_runs

    if doing_catchup:
        prev_td = today_td - timedelta(days=1)
        valid_days = valid_days + [prev_td]
        save_catchup(today_td, runs_done + 1)
        print(f"CATCHUP ON  — run {runs_done + 1}/{catchup_runs} "
              f"ke tor par {prev_td} ka bacha hua data bhi samet raha hoon")
    else:
        print(f"CATCHUP OFF — aaj ke {catchup_runs} audit run ho chuke, "
              f"ab sirf {today_td}")

    valid_set = set(valid_days)

    # "Dekhi hui" fehrist saare mehfooz dinon se uthate hain, sirf
    # window se nahi. Ye sasta hai aur duplicate ka koi khatra nahi
    # chhodta.
    seen = load_seen([today_td - timedelta(days=i) for i in range(keep)])

    win_start, _ = day_window_pkt(valid_days[-1])
    _, win_end = day_window_pkt(today_td)
    print(f"Recovery window: {recovery} trading day "
          f"({win_start.strftime('%d %b %H:%M')} -> "
          f"{win_end.strftime('%d %b %H:%M')} PKT)")
    print(f"Pehle se mehfooz: {len(seen)} khabrein\n")

    all_items, statuses = [], []
    for group in FEED_GROUPS:
        for feed in (cfg.get(group) or []):
            feed["_group"] = group
            st, items = fetch_feed(feed, cfg, now_utc)
            statuses.append(st)
            all_items.extend(items)
            print(f"  {st['status']:<6} {feed['name']:<22} "
                  f"{st['fetched']:>3}   {st['note']}")
            time.sleep(0.3)

    print(f"\nKul uthai: {len(all_items)}")

    keywords = cfg["keywords"]
    tag_weights = cfg.get("tag_weights", {})
    exempt = set(cfg.get("keyword_exempt_groups", []))
    fresh_by_day = {}
    skipped_old = 0
    already = 0

    # Har feed ka alag hisaab. Bina is ke pata hi nahi chalta ke
    # feed ne 50 khabrein di thin aur 0 kyun mehfooz huin.
    tally = {st["name"]: {"in_window": 0, "new": 0, "seen_before": 0,
                          "out_of_window": 0} for st in statuses}

    for it in all_items:
        pub_pkt = to_pkt(it["published_utc"])
        td = trading_day(pub_pkt)

        acc = tally.get(it["source"])

        if td not in valid_set:
            skipped_old += 1
            if acc:
                acc["out_of_window"] += 1
            continue

        if acc:
            acc["in_window"] += 1

        iid = item_id(it["link"], it["title"], it["source"])
        if iid in seen:
            already += 1
            if acc:
                acc["seen_before"] += 1
            continue

        if acc:
            acc["new"] += 1

        text = it["title"] + " " + it["body"]
        tags = find_tags(text, keywords, tag_weights)

        # Feed ka apna tag sirf tab lagao jab wo asal section ho.
        # Warna wo item kisi aise "section" mein chala jata hai jo
        # maujood hi nahi, aur BINA TAG mein gir jata hai.
        ft = it.get("feed_tag")
        if ft and ft in TAG_ORDER and ft not in tags:
            tags.insert(0, ft)
        if not tags and it["group"] in exempt:
            tags = ["rates"]

        rec = {
            "id": iid,
            "title": it["title"],
            "body": it["body"],
            "link": it["link"],
            "source": it["source"],
            "group": it["group"],
            "weight": it["weight"],
            "tags": tags,
            "published_utc": it["published_utc"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "published_pkt": pub_pkt.strftime("%d %b %H:%M"),
            "first_seen_pkt": now_pkt.strftime("%d %b %H:%M"),
            "trading_day": td.isoformat(),
        }
        seen.add(iid)
        fresh_by_day.setdefault(td, []).append(rec)

    for st in statuses:
        st.update(tally.get(st["name"], {}))

    total_new = sum(len(v) for v in fresh_by_day.values())
    print(f"Nayi khabrein: {total_new}   "
          f"(pehle se maujood: {already}, "
          f"window se bahar: {skipped_old})")

    for td, recs in sorted(fresh_by_day.items()):
        d = day_dir(td)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "news.jsonl"), "a", encoding="utf-8") as f:
            for rec in recs:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"  + {len(recs):>3} -> {td}")

    # Feed ke apne tags (CME wale) — dobara tag lagane ke liye
    feed_tags = {}
    for group in FEED_GROUPS:
        for f in (cfg.get(group) or []):
            if f.get("tag"):
                feed_tags[f["name"]] = f["tag"]

    # Har mehfooz din ka md dobara banao — sirf chhoo hue ka nahi.
    #
    # TAGS HAR BAAR NAYE SIRE SE LAGTE HAIN:
    #   Pehle tag likhte waqt pakka ho jata tha aur jsonl mein
    #   mehfooz ho jata tha. Nateeja: keywords behtar karne se
    #   sirf NAYI khabrein theek hoti thin, purani hamesha ghalat
    #   rehti thin.
    #   Ab tag padhte waqt lagta hai. Jab bhi keywords behtar
    #   karenge, saara mehfooz data khud theek ho jayega — data
    #   dobara lane ki zaroorat nahi.
    rebuild_days = [today_td - timedelta(days=i) for i in range(keep)]
    for td in sorted(set(rebuild_days + list(fresh_by_day.keys()))):
        d = day_dir(td)
        if not os.path.exists(os.path.join(d, "news.jsonl")):
            continue
        os.makedirs(d, exist_ok=True)
        recs = load_day_items(td)
        for r in recs:
            r["published_utc"] = datetime.strptime(
                r["published_utc"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)

            t = find_tags(r["title"] + " " + r.get("body", ""),
                          keywords, tag_weights)
            ft = feed_tags.get(r["source"])
            if ft and ft in TAG_ORDER and ft not in t:
                t.insert(0, ft)
            if not t and r.get("group") in exempt:
                t = ["rates"]
            r["tags"] = t
        with open(os.path.join(d, "news.md"), "w", encoding="utf-8") as f:
            f.write(build_markdown(td, recs, statuses, now_pkt,
                                   cfg.get("noise_patterns")))
        with open(os.path.join(d, "feeds.json"), "w", encoding="utf-8") as f:
            json.dump({"updated_pkt": now_pkt.strftime("%d %b %H:%M"),
                       "feeds": statuses}, f, ensure_ascii=False, indent=2)

    removed = prune(keep)
    if removed:
        print(f"\nMitaye gaye din: {', '.join(removed)}")

    ok = sum(1 for s in statuses if s["status"] == "OK")
    with open(os.path.join(OUT_DIR, "run_log.txt"), "a",
              encoding="utf-8") as f:
        f.write(f"{now_pkt.strftime('%Y-%m-%d %H:%M')} PKT  "
                f"td={today_td}  feeds_ok={ok}/{len(statuses)}  "
                f"new={total_new}\n")

    # Commit message yahin banta hai, workflow mein nahi.
    # Wajah: trading day ka hisaab sirf yahan hai. Agar bash mein
    # dobara likhte to do jagah do hisaab hote aur kabhi na kabhi
    # aapas mein farq aa jata.
    commit_msg = (f"news | TD {today_td.strftime('%d %b %Y')} | "
                  f"{now_pkt.strftime('%d %b %Y %H:%M')} PKT | "
                  f"nayi {total_new}")
    with open(os.path.join(OUT_DIR, "last_run.txt"), "w",
              encoding="utf-8") as f:
        f.write(commit_msg + "\n")

    print(f"\nAaj ka pack: {day_dir(today_td)}/news.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
