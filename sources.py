"""Job sources. Each returns a list of jobs in the shape _job() builds."""
import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape

from config import PROFILE, env
from store import canonical

log = logging.getLogger(__name__)

LIST_OWNERS = ["SimplifyJobs", "vanshb03"]  # each starts a Summer<year>-Internships repo every August
MAX_AGE_DAYS = 7

PROVINCES = "ON|QC|BC|AB|MB|SK|NS|NB|NL|PE|YT|NT|NU"
STATES = ("AL|AK|AZ|AR|CA|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|"
          "NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY")
STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida",
    "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska",
    "nevada", "new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee", "texas",
    "utah", "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming",
}
SEARCH_COUNTRY = {"canada": ("Canada", "Canada"), "usa": ("United States", "USA")}  # (location, Indeed country)


def country_of(location):
    """'canada', 'usa', 'other', or None when it can't tell (e.g. bare 'Remote')."""
    low = location.lower()
    if "canada" in low or re.search(rf",\s*({PROVINCES})\b", location):
        return "canada"
    if (re.search(r"\b(usa|united states)\b|\bu\.s\.", low) or re.search(rf",\s*({STATES})\b", location)
            or low in STATE_NAMES or location.startswith("US ")):
        return "usa"
    return None if "remote" in low else "other"


def _job(source, company, title, locations, url, posted=None, sponsorship="Other", description="", countries=None,
         degrees=(), terms=()):
    """degrees / terms: what the listing says it's open to ("PhD", "Summer 2027"); empty when it doesn't say."""
    return {
        "id": hashlib.sha1(canonical(url).encode()).hexdigest()[:16],
        "source": source, "company": company, "title": title, "locations": locations or ["Unknown"],
        "url": url, "posted": posted or time.time(), "sponsorship": sponsorship, "description": description,
        "countries": countries or list({country_of(loc) for loc in locations}) or [None],
        "degrees": list(degrees), "terms": [t for t in terms if t and t != "N/A"],
    }


def _get_json(url, headers=None):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=60) as r:
        return json.load(r)


def repos(today=None):
    """This year's and next year's lists, so the bot moves on to Summer2028 by itself when those repos appear."""
    year = (today or datetime.now()).year
    return [f"{owner}/Summer{y}-Internships" for owner in LIST_OWNERS for y in (year, year + 1)]


def github_repos():
    """Community internship lists (SimplifyJobs listings.json format)."""
    cutoff = time.time() - MAX_AGE_DAYS * 86400
    jobs = []
    for repo in repos():
        try:
            listings = _get_json(f"https://raw.githubusercontent.com/{repo}/dev/.github/scripts/listings.json")
        except urllib.error.HTTPError as e:
            if e.code != 404:  # 404: that year's list doesn't exist (yet, or any more)
                log.warning("%s: %s", repo, e)
            continue
        except Exception as e:  # one dead source shouldn't kill the digest
            log.warning("%s: %s", repo, e)
            continue
        for x in listings:
            if x.get("active") and x.get("is_visible", True) and x.get("date_posted", 0) >= cutoff:
                jobs.append(_job(repo.split("/")[0], x["company_name"], x["title"], x.get("locations", []),
                                 x["url"], x["date_posted"], x.get("sponsorship", "Other"),
                                 degrees=x.get("degrees") or [],
                                 terms=x.get("terms") or ([x["season"]] if x.get("season") else [])))
    # Last year's repo can mirror this year's listings: one job per posting.
    return list({(job["source"], job["id"]): job for job in jobs}.values())


def jobspy_jobs(countries):
    """LinkedIn + Indeed via python-jobspy. Guest listings only, we never log in to LinkedIn."""
    from jobspy import scrape_jobs  # pulls in pandas, so only import when used

    jobs = []
    for country in countries:
        location, indeed_country = SEARCH_COUNTRY[country]
        for term in PROFILE.get("search_terms", ["software engineering intern"]):
            try:
                df = scrape_jobs(site_name=["linkedin", "indeed"], search_term=term, location=location,
                                 country_indeed=indeed_country, results_wanted=25, hours_old=72)
            except Exception as e:
                log.warning("jobspy %s / %s: %s", country, term, e)
                continue
            for r in df.fillna("").to_dict("records"):
                # Prefer the company's own posting so we apply there, not through LinkedIn.
                url = r.get("job_url_direct") or r["job_url"]
                posted = r.get("date_posted")
                posted = time.mktime(posted.timetuple()) if hasattr(posted, "timetuple") else None
                jobs.append(_job(r["site"], r["company"], r["title"], [r["location"]] if r["location"] else [], url,
                                 posted, description=str(r.get("description", "")), countries=[country]))
    return jobs


def _text(html):
    """Plain text that keeps links, so Claude can see where to apply: <a href="u">Title</a> -> Title (u)."""
    html = re.sub(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r"\2 (\1)", html or "", flags=re.S | re.I)
    html = re.sub(r"<(br|/p|/li|/tr|/h\d)[^>]*>", "\n", html, flags=re.I)
    return re.sub(r"[ \t]+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def _time(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def canvas_jobs(days=2):
    """Job posts from Quercus (Canvas) from the last `days`: course announcements and pages, plus
    replies in discussion threads like the CS Career and Research Opportunities board."""
    base, token = env("CANVAS_URL"), env("CANVAS_TOKEN")
    courses = [c.strip() for c in env("CANVAS_COURSE_IDS", "").split(",") if c.strip()]
    threads = [t.strip() for t in env("CANVAS_DISCUSSIONS", "").split(",") if t.strip()]  # "course/topic"
    if not (base and token and (courses or threads)):
        return []
    import llm

    api, headers = f"{base.rstrip('/')}/api/v1", {"Authorization": f"Bearer {token}"}
    since = datetime.now(timezone.utc) - timedelta(days=days)
    texts = []
    for course in courses:
        try:
            for a in _get_json(f"{api}/announcements?context_codes[]=course_{course}&start_date={since.date()}",
                               headers):
                texts.append(f"{a['title']}\n{_text(a['message'])}")
            for p in _get_json(f"{api}/courses/{course}/pages?sort=updated_at&order=desc&per_page=20", headers):
                if _time(p["updated_at"]) >= since:
                    page = _get_json(f"{api}/courses/{course}/pages/{p['url']}", headers)
                    texts.append(f"{page['title']}\n{_text(page.get('body'))}")
        except Exception as e:
            log.warning("canvas course %s: %s", course, e)
    for thread in threads:
        course, _, topic = thread.partition("/")
        try:
            # ponytail: newest 50 replies only; plenty for a daily scan, raise per_page for long lookbacks
            for entry in _get_json(f"{api}/courses/{course}/discussion_topics/{topic}/entries?per_page=50", headers):
                if _time(entry["created_at"]) >= since:
                    texts.append(_text(entry.get("message")))
        except Exception as e:
            log.warning("canvas discussion %s: %s", thread, e)
    if not texts:
        return []
    postings = llm.extract_jobs("\n\n---\n\n".join(texts))
    return [_job("quercus", p.company, p.title, [p.location] if p.location else [], p.url)
            for p in postings if p.url]
