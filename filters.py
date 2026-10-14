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


def keep(job, countries, needs_us_sponsorship=True):
    """Internship-ish title, in an enabled country, and (for the US) not ruling out sponsorship."""
    if not INTERN.search(job["title"]):
        return False
    ca_ok = "canada" in countries
    us_ok = "usa" in countries and not (needs_us_sponsorship and (
        job["sponsorship"] in BAD_SPONSORSHIP or NO_SPONSOR.search(job["description"])))
    places = set(job["countries"])
    return ("canada" in places and ca_ok) or ("usa" in places and us_ok) or (None in places and (ca_ok or us_ok))
