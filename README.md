# Project Delphi

Every morning 5–10 internships you're qualified for land on your phone. Tap **Apply** and a browser
fills out the application. Anything it can't answer, it asks you in one message and remembers for next time.
Or turn on `/auto 8` and it applies to every strong match by itself.

```
sources ─► filter ─► rank (Claude) ─► Telegram digest ─► [Apply] ─► Playwright fills the form
                                                                     ├─ unknown fields? asks you
                                                                     ├─ screenshot ─► [Submit]
                                                                     └─ logged in applied.csv
```

| What | Where |
|---|---|
| Sources: SimplifyJobs + vanshb03 Summer internship lists (moves to next year's by itself), LinkedIn + Indeed (python-jobspy), Quercus pages and announcements | `sources.py` |
| Rules: internship/co-op titles, Canada/USA toggle, drops US postings that won't sponsor, other-degree (PhD-only) and other-term postings, `/block`ed companies | `filters.py` |
| Claude: ranking, Quercus extraction, mapping form fields to your profile | `llm.py` |
| Browser autofill for Greenhouse, Lever, Ashby, Workable (and company pages that embed them) | `apply.py` |
| Workday: account, sign-in, then the wizard page by page | `workday.py` |
| Telegram bot, digest schedule, apply queue | `bot.py` |
| `delphi.db` (seen jobs, settings), `applied.csv` (applications) | `store.py` |

## Setup

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/playwright install --with-deps chromium
cp .env.example .env && cp profile.example.yaml profile.yaml   # fill both in
```

1. Message [@BotFather](https://t.me/BotFather), `/newbot`, put the token in `.env`.
2. `.venv/bin/python bot.py`, send `/start` to your bot, put the chat id it replies with in `.env`, stop it (Ctrl-C).
3. `.venv/bin/python bot.py --check` lists anything still missing (keys, profile fields, resume, browser).
4. Send `/digest` to try it.

## Keep it running

```
./deploy.sh                       # checks the setup, then installs and starts the service from this folder
journalctl --user -u delphi -f    # logs
```

It starts on boot, restarts within a minute if it crashes, and keeps running after you log out. Re-run
`./deploy.sh` after pulling changes.

Nothing needs to stay open, but the machine has to be on. `DIGEST_HOUR=8,17` sends two a day. If it was asleep
at a digest hour, that digest goes out within an hour of waking up.

## Commands

| | |
|---|---|
| `/digest` | search now |
| `/country canada\|usa\|both` | where to look |
| `/count 5-10` | jobs per digest |
| `/pause`, `/resume` | stop / restart the scheduled digests |
| `/apply <link> [<link> …]` | apply to any postings, e.g. ones you found yourself or want to redo (in turn) |
| `/auto 8`, `/auto off` | apply to digest picks scoring 8/10+ without a tap (sites the bot can fill only) |
| `/status` | what it's doing now, queue, settings, counts |
| `/prep` | answer the common form questions (start date, relocation, pay, …) once, up front |
| `/answers`, `/answers 3 <text>`, `/forget 3` | see, change or delete the answers it reuses on forms |
| `/block <company or word>`, `/unblock` | never show (or auto-apply to) a company, or titles with a word, e.g. `/block phd`. Digest jobs have a 🚫 Company button too |
| `/applied` | latest rows of `applied.csv` |
| `/cost` | Claude spend: last 30 days, average per digest and per application |
| `/week` | this week's applications, and the ones it couldn't finish with their links (also sent Sunday evenings) |

## Phone view

Set `VNC_PASSWORD` in `.env` (max 8 characters) after `sudo apt install xvfb x11vnc novnc`. The bot's
browser then runs on a virtual screen, and the "Submit this?" and "stuck" messages carry a link that opens
it in your phone's browser, where you can scroll, fix a field or finish the form. The link works on your home
Wi-Fi. For anywhere else, install [Tailscale](https://tailscale.com) on the computer and phone and set
`PHONE_VIEW_HOST` to the computer's Tailscale name.

## Hands-off mode

`/auto 8` makes each digest apply to its picks scoring 8/10 or more by itself, one at a time. With
`CONFIRM_BEFORE_SUBMIT=1` you still get each filled form to approve; set it to `0` in `.env` to have them
submitted without asking. Run `/prep` first so the common questions are already answered. Questions it can't
answer from your profile or `answers.json` still come to you; after `ANSWER_TIMEOUT_HOURS` (default 6) without
a reply it hands that one to you and moves on, so answer a few early on and it needs you less and less.

It's built to be left alone: a failed search (network, API) is retried every hour, applications cut off by a
restart come back with an Apply-again button (or "it may have gone through" if it was mid-Submit), a frozen
page is stopped by a watchdog, a source that suddenly returns nothing gets a warning, and `/week` lists anything
that still needs you.

## applied.csv

Every application gets a row: `date, company, title, location, url, source, status`. Status is
`submitted` (the bot did it) or `manual` (you tapped **I applied** after finishing one yourself).
Jobs are checked against this file before they're shown and again before applying, by URL and by
company + title so reposts are caught. Add rows by hand for applications you sent elsewhere.

## Cost

Every Claude call is logged to `usage.csv` (tokens and dollars); `/cost` summarizes it.
Ranking and Quercus scanning use Haiku at low effort on a keyword-shortlisted 60 jobs. Form
filling and cover letters use Opus, but only for fields that aren't already known from your profile or saved
answers, and the profile + resume part of that prompt is cached.

## Resume versions

Have an ML resume and a general one? List the extra versions under `resumes:` in `profile.yaml`, each with
keywords. A job whose title contains one of those words gets that version; everything else gets `resume_pdf`.

## What you sent

Every filled form is saved as a full-page screenshot in `applications/<date>-<company>-<title>.png` just before
it's submitted (on Workday, the Review page with every answer), and cover letters in `cover_letters/`.

## Cover letters

When a form requires a cover letter upload, Claude writes one for that job from the posting and your resume
(three short paragraphs, nothing invented) and uploads it as a PDF. Each one is kept in `cover_letters/`.
`COVER_LETTER=always` in `.env` also fills optional cover letter uploads; `never` leaves them all empty. Essay
questions like "Why us?" are answered with the job posting in front of Claude too.

## Answers

Anything you answer is saved in `answers.json` under the question's label and reused on later
forms. Fix a wrong one from Telegram with `/answers` and `/forget`, or edit the file.

## Limits

- Greenhouse, Lever, Ashby, Workable and Workday are auto-filled, including company career pages that embed a
  Greenhouse form (`?gh_jid=`). iCIMS, Oracle, SmartRecruiters and SuccessFactors aren't: you get the link and an
  **I applied** button. Each digest job says which it is.
- Greenhouse sometimes emails a code after Submit: the bot asks you for it.
- Workday: the bot makes an account on each company's Workday site with your email and
  `WORKDAY_PASSWORD`. If a company emails a verification link, it asks you to click it. Work history
  and education come from Workday's own resume parsing; if a page still rejects, the browser is left
  open for you to finish.
- If submit can't be confirmed (usually a CAPTCHA) you get the link to finish by hand.
- LinkedIn is read as a guest only. The bot never logs in, and applies on the company's own site. LinkedIn hides
  that site from guests, so LinkedIn-only postings are "fill it yourself"; when the same job is also on a
  GitHub list or Indeed, that direct link is used instead.

## Dev

```
.venv/bin/python test_delphi.py          # self-check for filters, country parsing, applied.csv
.venv/bin/python test_forms.py           # headless browser: fills and submits a tricky local form, bot flow
.venv/bin/python apply.py <posting url>  # print the form fields the bot sees
```
