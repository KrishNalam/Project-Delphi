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
    # Embedded Greenhouse jobs differ only by their id parameter: they must stay different postings.
    assert canonical("https://boards.greenhouse.io/embed/job_app?token=1") != \
        canonical("https://boards.greenhouse.io/embed/job_app?token=2")
    assert canonical("https://x.com/jobs?gh_jid=5&utm_source=li") == "https://x.com/jobs?gh_jid=5"
    assert _job("t", "A", "B", [], "https://x.com/jobs?gh_jid=1")["id"] != \
        _job("t", "A", "B", [], "https://x.com/jobs?gh_jid=2")["id"]


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
    assert parse_answers("4th year undergrad, 4 months, no", 3) == ["4th year undergrad", "4 months", "no"]
    assert parse_answers("No, never", 3) == ["No, never"] * 3
    # One question takes the whole reply: a multi-paragraph answer used to be rejected forever.
    assert parse_answers("I built a compiler.\n\nIt was fun.", 1) == ["I built a compiler.\n\nIt was fun."]
    assert parse_answers("1. A7Z9", 1) == ["A7Z9"]
    assert parse_answers("  ", 1) is None


def test_known_values():
    llm.PROFILE.update(first_name="Ada", last_name="Lovelace", email="ada@x.com", phone=4165550100)
    fields = [{"index": 0, "type": "text", "label": "Full Name*", "options": []},
              {"index": 1, "type": "email", "label": "Email Address", "options": []},
              {"index": 2, "type": "text", "label": "Other website", "options": []},
              {"index": 3, "type": "radio", "label": "Pronouns", "options": ["She/her", "He/him"]},
              {"index": 4, "type": "radio", "label": "Do you need sponsorship?", "options": ["Yes", "No"]}]
    got = known_values(fields, {"Pronouns": "she/her", "Do you need sponsorship?": "Maybe"})
    assert got == {0: "Ada Lovelace", 1: "ada@x.com", 3: "she/her"}, got  # 4: saved answer isn't an option
    # An answer saved under an empty label must not fill every unlabelled field.
    assert known_values([{"index": 0, "type": "text", "label": "", "options": []}], {"": "oops"}) == {}


def test_cost_tracking():
    assert abs(llm.cost_of("claude-haiku-5-5", 1_000_000, 0, 0, 1_000_000) - 0.60) < 1e-9
    assert abs(llm.cost_of("claude-opus-5-5", 0, 1_000_000, 1_000_000, 0) - (5.00 + 0.20)) < 1e-9
    path = Path(tempfile.mkdtemp()) / "usage.csv"
    usage = type("U", (), dict(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=None,
                               cache_read_input_tokens=3000))
    llm._log("map_fields", "claude-opus-5-5", usage, path)
    llm._log("rank", "claude-haiku-5-5", usage, path)
    summary = llm.usage_summary(path)
    assert "Per form page: avg $0.0086 over 1" in summary and "Daily digest: avg $0.0002 over 1" in summary, summary


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



def test_rank_dedupes():
    jobs = [job(title=f"Intern {i}") for i in range(3)]
    picks = llm.Ranking(picks=[llm.Pick(index=1, score=9, why="a"), llm.Pick(index=1, score=9, why="a"),
                               llm.Pick(index=7, score=5, why="out of range"), llm.Pick(index=0, score=6, why="b")])
    ask = llm._ask
    llm._ask = lambda *a, **k: picks
    try:
        assert [j["title"] for j in llm.rank(jobs, 5)] == ["Intern 1", "Intern 0"]
    finally:
        llm._ask = ask


def test_form_urls():
    from apply import FORM_LINK, direct_url
    from bot import fillable
    assert direct_url("https://careers.withwaymo.com/jobs?gh_jid=8306841") == \
        "https://boards.greenhouse.io/embed/job_app?token=8306841"
    assert direct_url("https://jobs.lever.co/x/1") == "https://jobs.lever.co/x/1"
    for url in ["https://job-boards.greenhouse.io/acme/jobs/123", "https://boards.greenhouse.io/embed/job_app?token=5",
                "https://jobs.lever.co/acme/71e99c38-aa29-4414-956d-cc526644b9b7",
                "https://jobs.ashbyhq.com/acme/b2726ac7-dd0d-42f3-93a5-139e18c90779",
                "https://apply.workable.com/rokt/j/616F2D0286", "https://x.wd1.myworkdayjobs.com/External/job/T/Intern_R1"]:
        assert FORM_LINK.search(url), url
    for url in ["https://boards.greenhouse.io/acme", "https://jobs.lever.co/acme", "https://acme.com/careers"]:
        assert not FORM_LINK.search(url), url  # whole job boards, not one posting
    assert fillable("https://apply.workable.com/rokt/j/616F2D0286/apply")
    assert fillable("https://x.wd5.myworkdayjobs.com/en-US/External/job/Intern_R1")
    assert not fillable("https://jobs.smartrecruiters.com/Bosch/1")


def test_answer_once():
    import asyncio

    import bot

    async def go():
        waiting = asyncio.create_task(bot._wait("text"))
        await asyncio.sleep(0)
        assert bot._answer("text", "first") and not bot._answer("text", "second")  # used to raise InvalidStateError
        assert not bot._answer("confirm", True)
        return await waiting
    assert asyncio.run(go()) == "first"
    assert not bot._answer("text", "nobody's waiting")


def test_auto_apply_picks():
    """/auto 8: only picks scoring 8+ on fillable sites start by themselves, and they get no Apply button."""
    import asyncio

    import bot
    picks = [dict(job(title="A Intern"), url="https://jobs.lever.co/a/1", score=9, why="x"),
             dict(job(title="B Intern"), url="https://jobs.lever.co/b/2", score=7, why="x"),
             dict(job(title="C Intern"), url="https://www.linkedin.com/jobs/view/3", score=9, why="x")]
    started, sent = [], []

    class App:
        class bot:  # noqa: N801
            async def send_message(chat, text, reply_markup=None, **kw):
                sent.append((text.split("</b>")[0][3:], reply_markup is not None, text.rsplit("· ", 1)[-1]))

        def create_task(coro):
            started.append(coro.cr_frame.f_locals["job"]["title"])
            coro.close()
    saved = bot.find_jobs, bot.get_setting
    bot.find_jobs = lambda: picks
    bot.get_setting = lambda key: 8 if key == "auto_min" else saved[1](key)
    try:
        assert asyncio.run(bot.send_digest(App))
    finally:
        bot.find_jobs, bot.get_setting = saved
    assert started == ["A Intern"], started
    assert sent == [("A Intern", False, "auto-applying"), ("B Intern", True, "auto-fill"),
                    ("C Intern", True, "fill it yourself")], sent


def test_answers_commands():
    import asyncio

    import apply
    import bot
    saved_path = apply.ANSWERS
    apply.ANSWERS = Path(tempfile.mkdtemp()) / "answers.json"
    replies = []

    class Update:
        class message:  # noqa: N801
            async def reply_text(text):
                replies.append(text)

    def run(fn, *args):
        asyncio.run(fn(Update, type("Ctx", (), {"args": list(args)})))
        return replies[-1]
    try:
        assert "No saved answers" in run(bot.cmd_answers)
        apply.save_answers({"Pronouns": "she/her", "Earliest start date?": "May 2027"})
        assert "1. Pronouns → she/her\n2. Earliest start date? → May 2027" in run(bot.cmd_answers)
        assert run(bot.cmd_answers, "2", "September", "2027") == "Changed: 2. Earliest start date? → September 2027"
        assert "Forgot: Pronouns" in run(bot.cmd_forget, "1")
        assert "no answer 5" in run(bot.cmd_answers, "5", "x")
        assert "/forget <number>" in run(bot.cmd_forget, "9")
        # A form that was open during /forget saves only its new answers, not its stale copy of the file.
        assert apply.save_answers({"GPA": "3.9"}) == {"Earliest start date?": "September 2027", "GPA": "3.9"}
    finally:
        apply.ANSWERS = saved_path


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
