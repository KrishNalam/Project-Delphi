"""Fill out (and submit) an application in a real browser with Playwright.

Greenhouse, Lever and Ashby forms are handled here; Workday in workday.py, which reuses
fill_form(). Anything else (iCIMS, Oracle, ...) is handed back to you as a link.
Debug: python apply.py <job url>  prints the form fields the bot sees.
"""
import asyncio
import json
import logging
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

import llm
from config import PROFILE, ROOT, env

log = logging.getLogger(__name__)
ANSWERS = ROOT / "answers.json"  # label -> your answer, reused on every later form
SUPPORTED = re.compile(r"greenhouse\.io|lever\.co|ashbyhq\.com")
SUCCESS = re.compile(r"thank(s| you) for (applying|your application|your interest)|application (has been )?"
                     r"(received|submitted)|successfully submitted", re.I)

# Tags every visible form control with data-delphi=<index> and describes it for the LLM.
EXTRACT_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 300);
  const byId = id => id && document.getElementById(id)?.innerText;
  // Question text: the closest wrapper with text besides the field's own options.
  const near = (el, count, skip) => {
    for (let node = el.parentElement; node && node !== document.body; node = node.parentElement) {
      if (node.querySelectorAll('input, select, textarea').length > count + 3) break;  // climbed past the question
      let text = node.innerText || '';
      for (const s of [...skip, ...[...node.querySelectorAll('option')].map(o => o.text)]) text = text.replace(s, '');
      if (clean(text)) return clean(text);
    }
    return '';
  };
  const out = [], groups = new Set();
  for (const el of document.querySelectorAll('input, select, textarea, button[aria-haspopup="listbox"]')) {
    const auto = el.getAttribute('data-automation-id') || '';  // Workday's stable field ids
    if (auto === 'beecatcher') continue;  // Workday's honeypot: filling it flags you as a bot
    const type = el.tagName === 'BUTTON' ? 'listbox'  // Workday-style dropdown button
      : el.getAttribute('role') === 'combobox' || auto === 'searchBox' ? 'combobox'
      : auto.startsWith('dateSection') ? 'datepart'
      : (el.type || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'search', 'image', 'reset'].includes(type)) continue;
    if (type !== 'file' && !el.offsetParent) continue;  // invisible (file inputs are usually styled away)
    const shim = el.getAttribute('aria-hidden') === 'true' || el.tabIndex < 0;  // e.g. react-select's required-input
    if (shim && !['file', 'radio', 'checkbox'].includes(type)) continue;
    let members = [el], options = [];
    if (type === 'radio' || type === 'checkbox') {
      if (el.name && groups.has(el.name)) continue;
      groups.add(el.name);
      if (el.name) members = [...document.querySelectorAll(`input[type=${type}][name="${CSS.escape(el.name)}"]`)];
      options = members.map(m => clean(m.labels?.[0]?.innerText || m.value));
    }
    if (el.tagName === 'SELECT') options = [...el.options].map(o => clean(o.text)).filter(Boolean);
    const own = el.labels?.[0]?.innerText || el.getAttribute('aria-label') || byId(el.getAttribute('aria-labelledby'));
    const grouped = members.length > 1 || el.tagName === 'SELECT' || type === 'file';
    const f = {index: out.length, type, name: el.name || '', options,
               label: clean((grouped ? '' : own) || near(el, members.length, options) || own || el.placeholder),
               required: el.required || el.getAttribute('aria-required') === 'true'};
    if (type === 'datepart') f.label += ` (${auto.replace('dateSection', '').replace('-input', '')})`;  // (Month)
    if (f.label.endsWith('*')) f.required = true;
    el.setAttribute('data-delphi', f.index);
    out.push(f);
  }
  // Ashby-style Yes/No toggles: plain <button>s with no input behind them.
  for (const yes of document.querySelectorAll('button')) {
    const box = yes.parentElement, buttons = [...box.querySelectorAll(':scope > button')];
    if (yes !== buttons[0] || buttons.map(b => clean(b.innerText)).join('/') !== 'Yes/No') continue;
    const label = near(box, 0, ['Yes', 'No']);
    box.setAttribute('data-delphi', out.length);
    // required: Ashby only marks it in CSS, so assume it
    out.push({index: out.length, type: 'yesno', name: '', options: ['Yes', 'No'], label, required: true});
  }
  return out;
}
"""


def apply_url(url):
    """The page that actually has the form."""
    base = url.split("?")[0].rstrip("/")
    if "lever.co" in base and not base.endswith("/apply"):
        return base + "/apply"
    if "ashbyhq.com" in base and not base.endswith("/application"):
        return base + "/application"
    return url


def resume_path():
    resume = Path(PROFILE.get("resume_pdf") or "").expanduser()
    if not resume.is_file():
        raise RuntimeError(f"resume_pdf in profile.yaml doesn't exist: {resume}")
    return resume


async def open_page(p, url):
    browser = await p.chromium.launch(headless=env("HEADLESS", "1") == "1")
    page = await browser.new_page()
    await page.goto(url, wait_until="networkidle", timeout=60000)
    return page


async def read_fields(page):
    """Every fillable field on the page. Button dropdowns (Workday) get opened once to read their options."""
    fields = await page.evaluate(EXTRACT_JS)
    for f in fields:
        if f["type"] == "listbox":
            try:
                await page.locator(f'[data-delphi="{f["index"]}"]').click()
                options = page.get_by_role("option")
                await options.first.wait_for(timeout=3000)
                f["options"] = [o.strip() for o in await options.all_inner_texts() if o.strip()]
            except Exception as e:
                log.warning("options for %r: %s", f["label"], e)
            await page.keyboard.press("Escape")
    return fields


async def fields_of(url):
    async with async_playwright() as p:
        return await read_fields(await open_page(p, apply_url(url)))


async def _fill(page, field, value):
    el = page.locator(f'[data-delphi="{field["index"]}"]')
    kind = field["type"]
    if kind.startswith("select"):
        await el.select_option(label=value)
    elif kind == "combobox":
        # Search dropdowns load options as you type: click the first one containing the value, else press Enter.
        await el.click()
        await el.fill(value)
        options = page.locator("[role=option], [data-automation-id=promptOption]")
        option = options.filter(has_text=re.compile(re.escape(value[:30]), re.I)).first
        try:
            await option.click(timeout=4000)
        except Exception:
            await el.press("Enter")
    elif kind == "listbox":
        await el.click()
        await page.get_by_role("option", name=value, exact=True).first.click(timeout=4000)
    elif kind == "datepart":  # Workday's split MM / DD / YYYY boxes only take keystrokes
        await el.click()
        await el.press_sequentially(value)
    elif kind == "yesno":
        await el.get_by_role("button", name=value.strip(), exact=True).click()
    elif kind in ("radio", "checkbox"):
        group = page.locator(f'input[type={kind}][name="{field["name"]}"]') if field["name"] else el
        wanted = {v.strip().lower() for v in value.split(",")}
        single_yes = len(field["options"]) == 1 and value.strip().lower() in ("yes", "true")
        for i, option in enumerate(field["options"]):
            if option.lower() in wanted or single_yes:
                await group.nth(i).check(force=True)
    else:
        await el.fill(value)


def _norm(label):
    return re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()


def known_values(fields, answers):
    """{index: value} we can fill without Claude: obvious profile fields and exact repeats of saved answers."""
    p = PROFILE
    profile_fields = [
        (r"^(legal )?first name", p.get("first_name")), (r"^(legal )?last name", p.get("last_name")),
        (r"^(full |legal )?name$", f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()),
        (r"^e ?mail( address)?$", p.get("email")), (r"^(phone|mobile)( number)?$", p.get("phone")),
        (r"^linkedin( profile| url)?$", p.get("linkedin")), (r"^github( profile| url)?$", p.get("github")),
        (r"^(portfolio|website|personal website)( url)?$", p.get("website")),
    ]
    saved = {_norm(label): str(answer) for label, answer in answers.items()}
    values = {}
    for f in fields:
        label = _norm(f["label"])
        if f["type"] in ("text", "email", "tel", "url"):
            value = next((v for pattern, v in profile_fields if v and re.search(pattern, label)), None)
            if value:
                values[f["index"]] = str(value)
                continue
        answer = saved.get(label)
        if answer and (not f["options"] or answer.lower() in (o.lower() for o in f["options"])):
            values[f["index"]] = answer
    return values


def _question(field):
    options = f" [{' / '.join(field['options'])}]" if field["options"] else ""
    return field["label"] + options


async def fill_form(page, job, ask, resume=None):
    """Fill every field on the current page: known values first, then Claude, then you for what's left.

    Returns the fields found (empty if the page has none). `resume` goes in the page's resume upload, if any.
    """
    answers = json.loads(ANSWERS.read_text()) if ANSWERS.exists() else {}
    fields = await read_fields(page)
    values = known_values(fields, answers)
    rest = [f for f in fields if f["index"] not in values and f["type"] != "file"]
    unknown = []
    if rest:  # only what we couldn't fill ourselves goes to Claude
        plan = await asyncio.to_thread(llm.map_fields, job, rest, answers)
        asked = {f["index"] for f in rest}
        values |= {f.index: f.value for f in plan.fills if f.index in asked}
        unknown = [i for i in plan.unknown if i in asked and i not in values]
    if unknown:
        for i, reply in zip(unknown, await ask([_question(fields[i]) for i in unknown]), strict=True):
            values[i] = answers[fields[i]["label"]] = reply
        ANSWERS.write_text(json.dumps(answers, indent=1))
    for i, value in values.items():
        try:
            await _fill(page, fields[i], value)
        except Exception as e:  # one stubborn widget shouldn't sink the form; the screenshot shows it
            log.warning("fill %r: %s", fields[i]["label"], e)
    # Resume goes in the required upload if there is one, else the one labelled resume/CV, else the first.
    files = sorted((f for f in fields if f["type"] == "file"),
                   key=lambda f: (not f["required"], not re.search(r"resume|cv", f["label"], re.I)))
    if resume and files:
        await page.locator(f'[data-delphi="{files[0]["index"]}"]').set_input_files(resume)
    return fields


async def apply(job, ask, confirm, stuck):
    """Fill and submit. Returns 'submitted', 'cancelled', 'unsupported', 'manual' (you finished it) or 'handoff'.

    Async callbacks into the Telegram bot: ask(questions) -> answers, confirm(png) -> bool, and
    stuck(reason, png) -> bool when the bot can't finish (True = you finished it yourself).
    """
    if not SUPPORTED.search(job["url"]):
        return "unsupported"
    resume = resume_path()
    async with async_playwright() as p:
        page = await open_page(p, apply_url(job["url"]))
        try:
            if not await fill_form(page, job, ask, resume):
                raise RuntimeError("found no form fields on the page")
            if not await confirm(await page.screenshot(full_page=True)):
                return "cancelled"
            await page.locator("button[type=submit], input[type=submit], button:has-text('Submit')").first.click()
            await page.wait_for_timeout(6000)
            if SUCCESS.search(await page.inner_text("body")):
                return "submitted"
            reason = "I clicked submit but couldn't confirm it went through (often a CAPTCHA or a rejected field)"
        except Exception as e:
            log.exception("applying to %s", job["url"])
            reason = f"Something broke while filling the form: {str(e)[:300]}"
        return "manual" if await stuck(reason, await page.screenshot(full_page=True)) else "handoff"

if __name__ == "__main__":
    print(json.dumps(asyncio.run(fields_of(sys.argv[1])), indent=1))
