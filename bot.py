"""Telegram bot and daily digest. Run: python bot.py"""
import asyncio
import logging
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, filters

import llm
import sources
from config import PROFILE, env
from filters import keep
from store import already_applied, get_job, get_setting, is_seen, save_job, set_status
from store import set_setting

log = logging.getLogger("delphi")
CHAT_ID = int(env("TELEGRAM_CHAT_ID", "0"))
TZ = ZoneInfo(env("DELPHI_TZ", "America/Toronto"))
DIGEST_HOUR = int(env("DIGEST_HOUR", "8"))


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


async def cmd_start(update, ctx):
    await update.message.reply_text(f"Your chat id is {update.effective_chat.id}. Put it in .env as TELEGRAM_CHAT_ID.")


async def cmd_digest(update, ctx):
    await update.message.reply_text("Searching…")
    ctx.application.create_task(send_digest(ctx.application))


async def on_button(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.message.chat.id != CHAT_ID:
        return
    action, _, arg = q.data.partition(":")
    job = get_job(arg)
    if not job:
        return
    await q.edit_message_reply_markup(None)
    if action == "skip":
        set_status(arg, "skipped")
    elif row := already_applied(job):
        await q.message.reply_text(f"You already applied to this on {row['date']}.")
    else:
        set_status(arg, "interested")
        await q.message.reply_text(f"Saved. Auto-apply isn't built yet, apply here:\n{job['url']}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = Application.builder().token(env("TELEGRAM_BOT_TOKEN")).build()
    me = filters.Chat(CHAT_ID)  # only you can drive the bot
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("digest", cmd_digest, me))
    app.add_handler(CallbackQueryHandler(on_button))
    app.job_queue.run_repeating(tick, interval=3600, first=10)
    app.run_polling()


if __name__ == "__main__":
    main()
