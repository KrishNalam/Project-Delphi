"""Self-check for the rule logic. Run: python test_delphi.py"""
import tempfile
from pathlib import Path

from bot import parse_answers
from filters import NO_SPONSOR, keep
from sources import _job, country_of
from store import already_applied, log_application


def job(title="Software Engineering Intern", locations=("Austin, TX",), **kw):
    return _job("test", "Acme", title, list(locations), "https://acme.com/jobs/1", **kw)


def test_country_of():
    assert country_of("Toronto, ON") == "canada"
    assert country_of("Kanata, Ottawa, ON, Canada") == "canada"
    assert country_of("Vancouver, BC") == "canada"
    assert country_of("Vancouver, WA") == "usa"
    assert country_of("Remote in USA") == "usa"
    assert country_of("Idaho") == "usa"
    assert country_of("Cambridge, UK") == "other"
    assert country_of("Remote") is None


def test_keep():
    both = ["canada", "usa"]
    assert keep(job(), both)
    assert not keep(job(title="Senior Software Engineer"), both)
    assert not keep(job(title="International Sales Manager"), both)
    assert keep(job(title="Software Developer Co-op", locations=["Toronto, ON"]), ["canada"])
    assert not keep(job(), ["canada"])
    assert not keep(job(sponsorship="U.S. Citizenship is Required"), both)
    assert keep(job(sponsorship="U.S. Citizenship is Required"), both, needs_us_sponsorship=False)
    assert not keep(job(description="We are unable to provide visa sponsorship for this role."), both)
    # Sponsorship only matters for the US side of a multi-country posting.
    assert keep(job(sponsorship="U.S. Citizenship is Required", locations=["Austin, TX", "Toronto, ON"]), both)
    assert keep(job(locations=["Remote"]), ["canada"])


def test_no_sponsor_phrases():
    for s in ["No sponsorship available", "We will not provide sponsorship", "Must be a U.S. citizen",
              "without the need for current or future visa sponsorship", "Requires an active security clearance"]:
        assert NO_SPONSOR.search(s), s
    assert not NO_SPONSOR.search("We offer visa sponsorship to interns.")


def test_already_applied():
    path = Path(tempfile.mkdtemp()) / "applied.csv"
    first = _job("test", "Acme Corp", "SWE Intern", ["Toronto, ON"], "https://acme.com/jobs/1")
    assert already_applied(first, path) is None
    log_application(first, "submitted", path)
    assert already_applied(first, path)["company"] == "Acme Corp"
    # Same role reposted under another URL still counts; a different role doesn't.
    assert already_applied(_job("test", "ACME corp", "SWE Intern ", [], "https://other.com/2"), path)
    assert already_applied(_job("test", "Acme Corp", "Data Intern", [], "https://acme.com/jobs/3"), path) is None


def test_parse_answers():
    assert parse_answers("1. Yes\n2) 2028-04", 2) == ["Yes", "2028-04"]
    assert parse_answers("just one", 1) == ["just one"]
    assert parse_answers("1. only one", 2) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
