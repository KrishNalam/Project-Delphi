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
    # Degree and term, when the listing says and the profile asks.
    phd = job(degrees=["PhD"])
    assert keep(phd, both) and not keep(phd, both, degree="Bachelor's")
    assert keep(job(degrees=["Bachelor's", "Master's"]), both, degree="bachelor's")
    assert keep(job(), both, degree="Bachelor's")  # doesn't say: kept
    assert not keep(job(terms=["Winter 2027"]), both, terms=["Summer 2027"])
    assert keep(job(terms=["Summer"]), both, terms=["Summer 2027"])  # vanshb03's season
    assert keep(job(terms=["N/A"]), both, terms=["Summer 2027"])
    for mine in ["Bachelors", "BSc", "B.Eng", "undergraduate", "bachelor's degree"]:
        assert keep(job(degrees=["Bachelor's"]), both, degree=mine), mine
        assert not keep(phd, both, degree=mine), mine
    assert keep(job(degrees=["Master's"]), both, degree="MSc") and keep(phd, both, degree="Ph.D.")
    assert keep(job(terms=["Spring/Summer"]), both, terms=["Summer 2027"])


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
    import os
    os.environ["WORKDAY_PASSWORD"] = "Pw1!pass"
    assert fillable("https://x.wd5.myworkdayjobs.com/en-US/External/job/Intern_R1")
    del os.environ["WORKDAY_PASSWORD"]
    assert not fillable("https://x.wd5.myworkdayjobs.com/en-US/External/job/Intern_R1")  # can't make the account
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

        # /prep asks only what isn't saved yet, in one message, and saves the replies under those questions.
        msgs = []

        class Bot:
            async def send_message(chat, text, **kw):
                msgs.append(text)
                if text.startswith("Common application questions"):
                    n = text.count("\n")
                    asyncio.get_running_loop().call_later(
                        0.05, bot._answer, "text", "\n".join(f"{i}. a{i}" for i in range(1, n + 1)))
        apply.save_answers({bot.PREP_QUESTIONS[0]: "LinkedIn"})
        missing = [q for q in bot.PREP_QUESTIONS if q not in apply.load_answers()]
        asyncio.run(bot.prep(Bot, missing))
        saved = apply.load_answers()
        assert saved[bot.PREP_QUESTIONS[0]] == "LinkedIn" and saved[bot.PREP_QUESTIONS[1]] == "a1"
        assert all(q in saved for q in bot.PREP_QUESTIONS) and msgs[-1].startswith("Saved.")
    finally:
        apply.ANSWERS = saved_path


def test_sources_roll_over_and_warn():
    from datetime import datetime

    import bot
    import sources
    assert sources.repos(datetime(2027, 9, 1)) == [
        "SimplifyJobs/Summer2027-Internships", "SimplifyJobs/Summer2028-Internships",
        "vanshb03/Summer2027-Internships", "vanshb03/Summer2028-Internships"]
    store = {}
    saved = bot.get_setting, bot.set_setting
    bot.get_setting, bot.set_setting = (lambda k: store.get(k, {})), store.__setitem__
    try:
        assert bot.check_sources([job(), job()]) == []  # first run: nothing to compare with
        assert bot.check_sources([dict(job(), source="linkedin")]) == [
            "⚠️ test returned no jobs this time (it had 2 last time)."]
        assert store["source_counts"] == {"linkedin": 1}
    finally:
        bot.get_setting, bot.set_setting = saved


def test_blocked():
    from filters import block_term, blocked
    terms = [block_term(t) for t in ["Amazon", "PhD", "Ph.D", "Machine Learning"]]
    assert terms == ["amazon", "phd", "ph d", "machine learning"]
    assert blocked(_job("t", "Amazon", "SDE Intern", [], "u"), terms) == "amazon"
    assert blocked(_job("t", "Acme", "Research Intern (PhD)", [], "u"), terms) == "phd"
    assert blocked(_job("t", "Acme", "Ph.D. Research Intern", [], "u"), terms) == "ph d"
    assert blocked(_job("t", "Acme", "Machine Learning Intern", [], "u"), terms) == "machine learning"
    assert blocked(_job("t", "Amazon Web Services", "Intern", [], "u"), terms) == "amazon"
    # Whole words only, and a phrase can't run from the company into the title.
    assert blocked(_job("t", "Metamorph", "Intern", [], "u"), ["meta"]) is None
    assert blocked(_job("t", "Acme", "Graphdb Intern", [], "u"), ["phd"]) is None
    assert blocked(_job("t", "Machine", "Learning Intern", [], "u"), ["machine learning"]) is None


def test_week():
    import asyncio

    import bot
    import store
    saved = store.DB, store.APPLIED_CSV
    store.DB = store.open_db(":memory:")
    store.APPLIED_CSV = Path(tempfile.mkdtemp()) / "applied.csv"
    sent = []

    class Bot:
        async def send_message(chat, text, reply_markup=None, **kw):
            sent.append((text, reply_markup and [b.callback_data for b in reply_markup.inline_keyboard[0]]))
    try:
        done, stuck, finished = (_job("t", "Acme", t, [], f"https://acme.com/{t}") for t in ("A", "B", "C"))
        for j, status in [(done, "submitted"), (stuck, "handoff"), (finished, "handoff")]:
            store.save_job(j)
            store.set_status(j["id"], status)
        log_application(done, "submitted")
        log_application(finished, "manual")  # you finished it after the bot handed it over: not listed again
        asyncio.run(bot.send_week(Bot))
    finally:
        store.DB, store.APPLIED_CSV = saved
    assert sent[0][0].startswith("This week: 2 applications (1 submitted by me, 1 finished by you)"), sent
    assert "1 I couldn't finish" in sent[0][0]
    assert sent[1][0].startswith("<b>B</b> · Acme") and sent[1][1] == [f"done:{stuck['id']}", f"skip:{stuck['id']}"]
    assert len(sent) == 2


def test_resume_interrupted():
    import asyncio

    import bot
    import store
    saved = store.DB, store.APPLIED_CSV
    store.DB = store.open_db(":memory:")
    store.APPLIED_CSV = Path(tempfile.mkdtemp()) / "applied.csv"
    sent = []

    class App:
        class bot:  # noqa: N801
            async def send_message(chat, text, reply_markup=None, **kw):
                sent.append((text, [b.callback_data for b in reply_markup.inline_keyboard[0]]))
    try:
        cut, waiting, sending, done = (_job("t", "Acme", t, [], f"https://acme.com/{t}")
                                       for t in ("Cut off", "Queued", "Sending", "Got there"))
        for j, status in [(cut, "applying"), (waiting, "queued"), (sending, "submitting"), (done, "applying")]:
            store.save_job(j)
            store.set_status(j["id"], status)
        log_application(done, "submitted")  # crashed after logging: nothing to redo
        asyncio.run(bot.resume_interrupted(App))
        assert store.jobs_with_status(("queued", "applying", "submitting"), 30) == []
        assert store.job_status(done["id"]) == "submitted"
    finally:
        store.DB, store.APPLIED_CSV = saved
    assert sorted(sent) == sorted([
        ("I restarted before finishing Cut off @ Acme, so it wasn't sent.", [f"apply:{cut['id']}", f"skip:{cut['id']}"]),
        ("I restarted before finishing Queued @ Acme, so it wasn't sent.",
         [f"apply:{waiting['id']}", f"skip:{waiting['id']}"]),
        # Might have gone through: no plain retry offered first.
        ("I restarted just after submitting Sending @ Acme. It may have gone through: check your email.",
         [f"done:{sending['id']}", f"apply:{sending['id']}"])]), sent


def test_resume_variants():
    import apply
    tmp = Path(tempfile.mkdtemp())
    for name in ("main", "ml", "hw"):
        (tmp / f"{name}.pdf").write_bytes(b"%PDF")
    saved = dict(apply.PROFILE)
    apply.PROFILE.update(resume_pdf=str(tmp / "main.pdf"), resumes=[
        {"pdf": str(tmp / "ml.pdf"), "keywords": ["Machine Learning", "ML", "AI"]},
        {"pdf": str(tmp / "hw.pdf"), "keywords": ["embedded", "C++"]}])
    try:
        pick = lambda title: apply.resume_path({"title": title}).stem  # noqa: E731
        assert pick("Machine Learning Engineer Intern") == "ml"
        assert pick("ML/AI Intern") == "ml"
        assert pick("Embedded Software Co-op") == "hw" and pick("C++ Developer Intern") == "hw"
        assert pick("Software Engineer Intern") == "main"
        assert pick("Email Marketing Intern") == "main"  # "ai" inside "email" isn't the word AI
        assert apply.resume_path().stem == "main"
    finally:
        apply.PROFILE.clear()
        apply.PROFILE.update(saved)


def test_due_slot():
    from datetime import datetime

    import bot
    saved = bot.DIGEST_HOURS
    bot.DIGEST_HOURS = [8, 17]
    try:
        at = lambda h: datetime(2026, 10, 29, h)  # noqa: E731
        assert bot.due_slot(at(7), "") is None
        assert bot.due_slot(at(8), "2026-10-28@17") == "2026-10-29@8"
        assert bot.due_slot(at(12), "2026-10-29@8") is None
        assert bot.due_slot(at(17), "2026-10-29@8") == "2026-10-29@17"
        assert bot.due_slot(at(23), "2026-10-29@17") is None
        assert bot.due_slot(at(20), "2026-10-28@17") == "2026-10-29@17"  # asleep all day: one catch-up, not two
        assert bot.due_slot(at(9), "2026-10-29") is None  # last_digest from before slots: 8:00 already ran
        assert bot.due_slot(at(17), "2026-10-29") == "2026-10-29@17"
    finally:
        bot.DIGEST_HOURS = saved


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
