#!/usr/bin/env python3
"""
סוכן משרות: סורק לוחות משרות של חברות הייטק ופינטק, מסנן לפי הפרופיל,
מדרג כל משרה חדשה עם Claude ושולח התראה בטלגרם / מייל.

הרצה:
    python agent.py            # ריצה מלאה
    python agent.py --dry-run  # בלי Claude ובלי התראות - רק מדפיס מה עבר סינון
"""
import argparse
import hashlib
import html
import json
import os
import re
import smtplib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
SEEN_FILE = DATA / "seen_jobs.json"
CACHE_FILE = DATA / "ats_cache.json"
DISCOVERED_FILE = DATA / "discovered_companies.json"
LATEST_FILE = DATA / "latest_matches.json"
REVIEW_MD = DATA / "for_review.md"
REVIEW_JSON = DATA / "for_review.json"

CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
COMPANIES = yaml.safe_load((ROOT / "companies.yaml").read_text(encoding="utf-8"))["companies"]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-job-agent/1.0)"}
TIMEOUT = 20
RECHECK_DAYS = 3           # כל כמה ימים לבדוק שוב חברה שלא נמצא לה לוח משרות
SEEN_RETENTION_DAYS = 180  # כמה זמן לזכור משרות שכבר נבדקו


# ---------------------------------------------------------------- utils

def log(*a):
    print(*a, flush=True)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def strip_html(s):
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm(s):
    return re.sub(r"[^a-z0-9א-ת]", "", (s or "").lower())


class Transient(Exception):
    """תקלה זמנית (חסימת קצב / שרת נופל) - לא להסיק מזה שאין לחברה לוח משרות."""


TRANSIENT_CODES = {403, 408, 425, 429, 500, 502, 503, 504}


def get(url, **kw):
    """מחזיר תשובה, None אם הדף באמת לא קיים, וזורק Transient על תקלה זמנית."""
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)
        except requests.RequestException:
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code == 200:
            return r
        if r.status_code in TRANSIENT_CODES:
            time.sleep(2 * (attempt + 1))
            continue
        return None
    raise Transient(url)


def jget(url, **kw):
    """GET שמחזיר JSON, או None אם הדף לא קיים / לא JSON."""
    r = get(url, **kw)
    if not r:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def job(id_, company, title, location, url, description=""):
    return {
        "id": id_, "company": company, "title": (title or "").strip(),
        "location": (location or "").strip(), "url": url or "",
        "description": (description or "")[:6000],
    }


# ------------------------------------------------ ATS fetchers
# כל פונקציה מחזירה רשימת משרות, או None אם אין לוח כזה לחברה.

def fetch_greenhouse(slug, company):
    data = jget(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
    if not isinstance(data, dict):
        return None
    out = [job(f"gh:{slug}:{j['id']}", company, j.get("title"),
               (j.get("location") or {}).get("name", ""), j.get("absolute_url"),
               strip_html(j.get("content")))
           for j in data.get("jobs", [])]
    return out or None


def fetch_lever(slug, company):
    for host in ("api.lever.co", "api.eu.lever.co"):
        data = jget(f"https://{host}/v0/postings/{slug}", params={"mode": "json"})
        if not isinstance(data, list) or not data:
            continue
        out = []
        for j in data:
            cat = j.get("categories") or {}
            locs = cat.get("allLocations") or [cat.get("location", "")]
            desc = (j.get("descriptionPlain") or "") + " " + " ".join(
                strip_html(x.get("content", "")) for x in j.get("lists") or [])
            out.append(job(f"lv:{slug}:{j['id']}", company, j.get("text"),
                           " / ".join(l for l in locs if l), j.get("hostedUrl"), desc))
        return out
    return None


def fetch_ashby(slug, company):
    data = jget(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if not isinstance(data, dict):
        return None
    out = []
    for j in data.get("jobs", []):
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        out.append(job(f"ab:{slug}:{j['id']}", company, j.get("title"),
                       " / ".join(l for l in locs if l), j.get("jobUrl"), j.get("descriptionPlain", "")))
    return out or None


def fetch_smartrecruiters(slug, company):
    out, offset = [], 0
    while offset < 2000:
        data = jget(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                    params={"limit": 100, "offset": offset})
        if not isinstance(data, dict):
            break
        items = data.get("content", [])
        for j in items:
            loc = j.get("location") or {}
            loc_s = loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("country")) if x)
            out.append(job(f"sr:{slug}:{j['id']}", company, j.get("name"), loc_s,
                           f"https://jobs.smartrecruiters.com/{slug}/{j['id']}"))
        offset += len(items)
        if not items or offset >= data.get("totalFound", 0):
            break
    # ל-SmartRecruiters אין 404 לחברה לא קיימת, אז לוח ריק = לא נמצא
    return out or None


def fetch_workable(slug, company):
    data = jget(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", params={"details": "true"})
    if not isinstance(data, dict) or not data.get("jobs"):
        return None  # Workable מחזירה חשבון ריק גם לשמות שלא קיימים
    out = []
    for j in data.get("jobs", []):
        loc = j.get("location") or {}
        loc_s = ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
        if j.get("telecommuting"):
            loc_s += " (Remote)"
        out.append(job(f"wk:{slug}:{j.get('shortcode') or j.get('id')}", company, j.get("title"), loc_s,
                       j.get("url") or j.get("application_url"), strip_html(j.get("description", ""))))
    return out or None


def fetch_recruitee(slug, company):
    data = jget(f"https://{slug}.recruitee.com/api/offers/")
    if not isinstance(data, dict) or not data.get("offers"):
        return None
    out = []
    for j in data.get("offers", []):
        loc_s = ", ".join(x for x in (j.get("city"), j.get("country")) if x) or j.get("location", "")
        out.append(job(f"rc:{slug}:{j.get('id')}", company, j.get("title"), loc_s,
                       j.get("careers_url") or j.get("careers_apply_url"),
                       strip_html(j.get("description", ""))))
    return out or None


def fetch_bamboohr(slug, company):
    data = jget(f"https://{slug}.bamboohr.com/careers/list")
    if not isinstance(data, dict) or not data.get("result"):
        return None
    out = []
    for j in data.get("result", []):
        loc = j.get("location") or {}
        loc_s = ", ".join(x for x in (loc.get("city"), loc.get("state"), loc.get("country")) if x)
        out.append(job(f"bh:{slug}:{j.get('id')}", company, j.get("jobOpeningName"), loc_s,
                       f"https://{slug}.bamboohr.com/careers/{j.get('id')}"))
    return out or None


def fetch_comeet(cfg, company):
    data = jget(f"https://www.comeet.co/careers-api/2.0/company/{cfg['uid']}/positions",
                params={"token": cfg["token"], "details": "true"})
    if not isinstance(data, list):
        return None
    out = []
    for j in data:
        loc = j.get("location") or {}
        loc_s = ", ".join(x for x in (loc.get("name"), loc.get("city"), loc.get("country")) if x)
        desc = " ".join(strip_html(d.get("value", "")) for d in j.get("details") or [])
        url = j.get("url_active_page") or j.get("url_comeet_hosted_page") or j.get("position_url")
        out.append(job(f"cm:{cfg['uid']}:{j.get('uid')}", company, j.get("name"), loc_s, url, desc))
    return out


def fetch_workday(cfg, company):
    host, tenant, site = cfg["host"], cfg["tenant"], cfg["site"]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    search = cfg.get("search", "Israel")
    out, offset, total = [], 0, 0
    while offset < 1000:
        try:
            r = requests.post(api, json={"appliedFacets": {}, "limit": 20, "offset": offset,
                                         "searchText": search}, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code != 200:
                break
            data = r.json()
        except (requests.RequestException, ValueError):
            break
        total = data.get("total") or total  # Workday מחזיר total רק בעמוד הראשון
        posts = data.get("jobPostings", [])
        for j in posts:
            path = j.get("externalPath", "")
            loc = j.get("locationsText", "")
            if re.match(r"^\d+ Locations?$", loc) and search.lower() == "israel":
                loc += " (Israel)"
            out.append(job(f"wd:{tenant}:{path}", company, j.get("title"), loc, f"https://{host}/{site}{path}"))
        offset += 20
        if not posts or offset >= total:
            break
    return out or None


PROBE_ORDER = [
    ("greenhouse", fetch_greenhouse),
    ("lever", fetch_lever),
    ("ashby", fetch_ashby),
    ("smartrecruiters", fetch_smartrecruiters),
    ("workable", fetch_workable),
    ("recruitee", fetch_recruitee),
    ("bamboohr", fetch_bamboohr),
]
FETCHERS = dict(PROBE_ORDER)


GENERIC_WORDS = {"security", "software", "technologies", "technology", "labs", "lab", "global",
                 "networks", "network", "systems", "system", "group", "solutions", "ai", "io",
                 "platform", "insurance", "payments", "capital", "digital", "cyber", "health"}


def slug_candidates(entry):
    """מנחש את מזהה החברה בלוח המשרות. כל וריאציה נבדקת מול כל מערכת גיוס."""
    if entry.get("slugs"):
        return entry["slugs"]
    n = entry["name"].lower().strip()
    short = re.sub(r"\.(com|io|ai|co)$", "", n)
    words = [w for w in re.split(r"[^a-z0-9]+", short) if w]
    cands = [
        re.sub(r"[^a-z0-9]", "", n),          # checkpoint
        re.sub(r"[^a-z0-9]+", "-", n).strip("-"),  # check-point
        re.sub(r"[^a-z0-9]", "", short),
    ]
    # "Aqua Security" -> "aqua", "Papaya Global" -> "papaya"
    if len(words) > 1 and words[-1] in GENERIC_WORDS and len(words[0]) >= 4:
        core = "".join(words[:-1])
        cands += [core, "-".join(words[:-1])]
    # "Check Point" -> "checkpointsoftware", "Wix" -> "wixcom"
    cands.append(re.sub(r"[^a-z0-9]", "", n) + "careers")
    return list(dict.fromkeys(c for c in cands if c and len(c) > 2))


def collect(entry, cache):
    """מחזיר את כל המשרות של חברה. מזהה אוטומטית את מערכת הגיוס ושומר ב-cache."""
    name = entry["name"]
    ats = entry.get("ats")
    if ats == "comeet":
        return fetch_comeet(entry, name) or []
    if ats == "workday":
        return fetch_workday(entry, name) or []

    c = cache.get(name)
    if c and c.get("ats"):
        try:
            jobs = FETCHERS[c["ats"]](c["slug"], name)
        except Transient:
            log(f"  ~ {name}: תקלה זמנית בשרת, ננסה שוב בריצה הבאה")
            return []
        if jobs is not None:
            return jobs
        log(f"  ! {name}: הלוח ב-{c['ats']} נעלם, מחפש מחדש")
    elif c and time.time() - c.get("checked", 0) < RECHECK_DAYS * 86400:
        return []

    try:
        for slug in slug_candidates(entry):
            for a, fn in PROBE_ORDER:
                if ats and ats != a:
                    continue
                jobs = fn(slug, name)
                if jobs is not None:
                    cache[name] = {"ats": a, "slug": slug}
                    log(f"  ✓ {name}: {a}/{slug}")
                    return jobs
    except Transient:
        log(f"  ~ {name}: תקלה זמנית בשרת, ננסה שוב בריצה הבאה")
        return []  # בלי לשמור ב-cache שאין לה לוח
    cache[name] = {"ats": None, "checked": time.time()}
    log(f"  ✗ {name}: לא נמצא לוח משרות ציבורי (אפשר להגדיר ידנית ב-companies.yaml)")
    return []


# ------------------------------------------------ discovery (Google Jobs via SerpAPI, optional)

def discover():
    key = os.getenv("SERPAPI_KEY")
    if not key:
        return [], []
    d = CONFIG.get("discovery") or {}
    jobs, companies = [], set()
    for q in d.get("serpapi_queries", []):
        r = get("https://serpapi.com/search.json", params={
            "engine": "google_jobs", "q": q, "hl": "en", "api_key": key,
            "location": d.get("serpapi_location", "Tel Aviv-Yafo, Israel")})
        if not r:
            log(f"  ! SerpAPI נכשל עבור '{q}'")
            continue
        for j in r.json().get("jobs_results", []):
            comp = j.get("company_name", "")
            links = j.get("apply_options") or []
            url = links[0].get("link") if links else j.get("share_link", "")
            jid = "gj:" + hashlib.md5(f"{norm(comp)}|{norm(j.get('title'))}".encode()).hexdigest()[:16]
            jobs.append(job(jid, comp, j.get("title"), j.get("location"), url, j.get("description", "")))
            if comp:
                companies.add(comp)
    return jobs, sorted(companies)


# ------------------------------------------------ filtering

SHARED = CONFIG.get("filters") or {}
LOCATIONS = [k.lower() for k in SHARED.get("locations", [])]
SENIOR = [k.lower() for k in SHARED.get("senior_keywords", [])]
PROFILES = CONFIG["profiles"]
for _p in PROFILES:
    _p["_inc"] = [k.lower() for k in _p.get("title_include", [])]
    _p["_exc"] = [k.lower() for k in _p.get("title_exclude", [])]
MIN_SCORE = {p["name"]: p.get("min_score", CONFIG["scoring"].get("min_score", 70)) for p in PROFILES}


def location_ok(j):
    loc = j["location"].lower()
    if any(k in loc for k in LOCATIONS):
        return True
    if SHARED.get("include_remote") and "remote" in loc:
        return True
    # "Israel" בלי עיר (למשל "Israel" / "Israel - Hybrid"), אבל לא "Haifa, Israel"
    if SHARED.get("include_generic_israel") and ("israel" in loc or "ישראל" in loc):
        rest = re.sub(r"israel|ישראל|remote|hybrid|office|\(.*?\)|\d+ locations?|[,\-/|\s]", "", loc)
        return rest == ""
    return False


def matching_profiles(j):
    """אילו מהפרופילים שלך רלוונטיים למשרה הזו (יכול להיות יותר מאחד)."""
    if not location_ok(j):
        return []
    t = j["title"].lower()
    return [p["name"] for p in PROFILES
            if any(k in t for k in p["_inc"]) and not any(k in t for k in p["_exc"])]


# ------------------------------------------------ scoring with Claude

SYSTEM_PROMPT = (
    "You evaluate job postings for one specific candidate. "
    'Reply with ONLY a JSON object: {"score": <integer 0-100>, "reason": "<one short sentence in Hebrew>"}. '
    "The score reflects fit to the candidate's experience, seniority, domain and stated preferences. "
    "Be strict: give under 50 when seniority, domain or core skills clearly don't match. "
    "If there is no description, judge from title and company and be more conservative."
)


def profile_context(p):
    resume = (ROOT / p["resume_file"]).read_text(encoding="utf-8")
    return f"CANDIDATE RESUME:\n{resume}\n\nCANDIDATE PREFERENCES:\n{p.get('preferences', '')}"


def ask_claude(client, context, j):
    posting = (f"JOB POSTING:\nCompany: {j['company']}\nTitle: {j['title']}\n"
               f"Location: {j['location']}\nDescription: {j['description'] or '(not available)'}")
    try:
        msg = client.messages.create(
            model=CONFIG["scoring"]["model"],
            max_tokens=300,
            system=[{"type": "text", "text": SYSTEM_PROMPT},
                    {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": posting}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
        data = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        return {"score": int(data.get("score", 0)), "reason": data.get("reason", "")}
    except Exception as e:  # noqa: BLE001
        log(f"  ! דירוג נכשל ({j['company']} - {j['title']}): {e}")
        return None


def score_jobs(jobs):
    """מדרג כל משרה מול כל פרופיל רלוונטי, ושומר את ההתאמה הטובה ביותר."""
    import anthropic
    client = anthropic.Anthropic()
    contexts = {p["name"]: profile_context(p) for p in PROFILES}
    for i, j in enumerate(jobs, 1):
        best = None
        for pname in j["profiles"]:
            r = ask_claude(client, contexts[pname], j)
            if r and (best is None or r["score"] > best["score"]):
                best = dict(r, profile=pname)
        if best:
            j.update(best)
        else:
            j["score"] = None
        if i % 20 == 0:
            log(f"  דורגו {i}/{len(jobs)}")


# ------------------------------------------------ notifications

def format_job(j):
    tag = f"{j['score']}/100 · {j['profile']}" if j.get("score") is not None else j["profile"]
    reason = f"\n💡 {j['reason']}" if j.get("reason") else ""
    return f"{j['title']} — {j['company']}  [{tag}]\n📍 {j['location']}{reason}\n{j['url']}"


def send_telegram(blocks):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        log("  ! טלגרם: חסרים TELEGRAM_BOT_TOKEN או TELEGRAM_CHAT_ID ב-Secrets")
        return
    token, chat = token.strip(), chat.strip()
    if token.lower().startswith("bot"):
        token = token[3:]  # המילה bot היא חלק מהכתובת, לא מהטוקן

    def send(text):
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
                              timeout=TIMEOUT)
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        except requests.RequestException as e:
            log(f"  ! טלגרם: השליחה נכשלה ({e})")
            return False
        if data.get("ok"):
            return True
        log(f"  ! טלגרם החזיר שגיאה: {data.get('error_code')} - {data.get('description', r.text[:200])}")
        return False

    chunks, chunk = [], ""
    for b in blocks:
        if len(chunk) + len(b) > 3800:
            chunks.append(chunk)
            chunk = ""
        chunk += b + "\n\n"
    if chunk:
        chunks.append(chunk)

    sent = sum(1 for c in chunks if send(c))
    if sent == len(chunks):
        log(f"  נשלחו לטלגרם {sent} הודעות")
    else:
        log(f"  ! טלגרם: נשלחו {sent} מתוך {len(chunks)} הודעות")


def send_email(blocks, subject):
    user, pwd, to = os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD"), os.getenv("EMAIL_TO")
    if not (user and pwd and to):
        return
    msg = MIMEText("\n\n".join(blocks), "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    try:
        with smtplib.SMTP_SSL(os.getenv("SMTP_HOST", "smtp.gmail.com"), int(os.getenv("SMTP_PORT", "465"))) as srv:
            srv.login(user, pwd)
            srv.send_message(msg)
        log("  נשלח במייל")
    except Exception as e:  # noqa: BLE001
        log(f"  ! שליחת המייל נכשלה: {e}")


def write_review_files(jobs):
    """קבצים שה-Cowork קורא: אחד לקריאה נוחה, אחד מובנה."""
    save_json(REVIEW_JSON, [{"company": j["company"], "title": j["title"], "location": j["location"],
                             "url": j["url"], "profiles": j.get("profiles", []),
                             "description": j["description"][:1500]} for j in jobs])
    lines = [f"# משרות חדשות לדירוג — {date.today().isoformat()}", ""]
    for j in jobs:
        lines.append(f"## {j['title']} — {j['company']}")
        lines.append(f"- פרופיל רלוונטי: {', '.join(j.get('profiles', []))}")
        lines.append(f"- מיקום: {j['location']}")
        lines.append(f"- קישור: {j['url']}")
        if j["description"]:
            lines.append(f"- תיאור: {j['description'][:1200]}")
        lines.append("")
    REVIEW_MD.write_text("\n".join(lines), encoding="utf-8")


def notify(good, scored=True):
    if not good:
        log("אין היום משרות חדשות.")
        return
    subject = (f"🎯 {len(good)} משרות חדשות שמתאימות לך" if scored
               else f"📋 {len(good)} משרות חדשות לדירוג")
    blocks = [subject] + [format_job(j) for j in good]
    send_telegram(blocks)
    send_email(blocks, subject)


# ------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="בלי דירוג ובלי התראות")
    args = ap.parse_args()

    cache = load_json(CACHE_FILE, {})
    seen = load_json(SEEN_FILE, {})
    discovered = load_json(DISCOVERED_FILE, [])

    entries = list(COMPANIES)
    known = {e["name"].lower() for e in entries}
    entries += [{"name": n} for n in discovered if n.lower() not in known]

    log(f"סורק {len(entries)} חברות...")
    all_jobs = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(collect, e, cache): e["name"] for e in entries}
        for f in as_completed(futures):
            try:
                all_jobs += f.result()
            except Exception as e:  # noqa: BLE001
                log(f"  ! שגיאה ב-{futures[f]}: {e}")

    g_jobs, g_companies = discover()
    all_jobs += g_jobs
    new_companies = [c for c in g_companies if c.lower() not in known and c not in discovered]
    if new_companies:
        log(f"  התגלו {len(new_companies)} חברות חדשות: {', '.join(new_companies[:15])}")
        discovered += new_companies

    # איחוד כפילויות (לוח המשרות של החברה עדיף על Google Jobs כי הוא נאסף ראשון)
    uniq = {}
    for j in all_jobs:
        uniq.setdefault(norm(j["company"]) + "|" + norm(j["title"]), j)

    candidates = []
    for j in uniq.values():
        if j["id"] in seen:
            continue
        j["profiles"] = matching_profiles(j)
        if j["profiles"]:
            candidates.append(j)
    candidates.sort(key=lambda j: not any(k in j["title"].lower() for k in SENIOR))  # בכירים קודם
    log(f"נאספו {len(all_jobs)} משרות, {len(candidates)} חדשות עברו את הסינון")

    if args.dry_run:
        for j in candidates:
            log(f"  - [{'/'.join(j['profiles'])}] {j['company']} | {j['title']} | {j['location']}")
        save_json(CACHE_FILE, cache)
        return

    use_claude = bool(CONFIG["scoring"].get("enabled", True)) and bool(os.getenv("ANTHROPIC_API_KEY"))
    batch = candidates[:CONFIG["scoring"]["max_jobs_per_run"]]
    if len(candidates) > len(batch):
        log(f"  {len(candidates) - len(batch)} משרות יחכו לריצה הבאה (מגבלת max_jobs_per_run)")
    if batch and use_claude:
        score_jobs(batch)
    elif batch:
        log("  דירוג Claude כבוי - נשלחת רשימה גולמית לדירוג ב-Cowork")
        for j in batch:
            j["profile"] = " / ".join(j["profiles"])

    today = date.today().isoformat()
    for j in batch:
        if not use_claude or j.get("score") is not None:
            seen[j["id"]] = today
    cutoff = (date.today() - timedelta(days=SEEN_RETENTION_DAYS)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}

    if use_claude:
        good = sorted((j for j in batch if j.get("score") is not None
                       and j["score"] >= MIN_SCORE[j["profile"]]),
                      key=lambda j: (j["profile"], -j["score"]))
    else:
        good = sorted(batch, key=lambda j: (j["profile"], j["company"]))
    write_review_files(good)
    notify(good, scored=use_claude)

    save_json(LATEST_FILE, [{k: v for k, v in j.items() if k != "description"} for j in good])
    save_json(SEEN_FILE, seen)
    save_json(CACHE_FILE, cache)
    save_json(DISCOVERED_FILE, discovered)


if __name__ == "__main__":
    main()
