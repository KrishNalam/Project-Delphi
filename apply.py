"""Fill out (and submit) an application in a real browser with Playwright.

Greenhouse, Lever, Ashby and Workable forms are handled here; Workday in workday.py, which reuses
fill_form(). Anything else (iCIMS, Oracle, ...) is handed back to you as a link.
Debug: python apply.py <job url>  prints the form fields the bot sees.
"""
import asyncio
import contextlib
import json
import logging
import os
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

import llm
from config import PROFILE, ROOT, env

log = logging.getLogger(__name__)
ANSWERS = ROOT / "answers.json"  # label -> your answer, reused on every later form
DISPLAY = ":99"  # virtual screen for the phone view (see bot.start_phone_view)
SUPPORTED = re.compile(r"greenhouse\.io|lever\.co|ashbyhq\.com|apply\.workable\.com")
# Single tick boxes: agreements get ticked, promotions stay off, the site's own on/off settings are left alone.
CONSENT = re.compile(r"understand|agree|acknowledge|consent|accept|certify|confirm|attest|terms|privacy|allow|"
                     r"permission|authori[sz]", re.I)
PROMOTIONAL = re.compile(r"promot|marketing|newsletter|subscribe|sign (me )?up|mailing list|job (alerts?|postings?)|"
                         r"talent (community|network)", re.I)
SETTING = re.compile(r"currently work here|preferred name", re.I)
COOKIES_DECLINE = re.compile(r"^(decline|reject|refuse)( all)?( (optional|non-essential))?( cookies)?$", re.I)
COOKIES_ACCEPT = re.compile(r"^(accept|allow)( all)?( cookies)?$", re.I)
SUCCESS = re.compile(r"thank(s| you) for (applying|your application|your interest)|application (has been )?"
                     r"(received|submitted)|successfully submitted", re.I)

# Tags every visible form control with data-delphi=<index> and describes it for the LLM.
EXTRACT_JS = r"""
() => {
  // Long questions keep their start and end: the end holds the actual ask and the required '*'.
  const clean = s => {
    s = (s || '').replace(/\s+/g, ' ').trim();
    return s.length > 400 ? s.slice(0, 300) + ' … ' + s.slice(-90) : s;
  };
  const byId = id => id && document.getElementById(id)?.innerText;
  // Question text: the closest wrapper with text besides the field's own options.
  const near = (el, count, skip) => {
    for (let node = el.parentElement; node && node !== document.body; node = node.parentElement) {
      if (node.querySelectorAll('input, select, textarea').length > count + 3) break;  // climbed past the question
      let text = node.innerText || '';
      for (const s of [...skip, ...[...node.querySelectorAll('option')].map(o => o.text)]) {
        const at = s ? text.lastIndexOf(s) : -1;  // options follow the question, which can start with the same words
        if (at >= 0) text = text.slice(0, at) + text.slice(at + s.length);
      }
      if (clean(text)) return clean(text);
    }
    return '';
  };
  // Ashby marks a required question only with a CSS '*' on its title, so look for one in the question's own wrapper.
  const starred = n => /required/i.test(n.getAttribute('class') || '')
    || getComputedStyle(n, '::after').content.includes('*') || getComputedStyle(n, '::before').content.includes('*');
  const marked = (el, count) => {
    for (let node = el.parentElement, i = 0; node && node !== document.body && i < 6; node = node.parentElement, i++) {
      if (node.querySelectorAll('input, select, textarea').length > count + 1) return false;  // into the next question
      if ([...node.querySelectorAll('label, legend')].some(starred)) return true;
    }
    return false;
  };
  const out = [], groups = new Set();
  for (const el of document.querySelectorAll('input, select, textarea, button[aria-haspopup="listbox"]')) {
    const auto = el.getAttribute('data-automation-id') || '';  // Workday's stable field ids
    if (auto === 'beecatcher') continue;  // Workday's honeypot: filling it flags you as a bot
    if (auto === 'utilityMenuButton') continue;  // Workday's header Settings/account menus
    const type = el.tagName === 'BUTTON' ? 'listbox'  // Workday-style dropdown button
      : el.getAttribute('role') === 'combobox' || auto === 'searchBox' || el.dataset.uxiWidgetType === 'selectinput'
        ? 'combobox'
      : auto.startsWith('dateSection') ? 'datepart'
      : (el.type || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'search', 'image', 'reset'].includes(type)) continue;
    if (type !== 'file' && !el.offsetParent) continue;  // invisible (file inputs are usually styled away)
    if (el.closest('[role=listbox]')) continue;  // a choice in an open dropdown or search popup, not a question
    const shim = el.getAttribute('aria-hidden') === 'true' || el.tabIndex < 0;  // e.g. react-select's required-input
    // Workday's Day and Year boxes are tabindex -1 too, but real.
    if (shim && !['file', 'radio', 'checkbox', 'datepart'].includes(type)) continue;
    let members = [el], options = [], boxed = false;
    if (type === 'radio' || type === 'checkbox') {
      // One question = a shared name, or a <fieldset> holding only these boxes (Ashby names each box after itself).
      const set = el.closest('fieldset');
      boxed = !!set && [...set.querySelectorAll('input, select, textarea')].every(m => m.type === type);
      if (groups.has(el)) continue;
      if (boxed) members = [...set.querySelectorAll(`input[type=${type}]`)];
      else if (el.name) members = [...document.querySelectorAll(`input[type=${type}][name="${CSS.escape(el.name)}"]`)];
      members.forEach(m => groups.add(m));
      options = members.map(m => clean(m.labels?.[0]?.innerText || m.value));
    }
    if (el.tagName === 'SELECT') options = [...el.options].map(o => clean(o.text)).filter(Boolean);
    const own = el.labels?.[0]?.innerText || el.getAttribute('aria-label') || byId(el.getAttribute('aria-labelledby'));
    // Workday dropdown buttons name themselves "Select One Required": the question is in the wrapper instead.
    const grouped = members.length > 1 || boxed || el.tagName === 'SELECT' || type === 'file' || type === 'listbox';
    // react-select (Greenhouse, Ashby) empties its <input> after a pick and shows the choice beside it, where it
    // mustn't be mistaken for the question.
    const picked = type === 'combobox' &&
      el.closest('[class*=control]')?.querySelector('[class*=single-value], [class*=singleValue], [class*=multi-value]');
    const skip = type === 'listbox' ? [el.innerText] : picked ? [picked.innerText] : options;
    // name falls back to the id: Greenhouse's two uploads are both labelled "Attach" but have ids resume / cover_letter.
    const f = {index: out.length, type, name: el.name || el.id || '', options, workday: !!auto,
               label: clean((grouped ? '' : own) || near(el, members.length, skip) || own || el.placeholder),
               placeholder: el.placeholder || '',  // the format a date box wants, e.g. "MM/DD/YYYY"
               required: el.required || el.getAttribute('aria-required') === 'true' || marked(el, members.length)};
    // Already has a value (Workday prefills from the resume and from saved drafts): leave it alone.
    const chips = el.parentElement.querySelector('[data-automation-id=promptAriaInstruction]');
    f.filled = type === 'listbox' ? !/^select one/i.test(clean(el.innerText))
      : chips ? !/^0 /.test(chips.innerText)
      : picked ? true
      : type === 'radio' || type === 'checkbox' ? members.some(m => m.checked)
      : type !== 'file' && !!el.value?.trim();
    if (type === 'datepart') {  // each box is labelled just "Month": take the question from the date's form field
      const field = el.closest('[data-automation-id^=formField]')?.querySelector('legend, label');
      f.label = clean(field?.innerText || f.label) + ` (${auto.replace('dateSection', '').replace('-input', '')})`;
    }
    if (f.label.endsWith('*')) f.required = true;
    el.setAttribute('data-delphi', f.index);
    members.forEach(m => m.setAttribute('data-delphi-option', f.index));
    out.push(f);
  }
  // Ashby-style Yes/No toggles: plain <button>s with no input behind them.
  for (const yes of document.querySelectorAll('button')) {
    const box = yes.parentElement, buttons = [...box.querySelectorAll(':scope > button')];
    if (yes !== buttons[0] || buttons.map(b => clean(b.innerText)).join('/') !== 'Yes/No') continue;
    const label = near(box, 0, ['Yes', 'No']);
    box.setAttribute('data-delphi', out.length);
    // required: Ashby only marks it in CSS, so assume it
    out.push({index: out.length, type: 'yesno', name: '', options: ['Yes', 'No'], label, required: true,
              filled: false, workday: false, placeholder: ''});
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
    if "apply.workable.com" in base and not base.endswith("/apply"):
        return base + "/apply"
    return url


GH_JID = re.compile(r"[?&]gh_jid=(\d+)")
# A link to one posting's form (not a whole job board) on a site we can fill, Workday included.
FORM_LINK = re.compile(r"greenhouse\.io/(\S*/jobs/\d+|embed/job_app\?\S*token=\d+)|jobs\.lever\.co/[^/]+/[0-9a-f-]{36}"
                       r"|jobs\.ashbyhq\.com/[^/]+/[0-9a-f-]{36}|apply\.workable\.com/[^/]+/j/\w+"
                       r"|\.myworkday(jobs|site)\.com/\S+/job/")


def direct_url(url):
    """Career pages that embed Greenhouse name the job with ?gh_jid=123; Greenhouse redirects its embed link for that
    id to the form itself (careers.withwaymo.com/jobs?gh_jid=8306841 -> job-boards.greenhouse.io/embed/...)."""
    if match := GH_JID.search(url):
        return f"https://boards.greenhouse.io/embed/job_app?token={match[1]}"
    return url


async def embedded_form(url):
    """For a company page we can't fill: the one supported form it embeds or links to, else None."""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        try:
            await settle(page, url)
            links = await page.evaluate("() => [...document.querySelectorAll('iframe[src], a[href]')]"
                                        ".map(e => e.src || e.href)")
        except Exception as e:
            log.warning("looking for a form on %s: %s", url, e)
            return None
    forms = {direct_url(link).split("#")[0] for link in links if FORM_LINK.search(direct_url(link))}
    return forms.pop() if len(forms) == 1 else None  # several = a list of jobs, not this one


def resume_path():
    resume = Path(PROFILE.get("resume_pdf") or "").expanduser()
    if not resume.is_file():
        raise RuntimeError(f"resume_pdf in profile.yaml doesn't exist: {resume}")
    return resume


async def open_page(p, url):
    if env("VNC_PASSWORD"):  # draw on the display bot.py shares to your phone
        browser = await p.chromium.launch(headless=False, env={**os.environ, "DISPLAY": DISPLAY},
                                          args=["--window-position=0,0", "--window-size=1280,900"])
    else:
        browser = await p.chromium.launch(headless=env("HEADLESS", "1") == "1")
    page = await browser.new_page()
    page.set_default_timeout(10000)  # a widget that won't take a click fails fast instead of hanging 30s
    await settle(page, url)
    return page


async def settle(page, url=None):
    """Go to `url` (if given) and wait for the page to finish loading. Career sites full of trackers never go
    network-idle, so that wait is capped: the page is usable by then."""
    if url:
        await page.goto(url, wait_until="load", timeout=60000)
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=15000)
    # A cookie banner can sit over the Submit button (Workable's does): decline it, or accept if that's the only way.
    for name in (COOKIES_DECLINE, COOKIES_ACCEPT):
        button = page.get_by_role("button", name=name).filter(visible=True)
        with contextlib.suppress(Exception):
            if await button.count():
                await button.first.click(timeout=3000)
                return


async def _wait_count(locator, done, page):
    for _ in range(30):
        if done(await locator.count()):
            return
        await page.wait_for_timeout(100)


async def _open_dropdown(page, el):
    """Click a Workday dropdown button and return its popup's options. Closed popups stay in the page hidden,
    and some lists (skill chips) are always visible, so the popup is the visible listbox that just appeared."""
    popups = page.get_by_role("listbox").filter(visible=True)
    before = await popups.count()
    await el.click()
    await _wait_count(popups, lambda n: n > before, page)
    return popups.last.get_by_role("option"), before


# Pages keep closed menus (e.g. the phone-number country picker) in the DOM, so only visible choices count.
OPTIONS = "[role=option], [data-automation-id=promptOption]"
MAX_OPTIONS = 60  # longer lists (countries, schools) are search boxes: the value gets typed and matched instead


async def read_fields(page):
    """Every fillable field on the page. Dropdowns get opened once to read their options: Workday's buttons, and
    other sites' empty comboboxes (react-select shows its choices only when opened)."""
    fields = await page.evaluate(EXTRACT_JS)
    for f in fields:
        if f["type"] == "combobox" and not f["workday"] and not f["filled"]:
            el = page.locator(f'[data-delphi="{f["index"]}"]')
            try:
                await el.click(timeout=3000)
                options = page.locator(OPTIONS).filter(visible=True)
                await options.first.wait_for(timeout=1500)
                texts = [o.strip() for o in await options.all_inner_texts() if o.strip()]
                if len(texts) <= MAX_OPTIONS:
                    f["options"] = texts
            except Exception:  # nothing until you type (a location search): Claude's value gets typed in
                pass
            await page.keyboard.press("Escape")
        if f["type"] == "listbox":
            try:
                options, _ = await _open_dropdown(page, page.locator(f'[data-delphi="{f["index"]}"]'))
                await options.first.wait_for(timeout=3000)
                await _wait_count(options, lambda n: n > 1, page)  # "Select One" shows before the real choices
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
    with contextlib.suppress(Exception):  # mid-screen, so sticky headers/footers (Workday's Save bar) don't cover it
        await el.evaluate("e => e.scrollIntoView({block: 'center'})", timeout=2000)
    if kind.startswith("select"):
        await el.select_option(label=value)
    elif kind == "combobox":
        # Search dropdowns load options as you type: click the first one containing the value. If none shows,
        # press Enter: that picks the top match on most sites, and runs the search on Workday's prompts.
        # An exact match wins, so "No" doesn't pick "Not applicable".
        await el.click()
        await el.fill(value)
        options = page.locator(OPTIONS).filter(visible=True)
        exact = options.filter(has_text=re.compile(rf"^\s*{re.escape(value.strip())}\s*$", re.I))
        option = options.filter(has_text=re.compile(re.escape(value.strip()[:30]), re.I))
        if not field.get("workday"):  # Workday's prompts only search on Enter
            with contextlib.suppress(Exception):
                await options.first.wait_for(timeout=4000)
        try:
            await (exact if await exact.count() else option).first.click(timeout=4000)
        except Exception:
            # Enter with no list open would submit the whole form, so only when there's a list (or on Workday).
            if not (field.get("workday") or await options.count()):
                raise
            await el.press("Enter")
            with contextlib.suppress(Exception):
                await option.first.click(timeout=3000)
    elif kind == "listbox":
        for attempt in range(2):  # a pick sometimes doesn't take; check the button shows it, else try once more
            options, before = await _open_dropdown(page, el)
            try:
                await options.first.wait_for(timeout=4000)
            except Exception:  # the click didn't open it (focus was still in the previous widget)
                await page.keyboard.press("Escape")
                if attempt:
                    raise
                continue
            exact = options.filter(has_text=re.compile(f"^{re.escape(value.strip())}$", re.I))
            # A short reply like "Bachelor" still finds "Bachelor's Degree".
            option = (exact if await exact.count() else options.filter(has_text=value.strip())).first
            picked = (await option.inner_text()).strip()
            await option.click()
            # Wait for it to close, else the next dropdown's pick can land in this closing popup.
            await _wait_count(page.get_by_role("listbox").filter(visible=True), lambda n: n <= before, page)
            if (await el.inner_text()).strip() == picked:
                break
    elif kind == "datepart":  # Workday's split MM / DD / YYYY boxes only take keystrokes (a "YYYY" overlay eats clicks)
        await el.press_sequentially(value)
    elif kind == "yesno":
        await el.get_by_role("button", name=value.strip(), exact=True).click()
    elif kind in ("radio", "checkbox"):
        group = page.locator(f'[data-delphi-option="{field["index"]}"]')
        wanted = {v.strip().lower() for v in value.split(",")}
        single_yes = len(field["options"]) == 1 and value.strip().lower() in ("yes", "true")
        for i, option in enumerate(field["options"]):
            if option.lower() in wanted or single_yes:
                try:
                    await group.nth(i).check(force=True)
                except Exception:  # Workday's styled-away checkboxes only respond to a real DOM click
                    await group.nth(i).evaluate("e => e.checked || e.click()")
    elif await el.get_attribute("data-qa") == "location-input":
        # Lever's location box empties itself on blur unless a suggestion was picked: type the city, then pick the
        # suggestion sharing the most words with the answer ("Toronto, ON, Canada" -> "Toronto, ON, CAN").
        await el.fill("")
        await el.press_sequentially(value.split(",")[0].strip(), delay=30)
        hits = page.locator(".dropdown-location").filter(visible=True)
        await hits.first.wait_for(timeout=5000)
        wanted = set(_norm(value).split())
        texts = await hits.all_inner_texts()
        best = max(range(len(texts)), key=lambda i: len(wanted & set(_norm(texts[i]).split())))
        await hits.nth(best).click()
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
        (r"^(current )?(location|address|city)( city)?$", p.get("city")),
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
        answer = saved.get(label) if label else None  # unlabelled fields share no question
        if answer and (not f["options"] or answer.lower() in (o.lower() for o in f["options"])):
            values[f["index"]] = answer
    return values


def _lone_box(field):
    return field["type"] == "checkbox" and len(field["options"]) == 1


def _box_choice(field):
    """'tick', 'leave', or None when it's unclear (Claude, then you, decide)."""
    text = f"{field['label']} {field['options'][0]}"
    if SETTING.search(text) or (PROMOTIONAL.search(text) and not field["required"]):
        return "leave"
    return "tick" if CONSENT.search(text) else None


def _question(field):
    options = f" [{' / '.join(field['options'])}]" if field["options"] else ""
    label = field["label"] or field.get("placeholder") or field.get("name") or f"(unlabelled {field['type']} field)"
    return label + options


async def _fill_fields(page, job, ask, fields, todo, answers):
    """Fill `todo` (a subset of `fields`): known values first, then Claude, then you for what's left."""
    values = known_values(todo, answers)
    rest = [f for f in todo if f["index"] not in values and f["type"] != "file"]
    unknown = []
    if rest:  # only what we couldn't fill ourselves goes to Claude
        plan = await asyncio.to_thread(llm.map_fields, job, rest, answers)
        asked = {f["index"] for f in rest}
        values |= {f.index: f.value for f in plan.fills if f.index in asked}
        unknown = [i for i in plan.unknown if i in asked and i not in values]
        # A required field (or an unclear tick box) Claude neither answered nor flagged still needs an answer: ask.
        unknown += [i for i in asked if (fields[i]["required"] or _lone_box(fields[i]))
                    and i not in values and i not in unknown]
    if unknown:
        for i, reply in zip(unknown, await ask([_question(fields[i]) for i in unknown]), strict=True):
            values[i] = reply
            if fields[i]["label"]:  # saved for next time under its question; an unlabelled one has none
                answers[fields[i]["label"]] = reply
        ANSWERS.write_text(json.dumps(answers, indent=1))
    for i, value in values.items():
        try:
            await _fill(page, fields[i], value)
        except Exception as e:  # one stubborn widget shouldn't sink the form; the screenshot shows it
            log.warning("fill %r: %s", fields[i]["label"], e)


async def fill_form(page, job, ask, resume=None):
    """Fill every field on the current page. Returns the fields found (empty if the page has none).

    `resume` goes in the page's resume upload, if any.
    """
    answers = json.loads(ANSWERS.read_text()) if ANSWERS.exists() else {}
    fields = await read_fields(page)
    if not fields:  # single-page apps (Ashby) can draw the form a few seconds after the page says it has loaded
        with contextlib.suppress(Exception):
            await page.locator("input:visible, select:visible, textarea:visible").first.wait_for(timeout=20000)
            await page.wait_for_timeout(1000)  # the rest of the form renders with it
        fields = await read_fields(page)
    first, seen = fields, set()
    workday = bool(re.search(r"\.myworkday(jobs|site)\.com/", page.url))
    for attempt in range(3):  # an answer can reveal a follow-up question (e.g. a referrer's name), so look again
        # Workday's prefilled values come from your resume and saved drafts, so they stay. Elsewhere a value there on
        # arrival is the site's guess (Workable puts the server's geo-IP city in Address), so it gets overwritten;
        # on later looks, filled means we filled it.
        todo = [f for f in fields if (f["label"], f["type"]) not in seen
                and not (f["filled"] and (workday or attempt))]
        if not todo:
            break
        seen |= {(f["label"], f["type"]) for f in todo}
        # A lone tick box. "I currently work here" repeats once per job without saying which, so a guess could tick
        # every job as current: leave those as the site set them. Anything unclear goes to Claude, then you.
        for f in [f for f in todo if _lone_box(f) and _box_choice(f)]:
            todo.remove(f)
            if _box_choice(f) == "tick":
                try:
                    await _fill(page, f, "yes")
                except Exception as e:
                    log.warning("tick %r: %s", f["label"], e)
        await _fill_fields(page, job, ask, fields, todo, answers)
        fields = await read_fields(page)
    # Resume goes in the required upload if there is one, else the one labelled resume/CV, else the first.
    files = sorted((f for f in fields if f["type"] == "file"),
                   key=lambda f: (not f["required"], not re.search(r"resume|cv", f"{f['label']} {f['name']}", re.I)))
    if resume and files:
        await page.locator(f'[data-delphi="{files[0]["index"]}"]').set_input_files(resume)
    return first


SUBMIT = re.compile(r"^\s*submit( my| your)?( application)?\s*$", re.I)
# Greenhouse emails a code after Submit on some boards ("Enter the 8-character code sent to ...").
CODE = re.compile(r"(security|verification|confirmation) code|code (was )?sent|enter the code", re.I)


async def _submit(page):
    """Click the visible Submit button (by its name first: a page can have other submit buttons, e.g. search)."""
    named = page.get_by_role("button", name=SUBMIT).filter(visible=True)
    button = named if await named.count() else page.locator(
        "button[type=submit], input[type=submit], button:has-text('Submit')").filter(visible=True)
    await button.first.click()
    await page.wait_for_timeout(6000)


async def _verify_email(page, ask):
    """If the site emailed a code to confirm the application, ask for it, type it in and submit again.
    The code goes in one box, or one character per box. False if no code was asked for."""
    boxes = [f for f in await read_fields(page)
             if f["type"] in ("text", "tel", "number") and CODE.search(f"{f['label']} {f['placeholder']}")]
    if not boxes:
        return False
    host = re.sub(r"^www\.", "", re.sub(r"^https?://([^/]+).*", r"\1", page.url))
    (code,) = await ask([f"{host} emailed a verification code to {PROFILE.get('email', 'your email')}. "
                         "Reply with the code."])
    code = re.sub(r"\s+", "", code)
    parts = [code] if len(boxes) == 1 else list(code)
    for box, part in zip(boxes, parts):
        await page.locator(f'[data-delphi="{box["index"]}"]').fill(part)
    await _submit(page)
    return True


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
            await _submit(page)
            if SUCCESS.search(await page.inner_text("body")):
                return "submitted"
            if await _verify_email(page, ask) and SUCCESS.search(await page.inner_text("body")):
                return "submitted"
            reason = "I clicked submit but couldn't confirm it went through (often a CAPTCHA or a rejected field)"
        except TimeoutError:  # a question or the Submit check went unanswered for hours (asyncio's, not Playwright's)
            reason = "No answer from you for 6 hours, so I stopped here"
        except Exception as e:
            log.exception("applying to %s", job["url"])
            reason = f"Something broke while filling the form: {str(e)[:300]}"
        return "manual" if await stuck(reason, await page.screenshot(full_page=True)) else "handoff"

if __name__ == "__main__":
    print(json.dumps(asyncio.run(fields_of(sys.argv[1])), indent=1))
