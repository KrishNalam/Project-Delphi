# Project Delphi

Every morning 5–10 internships you're qualified for land on your phone. Tap **Apply** and a browser
fills out the application. Anything it can't answer, it asks you in one message and remembers for next time.

```
sources ─► filter ─► rank (Claude) ─► Telegram digest ─► [Apply] ─► Playwright fills the form
                                                                     ├─ unknown fields? asks you
                                                                     ├─ screenshot ─► [Submit]
                                                                     └─ logged in applied.csv
```

| What | Where |
|---|---|
| Sources: SimplifyJobs + vanshb03 Summer 2027 lists, LinkedIn + Indeed (python-jobspy), Quercus pages and announcements | `sources.py` |
| Rules: internship/co-op titles, Canada/USA toggle, drops US postings that won't sponsor | `filters.py` |
| Claude: ranking, Quercus extraction, mapping form fields to your profile | `llm.py` |
| Browser autofill for Greenhouse, Lever, Ashby | `apply.py` |
| Workday: account, sign-in, then the wizard page by page | `workday.py` |
| Telegram bot and daily schedule | `bot.py` |
| `delphi.db` (seen jobs, settings), `applied.csv` (applications) | `store.py` |

## Setup

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/playwright install --with-deps chromium
cp .env.example .env && cp profile.example.yaml profile.yaml   # fill both in
```

1. Message [@BotFather](https://t.me/BotFather), `/newbot`, put the token in `.env`.
2. `.venv/bin/python bot.py`, send `/start` to your bot, put the chat id it replies with in `.env`, restart.
3. Send `/digest` to try it.

## Keep it running

```
mkdir -p ~/.config/systemd/user
sed "s|%h/Projects/Delphi|$PWD|" delphi.service > ~/.config/systemd/user/delphi.service   # run from the repo
systemctl --user enable --now delphi
loginctl enable-linger $USER      # keeps running after you log out
journalctl --user -u delphi -f    # logs
```

Nothing needs to stay open, but the machine has to be on. If it was asleep at `DIGEST_HOUR`, the
digest goes out within an hour of waking up.

## Commands

| | |
|---|---|
| `/digest` | search now |
| `/country canada\|usa\|both` | where to look |
| `/count 5-10` | jobs per digest |
| `/pause`, `/resume` | stop / restart the daily digest |
| `/apply <link>` | apply to any posting, e.g. one you found yourself or want to redo |
| `/applied` | latest rows of `applied.csv` |
| `/cost` | Claude spend: last 30 days, average per digest and per application |

## Phone view

Set `VNC_PASSWORD` in `.env` (max 8 characters) after `sudo apt install xvfb x11vnc novnc`. The bot's
browser then runs on a virtual screen, and the "Submit this?" and "stuck" messages carry a link that opens
it in your phone's browser, where you can scroll, fix a field or finish the form. The link works on your home
Wi-Fi. For anywhere else, install [Tailscale](https://tailscale.com) on the computer and phone and set
`PHONE_VIEW_HOST` to the computer's Tailscale name.

## applied.csv

Every application gets a row: `date, company, title, location, url, source, status`. Status is
`submitted` (the bot did it) or `manual` (you tapped **I applied** after finishing one yourself).
Jobs are checked against this file before they're shown and again before applying, by URL and by
company + title so reposts are caught. Add rows by hand for applications you sent elsewhere.

## Cost

Every Claude call is logged to `usage.csv` (tokens and dollars); `/cost` summarizes it.
Ranking and Quercus scanning use Haiku at low effort on a keyword-shortlisted 60 jobs. Form
filling uses Opus, but only for fields that aren't already known from your profile or saved
answers, and the profile + resume part of that prompt is cached.

## Answers

Anything you answer is saved in `answers.json` under the question's label and reused on later
forms. Edit it freely.

## Limits

- Greenhouse, Lever, Ashby and Workday are auto-filled. iCIMS, Oracle and SuccessFactors aren't: you
  get the link and an **I applied** button.
- Workday: the bot makes an account on each company's Workday site with your email and
  `WORKDAY_PASSWORD`. If a company emails a verification link, it asks you to click it. Work history
  and education come from Workday's own resume parsing; if a page still rejects, the browser is left
  open for you to finish.
- If submit can't be confirmed (usually a CAPTCHA) you get the link to finish by hand.
- LinkedIn is read as a guest only. The bot never logs in, and applies on the company's own site.

## Dev

```
.venv/bin/python test_delphi.py          # self-check for filters, country parsing, applied.csv
.venv/bin/python apply.py <posting url>  # print the form fields the bot sees
```
