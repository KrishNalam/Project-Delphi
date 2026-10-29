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

    def fake_map(job, fields, answers):
        seen.update({f["label"]: f for f in fields})
        values = {"Are you authorized to work in Canada?": "Yes", "Graduation date": "04/30/2028"}
        return llm.FormPlan(fills=[llm.Fill(index=f["index"], value=values[f["label"]])
                                   for f in fields if f["label"] in values], unknown=[])
    llm.map_fields = fake_map
    asked, filled = [], {}

    async def ask(questions):
        asked.extend(questions)
        return ["A7Z" if "code" in q else "She/her" for q in questions]

    async def confirm(png):
        page = apply._last_page
        filled.update(await page.evaluate("""() => {
            const $ = id => document.getElementById(id);
            return {auth: window.picked, city: $('city').value, grad: $('grad').value, first: $('first').value,
                    pronoun: document.querySelector('[name=p]:checked')?.value, resume: $('resume').files.length};
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
        status = asyncio.run(apply.apply({"title": "Intern", "company": "Acme", "url": (tmp / "form.html").as_uri()},
                                         ask, confirm, stuck))
    finally:
        apply.open_page = open_page
    auth = seen["Are you authorized to work in Canada?"]
    assert auth["options"] == ["Yes", "No", "Not sure"] and auth["required"], auth  # visible choices only, CSS '*'
    assert seen["Graduation date"]["placeholder"] == "MM/DD/YYYY"
    assert filled == {"auth": "Yes", "city": "Toronto, ON, Canada", "grad": "04/30/2028", "first": "Ada",
                      "pronoun": "she", "resume": 1}, filled
    assert asked[0].startswith("Pronouns") and "verification code" in asked[1], asked
    assert json.loads(apply.ANSWERS.read_text()) == {"Pronouns": "She/her"}  # the code isn't saved as an answer
    assert status == "submitted", status


if __name__ == "__main__":
    test_fill_and_submit()
    print("ok test_fill_and_submit")
