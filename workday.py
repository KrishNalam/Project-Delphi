"""Workday applications. Every company runs its own Workday site with the same wizard:

  Apply -> Autofill with Resume -> Create Account / Sign In -> Autofill with Resume -> My Information
  -> My Experience -> Application Questions -> Voluntary Disclosures -> (Self Identify) -> Review

We sign in (or make an account with your email + WORKDAY_PASSWORD), upload the resume so Workday
prefills what it can, fill each page with apply.fill_form, and stop at Review for your OK.
"""
import logging
import re
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from apply import SUCCESS, fill_form, open_page, resume_path
from config import PROFILE, env
from store import get_setting, set_setting

log = logging.getLogger(__name__)
WORKDAY = re.compile(r"\.myworkdayjobs\.com/|\.myworkdaysite\.com/")
MAX_PAGES = 10


def handles(url):
    return bool(WORKDAY.search(url))


def _id(page, automation_id):
    return page.locator(f'[data-automation-id="{automation_id}"]').first


async def _visible(page, automation_id, timeout=1500):
    try:
        await _id(page, automation_id).wait_for(state="visible", timeout=timeout)
        return True
    except Exception:
        return False


async def _press(page, automation_id):
    # Workday lays an invisible "click_filter" div over its buttons. A forced click lands on it,
    # which is what Workday listens to.
    await _id(page, automation_id).click(force=True)
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(1500)


async def _step(page):
    """The wizard step we're on, e.g. 'My Information'."""
    if not await _visible(page, "progressBarActiveStep", 5000):
        return ""
    text = await _id(page, "progressBarActiveStep").inner_text()
    return re.sub(r"^current step \d+ of \d+\s*", "", text.strip(), flags=re.I)


async def _errors(page):
    alerts = page.locator('[data-automation-id="errorMessage"], [data-automation-id="inputAlert"]')
    texts = await alerts.all_inner_texts()
    return [t.strip() for t in texts if t.strip()]


async def _fill_sign_in(page, email, password):
    await _id(page, "email").fill(email)
    await _id(page, "password").fill(password)
    await _press(page, "signInSubmitButton")


async def _sign_in(page, ask):
    tenant, email, password = urlparse(page.url).netloc, PROFILE["email"], env("WORKDAY_PASSWORD")
    accounts = get_setting("workday_accounts")
    if await _visible(page, "SignInWithEmailButton"):
        await _press(page, "SignInWithEmailButton")
    if tenant not in accounts and await _visible(page, "createAccountLink"):
        await _press(page, "createAccountLink")
        await _id(page, "email").fill(email)
        await _id(page, "password").fill(password)
        await _id(page, "verifyPassword").fill(password)
        if await _visible(page, "createAccountCheckbox", 500):
            await _id(page, "createAccountCheckbox").check(force=True)
        await _press(page, "createAccountSubmitButton")
        errors = await _errors(page)
        if errors and "already" not in " ".join(errors).lower():
            raise RuntimeError(f"couldn't create a Workday account on {tenant}: {'; '.join(errors)}")
        set_setting("workday_accounts", accounts + [tenant])  # made now, or it already existed
        if errors and await _visible(page, "signInLink"):
            await _press(page, "signInLink")
    if await _visible(page, "signInSubmitButton"):
        await _fill_sign_in(page, email, password)
    body = (await page.inner_text("body")).lower()
    if "verif" in body and "email" in body and re.search(r"sign in|create account", await _step(page), re.I):
        await ask([f"Workday ({tenant}) sent a verification email to {email}. "
                   "Click the link in it, then reply 'done'."])
        if await _visible(page, "signInSubmitButton"):
            await _fill_sign_in(page, email, password)
    if re.search(r"sign in|create account", await _step(page), re.I):
        errors = await _errors(page) or ["still on the sign-in page"]
        raise RuntimeError(f"couldn't sign in to {tenant}: {'; '.join(errors)}")


async def apply(job, ask, confirm, stuck):
    """Same contract as apply.apply."""
    resume = resume_path()
    if not env("WORKDAY_PASSWORD"):
        raise RuntimeError("set WORKDAY_PASSWORD in .env (8+ characters with upper, lower, number and symbol)")
    async with async_playwright() as p:
        page = await open_page(p, job["url"])
        try:
            if await _visible(page, "legalNoticeAcceptButton"):
                await _press(page, "legalNoticeAcceptButton")
            if not await _visible(page, "adventureButton", 5000):
                raise RuntimeError("no Apply button, the posting may be closed")
            await _press(page, "adventureButton")
            for choice in ("autofillWithResume", "applyManually"):
                if await _visible(page, choice):
                    await _press(page, choice)
                    break
            await _sign_in(page, ask)
            for _ in range(MAX_PAGES):
                step = await _step(page)
                if re.search(r"review", step, re.I):
                    if not await confirm(await page.screenshot(full_page=True)):
                        return "cancelled"
                    await _press(page, "bottom-navigation-next-button")  # labelled Submit on this page
                    await page.wait_for_timeout(4000)
                    if SUCCESS.search(await page.inner_text("body")):
                        return "submitted"
                    raise RuntimeError("clicked Submit but Workday didn't confirm the application")
                if re.search(r"autofill with resume", step, re.I):
                    await _id(page, "file-upload-input-ref").set_input_files(resume)
                    await page.wait_for_timeout(6000)  # Workday parses the resume to prefill later pages
                else:
                    await fill_form(page, job, ask)
                await _press(page, "bottom-navigation-next-button")
                if errors := await _errors(page):
                    raise RuntimeError(f"Workday rejected '{step}': {'; '.join(errors[:5])}")
                if await _step(page) == step:
                    raise RuntimeError(f"still on '{step}' after Save and Continue")
            raise RuntimeError(f"no Review page after {MAX_PAGES} pages")
        except Exception as e:
            log.exception("workday %s", job["url"])
            reason = f"Workday: {str(e)[:300]}"
        return "manual" if await stuck(reason, await page.screenshot(full_page=True)) else "handoff"
