"""Self-check for the rule logic. Run: python test_delphi.py"""
import tempfile
from pathlib import Path

import llm
from apply import known_values
from bot import parse_answers
from filters import NO_SPONSOR, keep, relevance, words
from sources import _job, country_of
from store import already_applied, canonical, log_application


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


def test_relevance():
    resume = words("Python backend developer. Built machine learning pipelines and software tools.")
    score = lambda title: relevance(job(title=title), resume)  # noqa: E731
    assert score("Software Engineering Intern") > score("Mechanical Engineering Intern")
    assert score("Machine Learning Intern") > score("Marketing Intern")


def test_no_sponsor_phrases():
    for s in ["No sponsorship available", "We will not provide sponsorship", "Must be a U.S. citizen",
              "without the need for current or future visa sponsorship", "Requires an active security clearance"]:
        assert NO_SPONSOR.search(s), s
    assert not NO_SPONSOR.search("We offer visa sponsorship to interns.")


def test_canonical():
    base = "https://jobs.ashbyhq.com/1password/b2726ac7"
    for url in [base, base + "/", base + "/application", base + "/application?embed=true", base + "?src=x"]:
        assert canonical(url) == base, url
    assert canonical("https://jobs.lever.co/x/123/apply") == "https://jobs.lever.co/x/123"


def test_already_applied():
    path = Path(tempfile.mkdtemp()) / "applied.csv"
    first = _job("test", "Acme Corp", "SWE Intern", ["Toronto, ON"], "https://acme.com/jobs/1")
    assert already_applied(first, path) is None
    log_application(first, "submitted", path)
    assert already_applied(first, path)["company"] == "Acme Corp"
    # Same role reposted under another URL still counts; a different role doesn't.
    assert already_applied(_job("test", "ACME corp", "SWE Intern ", [], "https://other.com/2"), path)
    assert already_applied(_job("test", "Acme", "Renamed", [], "https://acme.com/jobs/1/apply?ref=li"), path)
    assert already_applied(_job("test", "Acme Corp", "Data Intern", [], "https://acme.com/jobs/3"), path) is None


def test_parse_answers():
    assert parse_answers("1. Yes\n2) 2028-04", 2) == ["Yes", "2028-04"]
    assert parse_answers("just one", 1) == ["just one"]
    assert parse_answers("1. only one", 2) is None
    assert parse_answers("April 2028", 2) == ["April 2028", "April 2028"]


def test_known_values():
    llm.PROFILE.update(first_name="Ada", last_name="Lovelace", email="ada@x.com", phone=4165550100)
    fields = [{"index": 0, "type": "text", "label": "Full Name*", "options": []},
              {"index": 1, "type": "email", "label": "Email Address", "options": []},
              {"index": 2, "type": "text", "label": "Other website", "options": []},
              {"index": 3, "type": "radio", "label": "Pronouns", "options": ["She/her", "He/him"]},
              {"index": 4, "type": "radio", "label": "Do you need sponsorship?", "options": ["Yes", "No"]}]
    got = known_values(fields, {"Pronouns": "she/her", "Do you need sponsorship?": "Maybe"})
    assert got == {0: "Ada Lovelace", 1: "ada@x.com", 3: "she/her"}, got  # 4: saved answer isn't an option


def test_cost_tracking():
    assert abs(llm.cost_of("claude-haiku-5-5", 1_000_000, 0, 0, 1_000_000) - 0.60) < 1e-9
    assert abs(llm.cost_of("claude-opus-5-5", 0, 1_000_000, 1_000_000, 0) - (5.00 + 0.20)) < 1e-9
    path = Path(tempfile.mkdtemp()) / "usage.csv"
    usage = type("U", (), dict(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=None,
                               cache_read_input_tokens=3000))
    llm._log("map_fields", "claude-opus-5-5", usage, path)
    llm._log("rank", "claude-haiku-5-5", usage, path)
    summary = llm.usage_summary(path)
    assert "Per application: avg $0.0086 over 1" in summary and "Daily digest: avg $0.0002 over 1" in summary, summary


def test_box_choice():
    from apply import _box_choice

    def box(label, option, required=False):
        return {"label": label, "options": [option], "required": required}
    assert _box_choice(box("I understand that offers are conditional on a background check.", "I understand")) == "tick"
    assert _box_choice(box("Do you agree to allow 1Password to contact you about job opportunities?", "Yes")) == "tick"
    assert _box_choice(box("I have read and consent to these terms and conditions.*", "Yes", True)) == "tick"
    assert _box_choice(box("Interested in careers at 1Password? Check this box to sign up.", "Yes")) == "leave"
    assert _box_choice(box("I currently work here", "I currently work here")) == "leave"
    assert _box_choice(box("Send me job alerts and promotions", "Yes")) == "leave"
    assert _box_choice(box("Are you open to relocation?", "Yes")) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
