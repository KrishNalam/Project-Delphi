"""Workday applications. Every company runs its own Workday site with the same wizard:

  Apply -> Autofill with Resume -> Create Account / Sign In -> Autofill with Resume -> My Information
  -> My Experience -> Application Questions -> Voluntary Disclosures -> (Self Identify) -> Review

We sign in (or make an account with your email + WORKDAY_PASSWORD), upload the resume so Workday
prefills what it can, fill each page with apply.fill_form, and stop at Review for your OK.
"""
import contextlib
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
# Wording Workday uses when an account needs its email confirmed (not the "Verify New Password" label).
VERIFY = re.compile(r"verify your (email|account)|verification (email|link)|activate your account|"
                    r"sent (you )?an? (verification )?email", re.I)


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


async def _next(page):
    """Save and Continue / Continue / Submit. Older Workday sites use the first id, newer ones the second."""
    for automation_id in ("bottom-navigation-next-button", "pageFooterNextButton"):
        if await _visible(page, automation_id, 500):
            return await _press(page, automation_id)
    raise RuntimeError("no Continue button on this page")


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
    if not await _visible(page, "signInSubmitButton") and await _visible(page, "signInLink"):
        await _press(page, "signInLink")
    await _id(page, "email").fill(email)
    await _id(page, "password").fill(password)
    await _press(page, "signInSubmitButton")


async def _signed_in(page):
    # The progress bar already reads "Autofill with Resume" above the sign-in form, so check for the form too.
    step = await _step(page)
    return (bool(step) and not re.search(r"sign in|create account", step, re.I)
            and not await _visible(page, "signInSubmitButton", 500)
            and not await _visible(page, "createAccountSubmitButton", 500))


async def _open_application(page, url):
    """Job posting -> Apply -> Autofill with Resume. Lands on sign-in, or the wizard if already signed in."""
    if url not in page.url:
        await page.goto(url, wait_until="networkidle", timeout=60000)
    if await _visible(page, "legalNoticeAcceptButton"):
        await _press(page, "legalNoticeAcceptButton")
    if not await _visible(page, "adventureButton", 5000):
        raise RuntimeError("no Apply button, the posting may be closed")
    await _press(page, "adventureButton")
    for choice in ("autofillWithResume", "applyManually"):
        if await _visible(page, choice):
            await _press(page, choice)
            return


async def _sign_in(page, ask, url):
    tenant, email, password = urlparse(page.url).netloc, PROFILE["email"], env("WORKDAY_PASSWORD")
    accounts = get_setting("workday_accounts")
    created = False
    if await _visible(page, "SignInWithEmailButton"):
        await _press(page, "SignInWithEmailButton")
    # Some sites open on the Create Account form, others on Sign In with a link to it.
    if tenant not in accounts and await _visible(page, "createAccountLink"):
        await _press(page, "createAccountLink")
    if tenant not in accounts and await _visible(page, "createAccountSubmitButton"):
        await _id(page, "email").fill(email)
        await _id(page, "password").fill(password)
        await _id(page, "verifyPassword").fill(password)
        if await _id(page, "createAccountCheckbox").count():  # the real input is styled invisible
            await _id(page, "createAccountCheckbox").check(force=True)
        await _press(page, "createAccountSubmitButton")
        errors = await _errors(page)
        if errors and "already" not in " ".join(errors).lower():
            raise RuntimeError(f"couldn't create a Workday account on {tenant}: {'; '.join(errors)}")
        set_setting("workday_accounts", accounts + [tenant])  # made now, or it already existed
        created = not errors
    for attempt in range(2):
        if await _signed_in(page):
            return
        if not await _step(page):  # bounced to a standalone sign-in page: go back into the application
            await _open_application(page, url)
            if await _signed_in(page):
                return
        await _fill_sign_in(page, email, password)
        if await _signed_in(page):
            return
        # New accounts usually need their email confirmed first; Workday calls that a "wrong password".
        if attempt == 0:
            await ask([f"Workday ({tenant}) needs {email} verified. Open the email from Workday, click the "
                       "link, then reply 'done'." if created or VERIFY.search(await page.inner_text("body")) else
                       f"Workday ({tenant}) rejected the sign-in for {email}. If you have an older account there, "
                       "reset its password to your WORKDAY_PASSWORD (or verify the email), then reply 'done'."])
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
            await _open_application(page, job["url"])
            await _sign_in(page, ask, job["url"])
            for _ in range(MAX_PAGES):
                if "Something went wrong" in await page.inner_text("body"):  # Workday's own hiccup; a reload clears it
                    await page.reload(wait_until="networkidle")
                    await page.wait_for_timeout(3000)
                step = await _step(page)
                if re.search(r"review", step, re.I):
                    with contextlib.suppress(Exception):  # Submit stays disabled until the summary has loaded
                        await page.locator('[data-automation-id="pageFooterNextButton"]:enabled, '
                                           '[data-automation-id="bottom-navigation-next-button"]:enabled'
                                           ).first.wait_for(timeout=20000)
                    if not await confirm(await page.screenshot(full_page=True)):
                        return "cancelled"
                    await _next(page)  # labelled Submit on this page
                    await page.wait_for_timeout(4000)
                    if SUCCESS.search(await page.inner_text("body")):
                        return "submitted"
                    raise RuntimeError("clicked Submit but Workday didn't confirm the application")
                if re.search(r"autofill with resume", step, re.I):
                    await _id(page, "file-upload-input-ref").set_input_files(resume)
                    await page.wait_for_timeout(6000)  # Workday parses the resume to prefill later pages
                else:
                    # The step name shows before the page's fields render.
                    with contextlib.suppress(Exception):
                        await page.locator('[data-automation-id^="applyFlow"] input').first.wait_for(timeout=15000)
                    await fill_form(page, job, ask)
                await _next(page)
                if errors := await _errors(page):
                    raise RuntimeError(f"Workday rejected '{step}': {'; '.join(errors[:5])}")
                if await _step(page) == step:
                    raise RuntimeError(f"still on '{step}' after Save and Continue")
            raise RuntimeError(f"no Review page after {MAX_PAGES} pages")
        except Exception as e:
            log.exception("workday %s", job["url"])
            reason = f"Workday: {str(e)[:300]}"
        return "manual" if await stuck(reason, await page.screenshot(full_page=True)) else "handoff"
