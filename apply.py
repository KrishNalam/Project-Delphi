"""Fill out (and submit) an application in a real browser with Playwright.

Greenhouse forms only for now. Anything else is handed back to you as a link.
Debug: python apply.py <job url>  prints the form fields the bot sees.
"""
import asyncio
import json
import logging
import re
import sys

from playwright.async_api import async_playwright

import llm
from config import ROOT, env

log = logging.getLogger(__name__)
ANSWERS = ROOT / "answers.json"  # label -> your answer, reused on every later form
SUPPORTED = re.compile(r"greenhouse\.io")

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
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const type = el.getAttribute('role') === 'combobox' ? 'combobox' : (el.type || el.tagName).toLowerCase();
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


async def _open(p, url):
    browser = await p.chromium.launch(headless=env("HEADLESS", "1") == "1")
    page = await browser.new_page()
    await page.goto(url, wait_until="networkidle", timeout=60000)
    return page, await page.evaluate(EXTRACT_JS)


async def fields_of(url):
    async with async_playwright() as p:
        return (await _open(p, url))[1]


async def _fill(page, field, value):
    el = page.locator(f'[data-delphi="{field["index"]}"]')
    kind = field["type"]
    if kind.startswith("select"):
        await el.select_option(label=value)
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


def _question(field):
    options = f" [{' / '.join(field['options'])}]" if field["options"] else ""
    return field["label"] + options


async def apply(job, ask):
    """Fill the form, asking you (ask(questions) -> answers) for anything unknown.

    Returns 'filled', or 'handoff' when it's not a site we handle.
    """
    if not SUPPORTED.search(job["url"]):
        return "handoff"
    answers = json.loads(ANSWERS.read_text()) if ANSWERS.exists() else {}
    async with async_playwright() as p:
        page, fields = await _open(p, job["url"])
        if not fields:
            return "handoff"
        plan = await asyncio.to_thread(llm.map_fields, job, fields, answers)
        values = {f.index: f.value for f in plan.fills
                  if 0 <= f.index < len(fields) and fields[f.index]["type"] != "file"}
        unknown = [i for i in plan.unknown if 0 <= i < len(fields) and i not in values]
        if unknown:
            for i, reply in zip(unknown, await ask([_question(fields[i]) for i in unknown]), strict=True):
                values[i] = answers[fields[i]["label"]] = reply
            ANSWERS.write_text(json.dumps(answers, indent=1))
        for i, value in values.items():
            try:
                await _fill(page, fields[i], value)
            except Exception as e:  # one stubborn widget shouldn't sink the form; the screenshot shows it
                log.warning("fill %r: %s", fields[i]["label"], e)
        return "filled"


if __name__ == "__main__":
    print(json.dumps(asyncio.run(fields_of(sys.argv[1])), indent=1))
