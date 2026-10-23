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
mkdir -p ~/.config/systemd/user && cp delphi.service ~/.config/systemd/user/
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
| `/applied` | latest rows of `applied.csv` |

## applied.csv

Every application gets a row: `date, company, title, location, url, source, status`. Status is
`submitted` (the bot did it) or `manual` (you tapped **I applied** after finishing one yourself).
Jobs are checked against this file before they're shown and again before applying, by URL and by
company + title so reposts are caught. Add rows by hand for applications you sent elsewhere.

## Answers

Anything you answer is saved in `answers.json` under the question's label and reused on later
forms. Edit it freely.

## Limits

- Only Greenhouse, Lever and Ashby are auto-filled. Workday, iCIMS, Oracle and SuccessFactors are
  about half of all postings, and for those you get the link and an **I applied** button. Workday
  needs an account per company; that's the next thing to build (an agent loop over multi-page forms).
- If submit can't be confirmed (usually a CAPTCHA) you get the link to finish by hand.
- LinkedIn is read as a guest only. The bot never logs in, and applies on the company's own site.

## Dev

```
python test_delphi.py          # self-check for filters, country parsing, applied.csv
python apply.py <posting url>  # print the form fields the bot sees
```
