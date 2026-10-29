"""Telegram bot and daily digest. Run: python bot.py"""
import asyncio
import contextlib
import logging
import atexit
import re
import secrets
import shutil
import socket
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

import apply
import llm
import sources
import workday
from config import PROFILE, ROOT, env
from filters import DEGREES, block_term, blocked, degree_kind, keep, relevance, words
from store import (_key, already_applied, applied_rows, canonical, get_job, get_setting, is_new, job_status,
                   jobs_with_status,
                   log_application, save_job, set_setting, set_status, status_counts, update_job)

log = logging.getLogger("delphi")
CHAT_ID = int(env("TELEGRAM_CHAT_ID", "0"))
TZ = ZoneInfo(env("DELPHI_TZ", "America/Toronto"))
DIGEST_HOURS = sorted(int(h) for h in env("DIGEST_HOUR", "8").split(",") if h.strip())  # e.g. "8" or "8,17"
pending = None  # (kind, Future, tag) the application is waiting on: "text" reply, "confirm" or "stuck" tap
apply_lock = asyncio.Lock()  # one application at a time so replies can't get crossed
digest_lock = asyncio.Lock()  # /digest tapped during the daily one would show the same jobs twice
running = None  # the job being applied to, for /status
queued = 0  # applications waiting for apply_lock
STARTED = datetime.now()


def fillable(url):
    """Can the bot fill this posting's form itself? (Workday needs WORKDAY_PASSWORD for the account it makes.)"""
    return bool(apply.SUPPORTED.search(url) or (workday.handles(url) and env("WORKDAY_PASSWORD")))


source_warnings = []  # set by find_jobs, sent with the digest


def check_sources(found):
    """Jobs per source this run; a source that had jobs last time and none now gets a warning (a list moved, a site
    started blocking us), since nothing else would show it."""
    counts = {}
    for job in found:
        counts[job["source"]] = counts.get(job["source"], 0) + 1
    before = get_setting("source_counts")
    set_setting("source_counts", counts)
    return [f"⚠️ {source} returned no jobs this time (it had {n} last time)."
            for source, n in before.items() if n and not counts.get(source)]


def find_jobs():
    countries = get_setting("countries")
    found = sources.github_repos() + sources.jobspy_jobs(countries) + sources.canvas_jobs()
    source_warnings[:] = check_sources(found)
    fresh, blocks = {}, get_setting("blocked")
    for job in found:
        # Only jobs already shown are skipped, so unpicked or filtered-out ones get another look later.
        if not is_new(job["id"]):
            continue
        save_job(job)
        if (keep(job, countries, PROFILE.get("needs_us_sponsorship", True), PROFILE.get("degree_level"),
                 PROFILE.get("terms") or ()) and not blocked(job, blocks)
                and not already_applied(job)):
            fresh[job["id"]] = job
    # Only 60 keyword matches (newest first on ties) go to Claude, to keep ranking cheap. Same role posted
    # twice counts once, keeping the company's own link over LinkedIn's (which the bot can't apply through).
    # Half the spots go to Canadian postings, since US ones need sponsorship.
    resume_words = words(PROFILE.get("resume_text", ""))
    ranked, roles = [], set()
    for job in sorted(fresh.values(), reverse=True,
                      key=lambda j: (relevance(j, resume_words), "linkedin.com/" not in j["url"], j["posted"])):
        role = _key(job["company"], job["title"])  # "Intern, Android" and "Intern - Android" are one role
        if role not in roles:
            roles.add(role)
            ranked.append(job)
    canadian = [j for j in ranked if "canada" in j["countries"]][:30]
    shortlist = canadian + [j for j in ranked if j not in canadian][:60 - len(canadian)]
    picks, urls = [], set()
    for job in llm.rank(shortlist, get_setting("count")):
        # Follow short links now, so the digest can say whether the bot can fill the form and dedupe by real URL.
        job["url"] = apply.direct_url(final_url(job["url"]))
        update_job({k: v for k, v in job.items() if k not in ("score", "why")})
        if canonical(job["url"]) not in urls and not already_applied(job):
            urls.add(canonical(job["url"]))
            picks.append(job)
    return picks


async def send_digest(app, quiet_failure=False):
    """Search, rank and send the picks. Returns False if the search failed (reported unless quiet_failure)."""
    async with digest_lock:
        try:
            jobs = await asyncio.to_thread(find_jobs)
        except Exception as e:
            log.exception("digest failed")
            if not quiet_failure:
                await app.bot.send_message(CHAT_ID, f"Digest failed: {e}\nI'll retry every hour today, "
                                                    "or fix it and send /digest.")
            return False
    if not jobs:
        await app.bot.send_message(CHAT_ID, "No new matches today.")
    if source_warnings:
        await app.bot.send_message(CHAT_ID, "\n".join(source_warnings))
    auto_min = get_setting("auto_min")
    auto = [j for j in jobs if auto_min is not None and j["score"] >= auto_min and fillable(j["url"])]
    sent_all = True
    for job in jobs:
        how = "auto-applying" if job in auto else "auto-fill" if fillable(job["url"]) else "fill it yourself"
        try:
            await app.bot.send_message(
                CHAT_ID,
                f"<b>{escape(job['title'])}</b>\n{escape(job['company'])} · {escape('; '.join(job['locations']))}\n"
                f"{job['score']}/10 · {escape(job['why'])}\n<a href=\"{escape(job['url'])}\">Open posting</a> · {how}",
                parse_mode="HTML", disable_web_page_preview=True,
                reply_markup=None if job in auto else Keyboard([[
                    Button("Apply", callback_data=f"apply:{job['id']}"),
                    Button("Skip", callback_data=f"skip:{job['id']}"),
                    Button("🚫 Company", callback_data=f"block:{job['id']}")]]))
        except Exception:  # Telegram down: the job stays new, so the hourly retry offers it again
            log.exception("sending %s", job["url"])
            sent_all = False
            continue
        set_status(job["id"], "shown")
        if job in auto:  # one at a time, in score order (apply_lock queues them)
            app.create_task(run_apply(job, app.bot))
    return sent_all


UNFINISHED = ("handoff", "failed", "unsupported")  # applications that ended with the link handed to you


async def send_week(bot):
    """This week's applications, the ones still waiting on you (with their links and an I-applied button each),
    and what Claude cost."""
    week_ago = (datetime.now(TZ) - timedelta(days=7)).date().isoformat()
    rows = [r for r in applied_rows() if r["date"] > week_ago]
    by_bot = sum(r["status"] == "submitted" for r in rows)
    open_jobs = [j for j in jobs_with_status(UNFINISHED, 7) if not already_applied(j)]
    await bot.send_message(
        CHAT_ID, f"This week: {len(rows)} applications ({by_bot} submitted by me, {len(rows) - by_bot} finished by "
                 f"you), Claude ${llm.spend_since(7):.2f}."
                 + (f"\n{len(open_jobs)} I couldn't finish are below." if open_jobs else ""))
    for job in open_jobs[:10]:
        await bot.send_message(
            CHAT_ID, f"<b>{escape(job['title'])}</b> · {escape(job['company'])}\n"
                     f"<a href=\"{escape(job['url'])}\">Finish it here</a>",
            parse_mode="HTML", disable_web_page_preview=True,
            reply_markup=Keyboard([[Button("I applied ✅", callback_data=f"done:{job['id']}"),
                                    Button("Drop it", callback_data=f"skip:{job['id']}")]]))


failed_on = ""  # the digest slot that last failed, so its hourly retries don't each send an error


def due_slot(now, last):
    """The digest slot ("2026-10-29@17") to run now, or None: the latest DIGEST_HOURS hour that has passed today,
    unless it already ran. A slot missed while the machine slept runs on the first tick after."""
    today = now.date().isoformat()
    passed = [h for h in DIGEST_HOURS if h <= now.hour]
    if not passed:
        return None
    slot = f"{today}@{passed[-1]}"
    # Before digest slots, last_digest held just the date: that's the day's first slot, done.
    done = {last, f"{today}@{DIGEST_HOURS[0]}" if last == today else None}
    return None if slot in done else slot


async def tick(ctx):
    """Hourly. Sends each digest slot once: at its DIGEST_HOUR, or on the first tick after (machine was asleep).
    A failed one (network down, API error) is retried on the next tick instead of skipping it."""
    global failed_on
    now = datetime.now(TZ)
    today = now.date().isoformat()
    if now.weekday() == 6 and now.hour >= 18 and get_setting("last_weekly") != today:  # Sunday evening
        try:
            await send_week(ctx.application.bot)
            set_setting("last_weekly", today)
        except Exception:  # retried next hour; the digest check below still runs
            log.exception("weekly summary")
    slot = due_slot(now, get_setting("last_digest"))
    if get_setting("paused") or not slot:
        return
    if await send_digest(ctx.application, quiet_failure=failed_on == slot):
        set_setting("last_digest", slot)
    else:
        failed_on = slot


def parse_answers(text, n):
    """'1. foo\\n2) bar' -> ['foo', 'bar']. One unnumbered line: 'a, b, c' with exactly n parts is one answer each,
    anything else answers all n. None if the count is off. One question takes the whole reply, paragraphs and all."""
    if n == 1:
        return [re.sub(r"^1[.)]\s*", "", text.strip())] if text.strip() else None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if n > 1 and len(lines) == 1 and not re.match(r"\d+[.)]", lines[0]):
        parts = [p.strip() for p in lines[0].split(",")]
        return parts if len(parts) == n else lines * n
    lines = [re.sub(r"^\d+[.)]\s*", "", line) for line in lines]
    return lines if len(lines) == n else None


async def _wait(kind, tag=None):
    """The reply or tap the running application needs. A tap must carry `tag`, so a button on an older message
    (a "Submit this?" that timed out) can't answer for the application now waiting."""
    global pending
    pending = (kind, asyncio.get_running_loop().create_future(), tag)
    try:
        return await asyncio.wait_for(pending[1], timeout=float(env("ANSWER_TIMEOUT_HOURS", "6")) * 3600)
    finally:
        pending = None


def _answer(kind, value, tag=None):
    """Hand a reply or tap to the application waiting for one. False if none is waiting for this kind (and tag)."""
    # A second quick reply finds it already answered.
    if pending and pending[0] == kind and pending[2] == tag and not pending[1].done():
        pending[1].set_result(value)
        return True
    return False


async def _buttons(bot, kind, png, filename, caption, labels):
    """Send a screenshot with two buttons and wait for a tap: True for the first. The buttons are removed afterwards,
    answered or not, so they can't be tapped later."""
    tag = secrets.token_hex(4)
    message = await bot.send_document(
        CHAT_ID, png, filename=filename, caption=caption[:1024],
        reply_markup=Keyboard([[Button(labels[0], callback_data=f"{kind}:{tag}:yes"),
                                Button(labels[1], callback_data=f"{kind}:{tag}:no")]]))
    try:
        return await _wait(kind, tag)
    finally:
        with contextlib.suppress(Exception):
            await message.edit_reply_markup(None)


def final_url(url):
    """Where a short link (go.vansh.nyc/eZ -> jobs.ashbyhq.com/...) lands, so the right filler picks it up."""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=20) as r:
            return r.url
    except Exception:  # some sites refuse scripts; the link itself is the best we have
        return url


def keep_copy(job, png):
    """applications/<date>-<company>-<title>.png: the filled form as it was about to be sent, so you can see what
    you said on it when they call."""
    folder = ROOT / "applications"
    folder.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", f"{job['company']} {job['title']}".lower()).strip("-")[:80]
    with contextlib.suppress(OSError):
        (folder / f"{datetime.now(TZ).date().isoformat()}-{slug}.png").write_bytes(png)


async def find_form(url):
    """The URL whose form the bot can fill: short links followed, ?gh_jid= pages and pages embedding a form resolved."""
    url = apply.direct_url(await asyncio.to_thread(final_url, url))
    if not fillable(url):
        try:
            url = await apply.embedded_form(url) or url
        except Exception:  # the link itself is still worth handing you
            log.exception("looking for a form on %s", url)
    return url


async def run_apply(job, bot):
    global running, queued
    set_status(job["id"], "queued")  # in memory only until it runs: a restart brings it back (resume_interrupted)
    if apply_lock.locked():
        await bot.send_message(CHAT_ID, f"Queued {job['title']} @ {job['company']}, it starts when "
                                        f"{running['company'] if running else 'the current one'} is done.")
    queued += 1
    try:
        await apply_lock.acquire()
    finally:
        queued -= 1
    try:
        running = job
        await _run_apply(job, bot)
    finally:
        running = None
        apply_lock.release()


async def _run_apply(job, bot):
    job = {**job, "url": await find_form(job["url"])}
    if row := already_applied(job):  # e.g. Apply tapped twice while the first run was going
        await bot.send_message(CHAT_ID, f"You already applied to {job['company']} on {row['date']}.")
        return
    set_status(job["id"], "applying")  # still this at startup = the bot stopped mid-application (resume_interrupted)
    await bot.send_message(CHAT_ID, f"Applying to {job['title']} @ {job['company']}…")

    async def ask(questions):
        if len(questions) == 1:
            await bot.send_message(CHAT_ID, f"{job['company']}: {questions[0]}")
        else:
            numbered = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
            await bot.send_message(CHAT_ID, f"{job['company']} needs a few answers. Reply with one numbered line "
                                            f"each, or one line like 'a, b, c', or one line to answer them all:\n"
                                            f"{numbered}")
        while not (answers := parse_answers(await _wait("text"), len(questions))):
            await bot.send_message(CHAT_ID, f"I need {len(questions)} numbered lines, or one line for all.")
        await bot.send_message(CHAT_ID, "Got it.")
        return answers

    async def confirm(png):
        keep_copy(job, png)
        # A cover letter Claude wrote for this form: you get it to read (and keep), even when submitting unasked.
        if letter := job.pop("cover_letter", None):
            with contextlib.suppress(Exception):
                await bot.send_document(CHAT_ID, letter.read_bytes(), filename=letter.name,
                                        caption=f"Cover letter for {job['company']}")
        if env("CONFIRM_BEFORE_SUBMIT", "1") != "1":
            return True
        return await _buttons(bot, "confirm", png, "application.png", "Submit this?" + watch_hint(),
                              ("Submit", "Cancel"))

    done_button = Keyboard([[Button("I applied ✅", callback_data=f"done:{job['id']}")]])

    async def stuck(reason, png):
        if env("HEADLESS", "1") == "1" and not env("VNC_PASSWORD"):
            caption = f"{reason}.\n\nThis is what I filled. Finish it here: {job['url']}"
            await bot.send_document(CHAT_ID, png, filename="form.png", caption=caption[:1024],
                                    reply_markup=done_button)
            return False
        # Watching mode: the browser stays open on your computer until you tap a button.
        try:
            return await _buttons(bot, "stuck", png, "form.png",
                                  f"{reason}.\n\nThe browser is still open on your computer, finish it there."
                                  + watch_hint(), ("I applied ✅", "Close browser"))
        except asyncio.TimeoutError:  # no tap for hours: close it and move on to the next application
            await bot.send_message(CHAT_ID, f"Closed the browser for {job['company']}. Finish it here if you want: "
                                            f"{job['url']}", reply_markup=done_button)
            return False

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


async def resume_interrupted(app):
    """At startup: applications the bot was queued for or in the middle of when it stopped (crash, restart, reboot)
    get a button again, since nothing else would bring them back. One stopped at or after Submit may have gone
    through, so that one says so instead of offering a plain retry."""
    for job in jobs_with_status(("queued", "applying", "submitting"), 30):
        if already_applied(job):
            set_status(job["id"], "submitted")
            continue
        name = f"{job['title']} @ {job['company']}"
        if job_status(job["id"]) == "submitting":
            text = f"I restarted just after submitting {name}. It may have gone through: check your email."
            buttons = [Button("It went through ✅", callback_data=f"done:{job['id']}"),
                       Button("Apply again", callback_data=f"apply:{job['id']}")]
        else:
            text = f"I restarted before finishing {name}, so it wasn't sent."
            buttons = [Button("Apply again", callback_data=f"apply:{job['id']}"),
                       Button("Skip", callback_data=f"skip:{job['id']}")]
        try:
            await app.bot.send_message(CHAT_ID, text, reply_markup=Keyboard([buttons]))
        except Exception:  # Telegram not reachable yet at boot: it stays as it is for the next start
            log.exception("telling you about %s", job["url"])
            continue
        set_status(job["id"], "shown")


COMMANDS = [  # shown in Telegram's / menu
    ("digest", "Search now"), ("status", "What I'm doing, settings, counts"),
    ("apply", "Apply to a posting: /apply <link>"), ("auto", "Auto-apply to picks scoring N+: /auto 8, /auto off"),
    ("week", "This week's applications and unfinished ones"), ("applied", "Latest applications"),
    ("prep", "Answer the common form questions up front"),
    ("answers", "Saved form answers: /answers 3 <new text> to change"), ("forget", "Delete a saved answer: /forget 3"),
    ("block", "Hide a company or title word: /block phd"), ("unblock", "Undo a block"),
    ("country", "canada | usa | both"), ("count", "Jobs per digest, 5-10"), ("pause", "Stop the daily digest"),
    ("resume", "Restart the daily digest"), ("cost", "Claude spend"),
]


async def on_start(app):
    with contextlib.suppress(Exception):  # cosmetic; never block startup on it
        await app.bot.set_my_commands(COMMANDS)
    try:
        await resume_interrupted(app)
    except Exception:  # never keep the bot from starting
        log.exception("resuming interrupted applications")


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


async def cmd_auto(update, ctx):
    arg = (ctx.args or [""])[0].lower()
    if arg == "off":
        set_setting("auto_min", None)
    elif arg.isdigit():
        set_setting("auto_min", max(0, min(10, int(arg))))
    auto_min = get_setting("auto_min")
    if auto_min is None:
        await update.message.reply_text("Auto-apply is off: tap Apply on the jobs you want.\n"
                                        "/auto 8 applies to every digest pick scoring 8/10 or more by itself.")
        return
    confirm = env("CONFIRM_BEFORE_SUBMIT", "1") == "1"
    await update.message.reply_text(
        f"Auto-apply is on for picks scoring {auto_min}/10 or more on sites I can fill. "
        + ("I'll still send each filled form for your OK (CONFIRM_BEFORE_SUBMIT=1 in .env)." if confirm else
           "They're submitted without asking (CONFIRM_BEFORE_SUBMIT=0).")
        + " Questions I can't answer still come to you. /auto off to stop.")


async def cmd_status(update, ctx):
    auto_min = get_setting("auto_min")
    counts = status_counts()
    lines = [
        f"Up since {STARTED:%b %d %H:%M}. " + ("Paused." if get_setting("paused") else
                                              f"Digests at {', '.join(f'{h}:00' for h in DIGEST_HOURS)} "
                                              f"(last: {get_setting('last_digest').replace('@', ' at ') or 'never'})."),
        f"Searching {', '.join(get_setting('countries'))}, {get_setting('count')} jobs per digest, auto-apply "
        + ("off." if auto_min is None else f"at {auto_min}/10+."),
        f"Applying to: {running['title']} @ {running['company']}" + (" (waiting on you)" if pending else "")
        if running else "Not applying to anything right now.",
    ]
    if queued:
        lines.append(f"{queued} more queued.")
    if sources_now := get_setting("source_counts"):
        lines.append("Last search: " + ", ".join(f"{s} {n}" for s, n in sorted(sources_now.items())) + ".")
    lines.append(f"Jobs seen: {sum(counts.values())}, shown {counts.get('shown', 0)}, "
                 f"submitted {counts.get('submitted', 0)}, finished by you {counts.get('manual', 0)}, "
                 f"applied.csv rows {len(applied_rows())}.")
    await update.message.reply_text("\n".join(lines))


def _answer_lines():
    return [f"{i}. {label[:90]} → {answer[:80]}" for i, (label, answer) in enumerate(apply.load_answers().items(), 1)]


async def _reply_long(update, text):
    """Telegram caps a message at 4096 characters: send long lists in pieces, split between lines."""
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) > 4000:
            await update.message.reply_text(chunk)
            chunk = ""
        chunk += line + "\n"
    await update.message.reply_text(chunk)


async def cmd_answers(update, ctx):
    """/answers lists what the bot reuses on forms; /answers 3 <text> changes one."""
    labels = list(apply.load_answers())
    if ctx.args and ctx.args[0].isdigit() and len(ctx.args) > 1:
        n = int(ctx.args[0])
        if not 1 <= n <= len(labels):
            await update.message.reply_text(f"There's no answer {n}. /answers lists them.")
            return
        apply.save_answers({labels[n - 1]: " ".join(ctx.args[1:])})
        await update.message.reply_text(f"Changed: {_answer_lines()[n - 1]}")
        return
    lines = _answer_lines()
    if not lines:
        await update.message.reply_text("No saved answers yet. They build up as you answer questions on forms.")
        return
    await _reply_long(update, "Saved answers (reused on every form):\n" + "\n".join(lines)
                      + "\n\nChange one: /answers 3 <new answer>. Delete one: /forget 3")


# Asked on most forms and not in profile.yaml: answering them once up front keeps /auto from stopping to ask.
PREP_QUESTIONS = [
    "How did you hear about this job?",
    "Are you willing to relocate for this internship?",
    "When is the earliest you can start?",
    "How long can your internship or co-op term be (e.g. 4, 8, 12 or 16 months)?",
    "Are you able to work on-site or hybrid at the office?",
    "What are your hourly pay expectations?",
    "What are your pronouns?",
    "Have you worked at or interned with this company before?",
]


async def cmd_prep(update, ctx):
    missing = [q for q in PREP_QUESTIONS if q not in apply.load_answers()]
    if not missing:
        await update.message.reply_text("All the common questions are answered. /answers shows them.")
        return
    ctx.application.create_task(prep(ctx.bot, missing))


async def prep(bot, questions):
    if apply_lock.locked():
        await bot.send_message(CHAT_ID, "I'll ask once the current application is done.")
    async with apply_lock:  # so these answers can't be taken as a form's (one thing waits on replies at a time)
        numbered = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
        await bot.send_message(CHAT_ID, "Common application questions: answer them once and I'll reuse them on every "
                                        "form. One numbered line each:\n" + numbered)
        try:
            while not (replies := parse_answers(await _wait("text"), len(questions))):
                await bot.send_message(CHAT_ID, f"I need {len(questions)} numbered lines.")
        except asyncio.TimeoutError:
            return
        apply.save_answers(dict(zip(questions, replies)))
        await bot.send_message(CHAT_ID, "Saved. /answers to review or change them.")


async def cmd_forget(update, ctx):
    labels = list(apply.load_answers())
    if not (ctx.args and ctx.args[0].isdigit() and 1 <= int(ctx.args[0]) <= len(labels)):
        await update.message.reply_text("Send /forget <number> from /answers.")
        return
    label = labels[int(ctx.args[0]) - 1]
    apply.save_answers({}, drop=[label])
    await update.message.reply_text(f"Forgot: {label[:120]}\nI'll ask again next time a form has it.")


async def cmd_block(update, ctx):
    terms = get_setting("blocked")
    if term := block_term(" ".join(ctx.args or [])):
        terms = sorted(set(terms) | {term})
        set_setting("blocked", terms)
        await update.message.reply_text(f"Blocked '{term}': jobs from that company or with it in the title won't "
                                        f"be shown or applied to. /unblock {term} to undo.")
        return
    await update.message.reply_text("Blocked: " + ", ".join(terms) + "\n/unblock <term> to remove one." if terms else
                                    "Nothing blocked. /block <company or title word>, e.g. /block phd")


async def cmd_unblock(update, ctx):
    term, terms = block_term(" ".join(ctx.args or [])), get_setting("blocked")
    if term not in terms:
        await update.message.reply_text("Not blocked. /block lists what is.")
        return
    set_setting("blocked", [t for t in terms if t != term])
    await update.message.reply_text(f"Unblocked '{term}'.")


async def cmd_week(update, ctx):
    await send_week(ctx.bot)


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
        tag, _, choice = arg.partition(":")
        if not _answer(action, choice == "yes", tag):
            await q.message.reply_text("That one's no longer waiting on you.")
        await q.edit_message_reply_markup(None)
        return
    job = get_job(arg)
    if not job:
        return
    if action == "skip":
        await q.edit_message_reply_markup(None)
        set_status(arg, "skipped")
    elif action == "block":
        await q.edit_message_reply_markup(None)
        set_status(arg, "skipped")
        term = block_term(job["company"])
        set_setting("blocked", sorted(set(get_setting("blocked")) | {term}))
        await q.message.reply_text(f"I won't show {job['company']} again. /unblock {term} to undo.")
    elif action == "done":
        await q.edit_message_reply_markup(None)
        if row := already_applied(job):  # e.g. the bot's own submit went through after all
            await q.message.reply_text(f"Already in applied.csv ({row['date']}, {row['status']}).")
            return
        log_application(job, "manual")
        set_status(arg, "manual")
        await q.message.reply_text("Logged in applied.csv.")
    elif row := already_applied(job):
        await q.message.reply_text(f"You already applied to this on {row['date']}.")
    else:
        ctx.application.create_task(run_apply(job, ctx.bot))


async def on_text(update, ctx):
    if not _answer("text", update.message.text):
        await update.message.reply_text("Nothing's waiting on an answer right now. /status shows what I'm doing.")


def check():
    """python bot.py --check: is everything the bot needs in place? Prints each problem; exit code 1 if any."""
    import json
    import sys

    from playwright.sync_api import sync_playwright

    problems, notes = [], []

    def need(ok, problem):
        if not ok:
            problems.append(problem)
        return ok

    need((ROOT / ".env").exists(), "no .env: cp .env.example .env and fill it in")
    if need(env("TELEGRAM_BOT_TOKEN"), "TELEGRAM_BOT_TOKEN is empty in .env (make a bot with @BotFather)"):
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/getMe", timeout=20) as r:
                notes.append(f"Telegram bot: @{json.load(r)['result']['username']}")
        except Exception as e:
            problems.append(f"Telegram rejected TELEGRAM_BOT_TOKEN: {e}")
    need(CHAT_ID, "TELEGRAM_CHAT_ID is empty: run python bot.py, send /start to your bot, put the id it replies in .env")
    if need(env("ANTHROPIC_API_KEY"), "ANTHROPIC_API_KEY is empty in .env"):
        try:
            llm._client().models.retrieve(llm.MODEL)
        except Exception as e:
            problems.append(f"Anthropic API rejected the key or model {llm.MODEL}: {e}")
    if need((ROOT / "profile.yaml").exists(), "no profile.yaml: cp profile.example.yaml profile.yaml and fill it in"):
        for key in ("first_name", "last_name", "email", "phone", "resume_text", "work_auth"):
            need(PROFILE.get(key), f"profile.yaml: {key} is empty")
        if (level := PROFILE.get("degree_level")) and degree_kind(level) not in {k for k, _ in DEGREES}:
            problems.append(f"profile.yaml: degree_level '{level}' isn't one I know (Bachelor's, Master's, PhD, MBA, "
                            "Associate's), so it would hide every listing that names a degree")
        try:
            apply.resume_path()
            for variant in PROFILE.get("resumes") or []:
                need(Path(variant.get("pdf") or "").expanduser().is_file() and variant.get("keywords"),
                     f"profile.yaml resumes: each needs an existing pdf and keywords ({variant})")
        except RuntimeError as e:
            problems.append(str(e))
    if not env("WORKDAY_PASSWORD"):
        notes.append("WORKDAY_PASSWORD is empty, so Workday postings (most big companies) will fail to start")
    if env("CANVAS_TOKEN"):
        notes.append("Quercus scanning on")
    try:
        with sync_playwright() as p:
            p.chromium.launch().close()
    except Exception as e:
        problems.append(f"Playwright's Chromium won't start ({str(e).splitlines()[0]}): "
                        ".venv/bin/playwright install --with-deps chromium")
    if env("VNC_PASSWORD") and not all(map(shutil.which, ("Xvfb", "x11vnc", "websockify"))):
        problems.append("VNC_PASSWORD is set, so the phone view needs: sudo apt install xvfb x11vnc novnc")
    for note in notes:
        print("·", note)
    for problem in problems:
        print("✗", problem)
    print("All set." if not problems else f"{len(problems)} thing(s) to fix.")
    sys.exit(1 if problems else 0)


def main():
    import sys

    if "--check" in sys.argv:
        check()
    if not env("TELEGRAM_BOT_TOKEN"):
        raise SystemExit("TELEGRAM_BOT_TOKEN isn't set. Run: .venv/bin/python bot.py --check")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if env("VNC_PASSWORD"):
        start_phone_view()
    app = Application.builder().token(env("TELEGRAM_BOT_TOKEN")).post_init(on_start).build()
    me = filters.Chat(CHAT_ID)  # only you can drive the bot
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("digest", cmd_digest, me))
    for name, fn in [("country", cmd_country), ("count", cmd_count), ("pause", cmd_pause),
                     ("resume", cmd_resume), ("applied", cmd_applied), ("cost", cmd_cost),
                     ("apply", cmd_apply), ("auto", cmd_auto), ("status", cmd_status), ("answers", cmd_answers),
                     ("forget", cmd_forget), ("block", cmd_block), ("unblock", cmd_unblock),
                     ("week", cmd_week), ("prep", cmd_prep)]:
        app.add_handler(CommandHandler(name, fn, me))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(me & filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(tick, interval=3600, first=10)
    app.run_polling()


if __name__ == "__main__":
    main()
