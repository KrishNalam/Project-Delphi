"""Fill out (and submit) an application in a real browser with Playwright.

Greenhouse forms only for now. Anything else is handed back to you as a link.
Debug: python apply.py <job url>  prints the form fields the bot sees.
"""
import asyncio
import json
import re
import sys

from playwright.async_api import async_playwright

from config import env
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


if __name__ == "__main__":
    print(json.dumps(asyncio.run(fields_of(sys.argv[1])), indent=1))
