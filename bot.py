"""Telegram bot and daily digest. Run: python bot.py"""
import asyncio
import logging
import atexit
import re
import shutil
import socket
import subprocess
import time
import urllib.request
from datetime import datetime
from html import escape
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

import apply
import llm
import sources
import workday
from config import PROFILE, ROOT, env
from filters import keep, relevance, words
from store import (already_applied, applied_rows, get_job, get_setting, is_new, log_application, save_job,
                   set_setting, set_status)

log = logging.getLogger("delphi")
CHAT_ID = int(env("TELEGRAM_CHAT_ID", "0"))
TZ = ZoneInfo(env("DELPHI_TZ", "America/Toronto"))
DIGEST_HOUR = int(env("DIGEST_HOUR", "8"))
pending = None  # (kind, Future) the running application is waiting on: "text" reply or "confirm" tap
apply_lock = asyncio.Lock()  # one application at a time so replies can't get crossed


def find_jobs():
    countries = get_setting("countries")
    found = sources.github_repos() + sources.jobspy_jobs(countries) + sources.canvas_jobs()
    fresh = {}
    for job in found:
        # Only jobs already shown are skipped, so unpicked or filtered-out ones get another look later.
        if not is_new(job["id"]):
            continue
        save_job(job)
        if keep(job, countries, PROFILE.get("needs_us_sponsorship", True)) and not already_applied(job):
            fresh[job["id"]] = job
    # Only 60 keyword matches (newest first on ties) go to Claude, to keep ranking cheap. Same role posted
    # twice counts once. Half the spots go to Canadian postings, since US ones need sponsorship.
    resume_words = words(PROFILE.get("resume_text", ""))
    ranked, roles = [], set()
    for job in sorted(fresh.values(), key=lambda j: (relevance(j, resume_words), j["posted"]), reverse=True):
        role = (job["company"].lower(), job["title"].lower())
        if role not in roles:
            roles.add(role)
            ranked.append(job)
    canadian = [j for j in ranked if "canada" in j["countries"]][:30]
    shortlist = canadian + [j for j in ranked if j not in canadian][:60 - len(canadian)]
    picks = llm.rank(shortlist, get_setting("count"))
    for job in picks:
        set_status(job["id"], "shown")
    return picks


async def send_digest(app):
    try:
        jobs = await asyncio.to_thread(find_jobs)
    except Exception as e:
        log.exception("digest failed")
        await app.bot.send_message(CHAT_ID, f"Digest failed: {e}\nFix it, then send /digest to retry.")
        return
    if not jobs:
        await app.bot.send_message(CHAT_ID, "No new matches today.")
    for job in jobs:
        await app.bot.send_message(
            CHAT_ID,
            f"<b>{escape(job['title'])}</b>\n{escape(job['company'])} · {escape('; '.join(job['locations']))}\n"
            f"{job['score']}/10 · {escape(job['why'])}\n<a href=\"{escape(job['url'])}\">Open posting</a>",
            parse_mode="HTML", disable_web_page_preview=True,
            reply_markup=Keyboard([[Button("Apply", callback_data=f"apply:{job['id']}"),
                                    Button("Skip", callback_data=f"skip:{job['id']}")]]))


async def tick(ctx):
    """Hourly. Sends the day's digest once: at DIGEST_HOUR, or on the first tick after (machine was asleep)."""
    now = datetime.now(TZ)
    today = now.date().isoformat()
    if get_setting("paused") or now.hour < DIGEST_HOUR or get_setting("last_digest") == today:
        return
    set_setting("last_digest", today)
    await send_digest(ctx.application)


def parse_answers(text, n):
    """'1. foo\\n2) bar' -> ['foo', 'bar']. One unnumbered line: 'a, b, c' with exactly n parts is one answer each,
    anything else answers all n. None if the count is off."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if n > 1 and len(lines) == 1 and not re.match(r"\d+[.)]", lines[0]):
        parts = [p.strip() for p in lines[0].split(",")]
        return parts if len(parts) == n else lines * n
    lines = [re.sub(r"^\d+[.)]\s*", "", line) for line in lines]
    return lines if len(lines) == n else None


async def _wait(kind):
    global pending
    pending = (kind, asyncio.get_running_loop().create_future())
    try:
        return await asyncio.wait_for(pending[1], timeout=6 * 3600)
    finally:
        pending = None


def final_url(url):
    """Where a short link (go.vansh.nyc/eZ -> jobs.ashbyhq.com/...) lands, so the right filler picks it up."""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=20) as r:
            return r.url
    except Exception:  # some sites refuse scripts; the link itself is the best we have
        return url


async def run_apply(job, bot):
    async with apply_lock:
        job = {**job, "url": await asyncio.to_thread(final_url, job["url"])}
        if row := already_applied(job):  # e.g. Apply tapped twice while the first run was going
            await bot.send_message(CHAT_ID, f"You already applied to {job['company']} on {row['date']}.")
            return
        await bot.send_message(CHAT_ID, f"Applying to {job['title']} @ {job['company']}…")

        async def ask(questions):
            numbered = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
            await bot.send_message(CHAT_ID, f"I need a few answers. Reply with one numbered line each, "
                                            f"or one line like 'a, b, c', or one line to answer them all:\n{numbered}")
            while not (answers := parse_answers(await _wait("text"), len(questions))):
                await bot.send_message(CHAT_ID, f"I need {len(questions)} numbered lines, or one line for all.")
            await bot.send_message(CHAT_ID, "Got it, saved for next time.")
            return answers

        async def confirm(png):
            if env("CONFIRM_BEFORE_SUBMIT", "1") != "1":
                return True
            await bot.send_document(
                CHAT_ID, png, filename="application.png", caption="Submit this?" + watch_hint(),
                reply_markup=Keyboard([[Button("Submit", callback_data="confirm:yes"),
                                        Button("Cancel", callback_data="confirm:no")]]))
            return await _wait("confirm")

        done_button = Keyboard([[Button("I applied ✅", callback_data=f"done:{job['id']}")]])

        async def stuck(reason, png):
            if env("HEADLESS", "1") == "1" and not env("VNC_PASSWORD"):
                caption = f"{reason}.\n\nThis is what I filled. Finish it here: {job['url']}"
                await bot.send_document(CHAT_ID, png, filename="form.png", caption=caption[:1024],
                                        reply_markup=done_button)
                return False
            # Watching mode: the browser stays open on your computer until you tap a button.
            await bot.send_document(
                CHAT_ID, png, filename="form.png",
                caption=(f"{reason}.\n\nThe browser is still open on your computer, finish it there."
                         + watch_hint())[:1024],
                reply_markup=Keyboard([[Button("I applied ✅", callback_data="stuck:yes"),
                                        Button("Close browser", callback_data="stuck:no")]]))
            return await _wait("stuck")

        error = ""
        try:
            site = workday if workday.handles(job["url"]) else apply
            status = await site.apply(job, ask, confirm, stuck)
        except Exception as e:  # before the form opened: missing resume, page didn't load, ...
            log.exception("applying to %s", job["url"])
            status, error = "failed", str(e)[:300]
        set_status(job["id"], status)
        if status == "submitted":
            log_application(job, "submitted")
            await bot.send_message(CHAT_ID, f"Submitted ✅ {job['title']} @ {job['company']}")
        elif status == "manual":
            log_application(job, "manual")
            await bot.send_message(CHAT_ID, "Logged in applied.csv.")
        elif status == "cancelled":
            await bot.send_message(CHAT_ID, "Cancelled, nothing was submitted. Tap Apply again to retry.")
        elif status in ("unsupported", "failed"):
            reason = "I can't fill this site automatically" if status == "unsupported" else f"Couldn't start: {error}"
            await bot.send_message(CHAT_ID, f"{reason}.\nFinish it here: {job['url']}\n"
                                            "Or fix the problem and tap Apply again.", reply_markup=done_button)
        # "handoff": stuck() already sent the screenshot and link.


def start_phone_view():
    """With VNC_PASSWORD set, the bot's browser draws on a virtual screen that your phone can open in Safari:
    Xvfb is the screen, x11vnc shares it, noVNC (websockify) serves it as a web page on port 6080."""
    if not all(map(shutil.which, ("Xvfb", "x11vnc", "websockify"))):
        raise SystemExit("VNC_PASSWORD is set, so the phone view needs: sudo apt install xvfb x11vnc novnc")
    secret = ROOT / ".vncpass"
    secret.write_text(env("VNC_PASSWORD"))
    secret.chmod(0o600)
    procs = []
    for cmd in (["Xvfb", apply.DISPLAY, "-screen", "0", "1280x900x24"],
                ["x11vnc", "-display", apply.DISPLAY, "-rfbport", "5999", "-localhost", "-forever", "-shared",
                 "-passwdfile", str(secret), "-quiet"],
                ["websockify", "--web", "/usr/share/novnc", "6080", "localhost:5999"]):
        procs.append(subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(1)  # each needs the one before it up
    atexit.register(lambda: [proc.terminate() for proc in procs])
    log.info("phone view: %s", phone_link())


def phone_link():
    host = env("PHONE_VIEW_HOST")  # e.g. your Tailscale name; default: this computer's address on your Wi-Fi
    if not host:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # no packet is sent; this just picks the outgoing interface
            host = s.getsockname()[0]
    return f"http://{host}:6080/vnc.html?autoconnect=1&resize=scale"


def watch_hint():
    return f"\nOr from your phone: {phone_link()}" if env("VNC_PASSWORD") else ""


async def cmd_start(update, ctx):
    await update.message.reply_text(f"Your chat id is {update.effective_chat.id}. Put it in .env as TELEGRAM_CHAT_ID.")


async def cmd_digest(update, ctx):
    await update.message.reply_text("Searching…")
    ctx.application.create_task(send_digest(ctx.application))


async def cmd_country(update, ctx):
    countries = {"canada": ["canada"], "usa": ["usa"], "both": ["canada", "usa"]}.get((ctx.args or [""])[0].lower())
    if countries:
        set_setting("countries", countries)
    await update.message.reply_text(f"Searching: {', '.join(get_setting('countries'))}. "
                                    "Change with /country canada|usa|both")


async def cmd_count(update, ctx):
    if ctx.args and ctx.args[0].isdigit():
        set_setting("count", max(5, min(10, int(ctx.args[0]))))
    await update.message.reply_text(f"{get_setting('count')} jobs per digest. Change with /count 5-10")


async def cmd_pause(update, ctx):
    set_setting("paused", True)
    await update.message.reply_text("Paused. /resume to start again.")


async def cmd_resume(update, ctx):
    set_setting("paused", False)
    await update.message.reply_text("Resumed.")


async def cmd_cost(update, ctx):
    await update.message.reply_text(llm.usage_summary())


async def cmd_applied(update, ctx):
    rows = applied_rows()
    lines = [f"{r['date']}  {r['company']} · {r['title']} ({r['status']})" for r in rows[-15:]]
    await update.message.reply_text(f"{len(rows)} applications. Latest:\n" + "\n".join(lines) if rows else "None yet.")


def job_from_url(url):
    """A job for /apply: the saved one if the digest found it, else a stub named after the link."""
    stub = sources._job("telegram", "", "", [], url)
    if job := get_job(stub["id"]):
        return job
    # e.g. altera.wd1.myworkdayjobs.com/.../Software-Intern_R03373 -> Altera, "Software Intern"
    host, slug = urlparse(url).netloc, urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    # jobs.lever.co/acme/..., boards.greenhouse.io/acme/...: the company is the first path segment
    name = host.removeprefix("www.").split(".")[0]
    if name in ("jobs", "boards", "job-boards", "apply", "careers"):
        name = urlparse(url).path.strip("/").split("/")[0] or name
    stub["company"] = name.title()
    stub["title"] = re.sub(r"_R?\d+$", "", slug).replace("-", " ").strip() or url
    save_job(stub)
    return stub


async def cmd_apply(update, ctx):
    url = (ctx.args or [""])[0]
    if not url.startswith("http"):
        await update.message.reply_text("Send /apply <link to the posting>")
        return
    ctx.application.create_task(run_apply(job_from_url(url), ctx.bot))


async def on_button(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.message.chat.id != CHAT_ID:
        return
    action, _, arg = q.data.partition(":")
    if action in ("confirm", "stuck"):
        if pending and pending[0] == action:
            pending[1].set_result(arg == "yes")
        await q.edit_message_reply_markup(None)
        return
    job = get_job(arg)
    if not job:
        return
    if action == "skip":
        await q.edit_message_reply_markup(None)
        set_status(arg, "skipped")
    elif action == "done":
        await q.edit_message_reply_markup(None)
        log_application(job, "manual")
        set_status(arg, "manual")
        await q.message.reply_text("Logged in applied.csv.")
    elif row := already_applied(job):
        await q.message.reply_text(f"You already applied to this on {row['date']}.")
    else:
        ctx.application.create_task(run_apply(job, ctx.bot))


async def on_text(update, ctx):
    if pending and pending[0] == "text":
        pending[1].set_result(update.message.text)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if env("VNC_PASSWORD"):
        start_phone_view()
    app = Application.builder().token(env("TELEGRAM_BOT_TOKEN")).build()
    me = filters.Chat(CHAT_ID)  # only you can drive the bot
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("digest", cmd_digest, me))
    for name, fn in [("country", cmd_country), ("count", cmd_count), ("pause", cmd_pause),
                     ("resume", cmd_resume), ("applied", cmd_applied), ("cost", cmd_cost),
                     ("apply", cmd_apply)]:
        app.add_handler(CommandHandler(name, fn, me))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(me & filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(tick, interval=3600, first=10)
    app.run_polling()


if __name__ == "__main__":
    main()
