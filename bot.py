"""Telegram bot and daily digest. Run: python bot.py"""
import asyncio
import logging
import re
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

import apply
import llm
import sources
from config import PROFILE, env
from filters import keep
from store import (already_applied, applied_rows, get_job, get_setting, is_seen, log_application, save_job,
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
        if is_seen(job["id"]):
            continue
        save_job(job)
        if keep(job, countries, PROFILE.get("needs_us_sponsorship", True)) and not already_applied(job):
            fresh[job["id"]] = job
    # ponytail: only the newest 150 go to the ranker to bound cost; raise it if good jobs get cut
    newest = sorted(fresh.values(), key=lambda j: j["posted"], reverse=True)[:150]
    return llm.rank(newest, get_setting("count"))


async def send_digest(app):
    jobs = await asyncio.to_thread(find_jobs)
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
    """'1. foo\\n2) bar' -> ['foo', 'bar']. None unless there are exactly n answers."""
    lines = [re.sub(r"^\s*\d+[.)]\s*", "", line).strip() for line in text.splitlines() if line.strip()]
    return lines if len(lines) == n else None


async def _wait(kind):
    global pending
    pending = (kind, asyncio.get_running_loop().create_future())
    try:
        return await asyncio.wait_for(pending[1], timeout=6 * 3600)
    finally:
        pending = None


async def run_apply(job, bot):
    async with apply_lock:
        await bot.send_message(CHAT_ID, f"Applying to {job['title']} @ {job['company']}…")

        async def ask(questions):
            numbered = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
            await bot.send_message(CHAT_ID, f"I need a few answers (one numbered line each):\n{numbered}")
            while not (answers := parse_answers(await _wait("text"), len(questions))):
                await bot.send_message(CHAT_ID, f"I need exactly {len(questions)} numbered answers, try again.")
            await bot.send_message(CHAT_ID, "Got it, saved for next time.")
            return answers

        async def confirm(png):
            if env("CONFIRM_BEFORE_SUBMIT", "1") != "1":
                return True
            await bot.send_document(
                CHAT_ID, png, filename="application.png", caption="Submit this?",
                reply_markup=Keyboard([[Button("Submit", callback_data="confirm:yes"),
                                        Button("Cancel", callback_data="confirm:no")]]))
            return await _wait("confirm")

        try:
            status = await apply.apply(job, ask, confirm)
        except Exception:
            log.exception("applying to %s", job["url"])
            status = "failed"
        set_status(job["id"], status)
        if status == "submitted":
            log_application(job, "submitted")
            await bot.send_message(CHAT_ID, f"Submitted ✅ {job['title']} @ {job['company']}")
        elif status == "cancelled":
            await bot.send_message(CHAT_ID, "Cancelled, nothing was submitted.")
        else:
            reason = {"handoff": "I can't fill this site automatically",
                      "unconfirmed": "I clicked submit but couldn't confirm it went through (often a CAPTCHA)",
                      "failed": "Something broke while filling the form"}[status]
            await bot.send_message(
                CHAT_ID, f"{reason}. Finish it here:\n{job['url']}",
                reply_markup=Keyboard([[Button("I applied ✅", callback_data=f"done:{job['id']}")]]))


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


async def cmd_applied(update, ctx):
    rows = applied_rows()
    lines = [f"{r['date']}  {r['company']} · {r['title']} ({r['status']})" for r in rows[-15:]]
    await update.message.reply_text(f"{len(rows)} applications. Latest:\n" + "\n".join(lines) if rows else "None yet.")


async def on_button(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.message.chat.id != CHAT_ID:
        return
    action, _, arg = q.data.partition(":")
    if action == "confirm":
        if pending and pending[0] == "confirm":
            pending[1].set_result(arg == "yes")
        await q.edit_message_reply_markup(None)
        return
    job = get_job(arg)
    if not job:
        return
    await q.edit_message_reply_markup(None)
    if action == "skip":
        set_status(arg, "skipped")
    elif action == "done":
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
    app = Application.builder().token(env("TELEGRAM_BOT_TOKEN")).build()
    me = filters.Chat(CHAT_ID)  # only you can drive the bot
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("digest", cmd_digest, me))
    for name, fn in [("country", cmd_country), ("count", cmd_count), ("pause", cmd_pause),
                     ("resume", cmd_resume), ("applied", cmd_applied)]:
        app.add_handler(CommandHandler(name, fn, me))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(me & filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(tick, interval=3600, first=10)
    app.run_polling()


if __name__ == "__main__":
    main()
