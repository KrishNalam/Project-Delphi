"""Job sources. Each returns a list of jobs in the shape _job() builds."""
import hashlib
import json
import logging
import re
import time
import urllib.request

from config import PROFILE

log = logging.getLogger(__name__)

REPOS = ["SimplifyJobs/Summer2027-Internships", "vanshb03/Summer2027-Internships"]
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


def _job(source, company, title, locations, url, posted=None, sponsorship="Other", description="", countries=None):
    return {
        "id": hashlib.sha1(url.encode()).hexdigest()[:16],
        "source": source, "company": company, "title": title, "locations": locations or ["Unknown"],
        "url": url, "posted": posted or time.time(), "sponsorship": sponsorship, "description": description,
        "countries": countries or list({country_of(loc) for loc in locations}) or [None],
    }


def _get_json(url, headers=None):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=60) as r:
        return json.load(r)


def github_repos():
    """Community internship lists (SimplifyJobs listings.json format)."""
    cutoff = time.time() - MAX_AGE_DAYS * 86400
    jobs = []
    for repo in REPOS:
        try:
            listings = _get_json(f"https://raw.githubusercontent.com/{repo}/dev/.github/scripts/listings.json")
        except Exception as e:  # one dead source shouldn't kill the digest
            log.warning("%s: %s", repo, e)
            continue
        for x in listings:
            if x.get("active") and x.get("is_visible", True) and x.get("date_posted", 0) >= cutoff:
                jobs.append(_job(repo.split("/")[0], x["company_name"], x["title"], x.get("locations", []),
                                 x["url"], x["date_posted"], x.get("sponsorship", "Other")))
    return jobs


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
                jobs.append(_job(r["site"], r["company"], r["title"], [r["location"]], url,
                                 description=str(r.get("description", "")), countries=[country]))
    return jobs
