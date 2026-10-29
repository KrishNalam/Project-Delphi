"""Browser self-check: fills a local form that mimics the tricky parts of real ones, then submits it.
No network or API key needed (Claude is stubbed). Run: python test_forms.py"""
import asyncio
import json
import tempfile
from pathlib import Path

import apply
import llm
from config import PROFILE

# Greenhouse-style dropdown next to a hidden phone-country list (whose options must be ignored), a question marked
# required only by CSS (Ashby), a site-guessed prefill (Workable's geo-IP address), a cookie banner over Submit,
# and an emailed code to type in after submitting (one character per box).
FORM = """<!doctype html><html><head><style>
  .req::after { content: "*"; color: red }
  #cookies { position: fixed; bottom: 0; left: 0; right: 0; height: 200px; background: #333 }
</style></head><body>
<div id="cookies"><button onclick="this.parentElement.remove()">Decline all</button></div>
<ul style="display:none"><li role="option">Canada +1</li><li role="option">Yes</li></ul>
<h2>About the role</h2><p>Acme builds rocket-powered roller skates for road runners.</p>
<form onsubmit="event.preventDefault(); askCode()">
  <label for="first">First Name*</label><input id="first" required>
  <label for="email">Email*</label><input id="email" type="email" required>
  <label for="city">Address*</label><input id="city" value="Whitby, Canada" required>
  <div><label class="req">Are you authorized to work in Canada?</label>
    <div class="select__control"><input role="combobox" id="auth" autocomplete="off"></div>
    <div id="auth-menu" role="listbox" style="display:none"></div></div>
  <div><label class="req" for="grad">Graduation date</label><input id="grad" placeholder="MM/DD/YYYY"></div>
  <fieldset><legend>Pronouns</legend>
    <label><input type="radio" name="p" value="she" required>She/her</label><label><input type="radio" name="p" value="he">He/him</label>
  </fieldset>
  <label for="resume">Resume</label><input type="file" id="resume">
  <label for="cover">Cover Letter*</label><input type="file" id="cover" required>
  <label for="why">Why Acme?</label><textarea id="why"></textarea>
  <button type="submit">Submit application</button>
</form>
<div style="height:400px"></div>
<script>
  const auth = document.getElementById('auth'), menu = document.getElementById('auth-menu');
  auth.addEventListener('focus', () => {
    menu.innerHTML = ['Yes', 'No', 'Not sure'].map(o => `<div role="option">${o}</div>`).join('');
    menu.style.display = 'block';
    for (const o of menu.children) o.onclick = () => {
      auth.value = ''; menu.style.display = 'none';
      auth.parentElement.querySelector('.select__single-value')?.remove();
      auth.insertAdjacentHTML('beforebegin', `<div class="select__single-value">${o.innerText}</div>`);
      window.picked = o.innerText;
    };
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') menu.style.display = 'none'; });
  function askCode() {
    document.body.innerHTML = '<form onsubmit="event.preventDefault(); check()"><p>Enter the code sent to your email</p>'
      + [0, 1, 2].map(i => `<label for="c${i}">Security code</label><input id="c${i}" maxlength="1">`).join('')
      + '<button type="submit">Submit</button></form>';
    window.check = () => {
      const code = [0, 1, 2].map(i => document.getElementById('c' + i).value).join('');
      document.body.innerHTML = code === 'A7Z' ? '<h1>Thank you for applying!</h1>' : '<h1>Wrong code</h1>';
    };
  }
</script></body></html>"""


def test_fill_and_submit():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "form.html").write_text(FORM)
    (tmp / "resume.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    PROFILE.update(first_name="Ada", email="ada@x.com", city="Toronto, ON, Canada", resume_pdf=str(tmp / "resume.pdf"))
    apply.ANSWERS = tmp / "answers.json"
    apply.SUPPORTED = apply.re.compile(r"^file:")
    seen = {}

    def fake_map(job, fields, answers, posting=""):
        seen.update({f["label"]: f for f in fields})
        seen["posting"] = posting
        values = {"Are you authorized to work in Canada?": "Yes", "Graduation date": "04/30/2028",
                  "Why Acme?": "Skates."}
        return llm.FormPlan(fills=[llm.Fill(index=f["index"], value=values[f["label"]])
                                   for f in fields if f["label"] in values], unknown=[])
    llm.map_fields = fake_map
    llm.cover_letter = lambda job, posting, answers: seen.setdefault("letter_posting", posting) and [
        "I build things that go fast.", "Second paragraph."]
    apply.ROOT = tmp
    asked, filled = [], {}

    async def ask(questions):
        asked.extend(questions)
        return ["A7Z" if "code" in q else "She/her" for q in questions]

    async def confirm(png):
        page = apply._last_page
        filled.update(await page.evaluate("""() => {
            const $ = id => document.getElementById(id);
            return {auth: window.picked, city: $('city').value, grad: $('grad').value, first: $('first').value,
                    pronoun: document.querySelector('[name=p]:checked')?.value, resume: $('resume').files[0]?.name,
                    cover: $('cover').files[0]?.name};
        }"""))
        return True

    async def stuck(reason, png):
        raise AssertionError(reason)

    open_page = apply.open_page

    async def remember(p, url):  # keep a handle on the page so confirm() can look at it
        apply._last_page = await open_page(p, url)
        return apply._last_page
    apply.open_page = remember
    try:
        status = asyncio.run(apply.apply({"id": "test-form", "title": "Intern", "company": "Acme", "url": (tmp / "form.html").as_uri()},
                                         ask, confirm, stuck))
    finally:
        apply.open_page = open_page
    auth = seen["Are you authorized to work in Canada?"]
    assert auth["options"] == ["Yes", "No", "Not sure"] and auth["required"], auth  # visible choices only, CSS '*'
    assert seen["Graduation date"]["placeholder"] == "MM/DD/YYYY"
    assert filled == {"auth": "Yes", "city": "Toronto, ON, Canada", "grad": "04/30/2028", "first": "Ada",
                      "pronoun": "she", "resume": "resume.pdf", "cover": "acme-intern.pdf"}, filled
    # The essay question and the cover letter both got the job ad's text.
    assert "rocket-powered roller skates" in seen["posting"] and "rocket-powered" in seen["letter_posting"]
    letter = tmp / "cover_letters" / "acme-intern.pdf"
    assert letter.read_bytes().startswith(b"%PDF") and letter.stat().st_size > 1000
    assert asked[0].startswith("Pronouns") and "verification code" in asked[1], asked
    assert json.loads(apply.ANSWERS.read_text()) == {"Pronouns": "She/her"}  # the code isn't saved as an answer
    assert status == "submitted", status


def test_bot_flow():
    """Two Apply taps at once through bot.run_apply with a fake Telegram: the second waits its turn, questions and
    the Submit tap are answered from 'chat', and both end up in applied.csv."""
    import bot
    import store

    tmp = Path(tempfile.mkdtemp())
    store.APPLIED_CSV = tmp / "applied.csv"
    bot.ROOT = tmp
    PROFILE.update(first_name="Ada", email="ada@x.com", city="Toronto", resume_pdf=str(tmp / "resume.pdf"))
    (tmp / "resume.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    apply.ANSWERS = tmp / "answers.json"
    apply.SUPPORTED = apply.re.compile(r"^file:")
    llm.map_fields = lambda job, fields, answers, posting="": llm.FormPlan(fills=[], unknown=[])
    llm.cover_letter = lambda job, posting, answers: ["Hello."]
    sent, stale, cleared, letters = [], [], [], []

    class FakeBot:
        async def send_message(self, chat, text, **kw):
            sent.append(text)
            # Reply like you would: answers for questions, a code when one is asked for.
            if "verification code" in text:
                asyncio.get_running_loop().call_later(0.1, bot._answer, "text", "A7Z")
            elif "needs a few answers" in text or text.startswith("Acme"):
                asyncio.get_running_loop().call_later(0.1, bot._answer, "text", "Yes, 04/30/2028, She/her")

        async def send_document(self, chat, png, caption="", reply_markup=None, **kw):
            sent.append(caption)
            if caption.startswith("Cover letter for"):
                letters.append(png[:4])
                return
            if not caption.startswith("Submit this?"):
                raise AssertionError(caption)
            kind, tag, _ = reply_markup.inline_keyboard[0][0].callback_data.split(":")
            loop = asyncio.get_running_loop()
            # A tap on an older message's Submit (another tag) must not count; the real one does.
            loop.call_later(0.05, lambda: stale.append(bot._answer(kind, True, "0ld0ld00")))
            loop.call_later(0.1, bot._answer, kind, True, tag)

            class Message:
                async def edit_reply_markup(self, markup):
                    cleared.append(markup)
            return Message()

    jobs = []
    for i, title in enumerate(["SWE Intern", "Data Intern"]):
        (tmp / f"form{i}.html").write_text(FORM)
        jobs.append(bot.sources._job("test", "Acme", title, ["Toronto, ON"], (tmp / f"form{i}.html").as_uri()))

    async def go():
        fake = FakeBot()
        await asyncio.gather(bot.run_apply(jobs[0], fake), bot.run_apply(jobs[1], fake))
    asyncio.run(go())
    assert any(m.startswith("Queued Data Intern @ Acme") for m in sent), sent
    assert [m for m in sent if m.startswith("Submitted")] == ["Submitted ✅ SWE Intern @ Acme",
                                                              "Submitted ✅ Data Intern @ Acme"], sent
    assert [r["title"] for r in store.applied_rows()] == ["SWE Intern", "Data Intern"]
    assert bot.running is None and bot.queued == 0 and not bot.apply_lock.locked()
    assert stale == [False, False] and cleared == [None, None]  # stale taps ignored, buttons removed after use
    assert letters == [b"%PDF", b"%PDF"]  # each application's cover letter was sent to read before Submit
    assert sorted(f.name[11:] for f in (tmp / "applications").iterdir()) == [
        "acme-data-intern.png", "acme-swe-intern.png"]  # a copy of each filled form, as sent


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
