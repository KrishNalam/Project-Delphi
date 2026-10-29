"""Rule-based filtering. Cheap and predictable, runs before any LLM call."""
import re

INTERN = re.compile(r"\bintern(ship)?s?\b|\bco-?op\b|\bstudent\b|\bplacement\b", re.I)
# Simplify's own sponsorship tags, plus phrases in free-text descriptions (LinkedIn/Indeed).
BAD_SPONSORSHIP = {"Does Not Offer Sponsorship", "U.S. Citizenship is Required"}
NO_SPONSOR = re.compile(
    r"no (visa )?sponsorship"
    r"|(not|unable to|cannot|won't|will not) (be able to )?(provide|offer|support) (visa |immigration )?sponsorship"
    r"|without (the need for )?(current or future )?(visa )?sponsorship"
    r"|u\.?s\.? citizenship (is )?required|must be (a )?u\.?s\.? citizens?|security clearance",
    re.I,
)


def keep(job, countries, needs_us_sponsorship=True, degree=None, terms=()):
    """Internship-ish title, in an enabled country, and (for the US) not ruling out sponsorship. With `degree`
    ("Bachelor's") or `terms` (["Summer 2027"]) from the profile, listings that say they're for other degrees
    (PhD-only) or other terms are dropped; ones that don't say are kept."""
    if not INTERN.search(job["title"]):
        return False
    if degree and job.get("degrees") and degree.lower() not in (d.lower() for d in job["degrees"]):
        return False
    listed = [t.lower() for t in job.get("terms", [])]
    # "Summer" (vanshb03's season) and "Summer 2027" (Simplify's term) match either way round.
    if terms and listed and not any(a in b or b in a for a in (t.lower() for t in terms) for b in listed):
        return False
    ca_ok = "canada" in countries
    us_ok = "usa" in countries and not (needs_us_sponsorship and (
        job["sponsorship"] in BAD_SPONSORSHIP or NO_SPONSOR.search(job["description"])))
    places = set(job["countries"])
    return ("canada" in places and ca_ok) or ("usa" in places and us_ok) or (None in places and (ca_ok or us_ok))

def blocked(job, terms):
    """The /block term that rules this job out: whole words of the company name ("amazon" covers Amazon Web
    Services) or of the title ("phd"). Else None."""
    company, title = (f" {block_term(job[k])} " for k in ("company", "title"))
    return next((t for t in terms if f" {t} " in company or f" {t} " in title), None)


def block_term(text):
    """How /block stores what you typed: lowercase words, punctuation as spaces ("Ph.D" -> "ph d")."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


# Words every internship title shares; they say nothing about fit.
GENERIC = {"intern", "interns", "internship", "co", "op", "coop", "student", "summer", "fall", "winter", "spring",
           "and", "the", "of", "for", "to", "in", "with", "a", "an", "at", "on", "engineer", "engineering", "program",
           "team", "new", "grad", "i", "ii", "iii", "us", "usa", "canada", "remote", "month", "months", "term"}


def words(text):
    return set(re.findall(r"[a-z][a-z0-9+#]*", text.lower())) - GENERIC


def relevance(job, resume_words):
    """Free pre-rank before Claude: how many title words appear in the resume."""
    # ponytail: title-only keyword overlap; descriptions are missing for most listings and full of filler.
    # Swap for embeddings if good jobs keep getting cut before ranking.
    return len(words(job["title"]) & resume_words)
