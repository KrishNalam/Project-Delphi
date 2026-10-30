# Work queue

Shared ledger for Project Delphi: what's been done, what's verified, what's next. Whoever picks this up (person
or agent) updates it in the same commit as the work: move items between sections, add a dated line to the log.

Status keys: `[ ]` todo · `[~]` in progress · `[x]` done · `[!]` blocked (say on what)

## Now: to get it running on this server

- [ ] Fill `.env` (from `.env.example`): `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `WORKDAY_PASSWORD`, optional
      `CANVAS_TOKEN`, `DIGEST_HOUR`, `CONFIRM_BEFORE_SUBMIT`, `COVER_LETTER`, `ANSWER_TIMEOUT_HOURS`.
- [ ] Fill `profile.yaml` (from `profile.example.yaml`), including `resume_pdf`, `resume_text`, `work_auth`,
      and optionally `degree_level`, `terms`, `resumes:`, `transcript_pdf`.
- [ ] `.venv/bin/python bot.py`, send `/start` to the bot, put the chat id in `.env` as `TELEGRAM_CHAT_ID`, Ctrl-C.
- [ ] `./deploy.sh` (runs `bot.py --check` first; installs + starts the systemd user service only if it passes).
- [ ] In Telegram: `/prep`, then `/digest`; tap Apply on one job and watch it through "Submit this?".
- [ ] After a few good manual runs: `/auto 8` (keep `CONFIRM_BEFORE_SUBMIT=1` until you trust it).
- [ ] Review and merge PR #1: https://github.com/KrishNalam/Project-Delphi/pull/1 (needs Krish; mh-anwar is read-only
      upstream, so branches go to the fork `mh-anwar/Project-Delphi` and PR from there).

## Verify once real keys exist (couldn't be done without them)

- [ ] A real Claude call end to end: `/digest` ranking, a form page's `map_fields`, a cover letter. (Request shape was
      only checked against a local fake API.)
- [ ] A real **submit** on each of Greenhouse, Lever, Ashby, Workable (pick postings you actually want). Watch for the
      success text match (`apply.SUCCESS`) and Greenhouse's emailed-code step.
- [ ] Workday past sign-in: account creation, email verification ask, each wizard page, Review, Submit. Only tested
      live up to the Create Account form.
- [ ] Phone view (`VNC_PASSWORD`): needs `sudo apt install x11vnc novnc` here (Xvfb is installed, the others aren't).
- [ ] `/week` on a real Sunday evening; a second `DIGEST_HOUR` slot firing.

## Backlog (ideas, roughly by value)

- [ ] Oracle Cloud HCM forms (22 live postings): email + emailed-code sign-in per application, then a wizard.
- [ ] iCIMS (10 live postings): account + iframe-based forms.
- [ ] LinkedIn-only postings: find the company's own posting (Greenhouse/Lever/Ashby public job APIs by company
      slug). Slug guessing missed 6/6 in a spot check, so it needs a smarter lookup.
- [ ] Outcome tracking: `/outcome <company> interview|rejected|offer` in a separate column of `applied.csv`.
- [ ] Strip cookie-banner text from `posting_text` before it goes to Claude (cosmetic, a few tokens).
- [ ] SmartRecruiters: **won't do**. It blocks headless browsers; getting past that would mean evading bot detection.

## Done (branch `improvements`, PR #1, 2026-10-29)

Everything below was verified with the offline tests (`test_delphi.py`, `test_forms.py`, green in CI on 3.10/3.12)
and, for form filling, live on real postings with Claude stubbed and **no submit**.

Form filling
- [x] Greenhouse (job-boards) dropdowns: visible options only, scoped to the widget's popup; choices read for Claude
- [x] No premature submit: dropdown fallback never presses Enter with no list open, or on a known list with no match
- [x] Ashby: CSS-only required marks; late-rendering forms waited for
- [x] Lever: location suggestion picked; uploads first so its resume parser can't overwrite answers
- [x] Workable support; geo-IP prefills overwritten; cookie banners dismissed; number-only boxes get the number
- [x] `?gh_jid=` / embedded forms resolved to the real form; page loads capped instead of waiting on trackers
- [x] Date placeholders passed to Claude; pre-ticked marketing opt-ins unticked (consent boxes never)
- [x] Cover letters (required uploads; `COVER_LETTER`), posting text for essay answers, `transcript_pdf`, resume versions
- [x] Verification code after Submit asked over Telegram; "why it didn't submit" (CAPTCHA / form errors)

Bot and unattended running
- [x] `/auto`, `/prep`, `/status`, `/week`, `/answers` `/forget`, `/block` `/unblock`, multi-link `/apply`, command menu
- [x] Stale Submit taps can't approve another application (per-request tags); safe double replies
- [x] Digest: retried hourly on failure, several slots a day, picks marked shown only once sent, dedupe by real URL
      and punctuation-free titles, direct links preferred over LinkedIn, degree/term filters, sources roll over yearly,
      quiet-source warnings
- [x] Restarts: queued / mid-apply / mid-submit applications come back with the right buttons; watchdog on hangs
- [x] `bot.py --check`, `deploy.sh` (tested here with a stub bot: installed, ran, restarted ~60s after `kill -9`,
      then removed), screenshot copy of every sent form in `applications/`

Reviews: three independent review passes over the branch; every finding fixed ("Review fixes", "Second review
fixes", "Third review fixes" commits).

## Log

- 2026-10-29: 30 commits on `improvements`, PR #1 opened from the fork. Live verification scripts lived in
  `/tmp/delphi-verify` (gone after reboot): `fill_live.py` fills a posting with stubbed answers and screenshots it;
  `e2e.py` runs digest → /auto → apply with a fake Telegram that taps Cancel. Nothing deployed: no secrets on the server.
