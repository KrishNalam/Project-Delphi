"""Local state: SQLite for jobs we've seen and settings, applied.csv for the application log."""
import csv
import json
import re
import sqlite3
import threading
from datetime import date

from config import ROOT

def open_db(path):
    db = sqlite3.connect(path, check_same_thread=False)
    db.executescript("""
    create table if not exists jobs (id text primary key, data text, status text default 'new',
                                     seen_at text default current_timestamp);
    create table if not exists settings (key text primary key, value text);
    """)
    if "updated_at" not in [row[1] for row in db.execute("pragma table_info(jobs)")]:  # databases from before /week
        db.execute("alter table jobs add column updated_at text")
        db.commit()
    return db


DB = open_db(ROOT / "delphi.db")
LOCK = threading.RLock()  # the digest runs in a worker thread while Telegram handlers use the same connection
# auto_min: digest picks scoring at least this get applied to without a tap (None = off).
DEFAULTS = {"countries": ["canada", "usa"], "count": 8, "paused": False, "last_digest": "", "workday_accounts": [],
            "auto_min": None, "source_counts": {},
            "blocked": [], "last_weekly": ""}


def _run(sql, args=()):
    with LOCK:
        rows = DB.execute(sql, args).fetchall()
        DB.commit()
        return rows


def get_setting(key):
    rows = _run("select value from settings where key = ?", (key,))
    return json.loads(rows[0][0]) if rows else DEFAULTS[key]


def set_setting(key, value):
    _run("insert or replace into settings values (?, ?)", (key, json.dumps(value)))


def is_new(job_id):
    """Not shown to you yet (unknown jobs count as new)."""
    rows = _run("select status from jobs where id = ?", (job_id,))
    return not rows or rows[0][0] == "new"


def save_job(job):
    _run("insert or ignore into jobs (id, data) values (?, ?)", (job["id"], json.dumps(job)))


def update_job(job):
    """Overwrite a saved job's data, e.g. with the posting's real URL once a short link is followed."""
    _run("update jobs set data = ? where id = ?", (json.dumps(job), job["id"]))


def get_job(job_id):
    rows = _run("select data from jobs where id = ?", (job_id,))
    return json.loads(rows[0][0]) if rows else None


def set_status(job_id, status):
    _run("update jobs set status = ?, updated_at = current_timestamp where id = ?", (status, job_id))


def jobs_with_status(statuses, days):
    """Jobs whose status became one of `statuses` in the last `days` days, newest first."""
    marks = ", ".join("?" * len(statuses))
    rows = _run(f"select data from jobs where status in ({marks}) and updated_at >= datetime('now', ?) "
                "order by updated_at desc", (*statuses, f"-{days} days"))
    return [json.loads(data) for (data,) in rows]


def job_status(job_id):
    rows = _run("select status from jobs where id = ?", (job_id,))
    return rows[0][0] if rows else None


def status_counts():
    """{'shown': 12, 'submitted': 3, ...} over every job ever seen."""
    return dict(_run("select status, count(*) from jobs group by status"))


# applied.csv is the source of truth for "did I already apply?". Add rows by hand for
# applications you sent outside the bot and those jobs get skipped too.
APPLIED_CSV = ROOT / "applied.csv"
CSV_FIELDS = ["date", "company", "title", "location", "url", "source", "status"]
DONE = {"submitted", "manual"}


# Query parameters that name the job itself (Greenhouse embeds: ?gh_jid=, ?token=), unlike tracking ones (?src=).
JOB_PARAMS = re.compile(r"^(gh_jid|token|jobid|job_id|jid|jk|reqid|requisitionid)=", re.I)


def canonical(url):
    """One posting, many links: drop tracking parameters and a trailing /apply or /application."""
    base, _, query = url.partition("?")
    keep = sorted(p for p in query.split("&") if JOB_PARAMS.match(p))
    base = re.sub(r"/(apply|application)$", "", base.rstrip("/"))
    return base + ("?" + "&".join(keep) if keep else "")


def _key(company, title):
    return re.sub(r"[^a-z0-9]", "", f"{company}{title}".lower())


def applied_rows(path=None):
    path = path or APPLIED_CSV
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def already_applied(job, path=None):
    """The applied.csv row for this job (same URL, or same company + title), else None."""
    key, url = _key(job["company"], job["title"]), canonical(job["url"])
    for row in applied_rows(path):
        if row["status"] in DONE and (canonical(row["url"]) == url or _key(row["company"], row["title"]) == key):
            return row
    return None


def log_application(job, status, path=None):
    path = path or APPLIED_CSV
    new = not path.exists()
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, CSV_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow({"date": date.today().isoformat(), "company": job["company"], "title": job["title"],
                         "location": "; ".join(job["locations"]), "url": job["url"], "source": job["source"],
                         "status": status})
