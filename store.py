"""Local state: SQLite for jobs we've seen and settings, applied.csv for the application log."""
import csv
import json
import re
import sqlite3
from datetime import date

from config import ROOT

DB = sqlite3.connect(ROOT / "delphi.db", check_same_thread=False)
DB.executescript("""
create table if not exists jobs (id text primary key, data text, status text default 'new',
                                 seen_at text default current_timestamp);
create table if not exists settings (key text primary key, value text);
""")
DEFAULTS = {"countries": ["canada", "usa"], "count": 8, "paused": False, "last_digest": ""}


def get_setting(key):
    row = DB.execute("select value from settings where key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else DEFAULTS[key]


def set_setting(key, value):
    DB.execute("insert or replace into settings values (?, ?)", (key, json.dumps(value)))
    DB.commit()


def is_new(job_id):
    """Not shown to you yet (unknown jobs count as new)."""
    row = DB.execute("select status from jobs where id = ?", (job_id,)).fetchone()
    return row is None or row[0] == "new"


def save_job(job):
    DB.execute("insert or ignore into jobs (id, data) values (?, ?)", (job["id"], json.dumps(job)))
    DB.commit()


def get_job(job_id):
    row = DB.execute("select data from jobs where id = ?", (job_id,)).fetchone()
    return json.loads(row[0]) if row else None


def set_status(job_id, status):
    DB.execute("update jobs set status = ? where id = ?", (status, job_id))
    DB.commit()


# applied.csv is the source of truth for "did I already apply?". Add rows by hand for
# applications you sent outside the bot and those jobs get skipped too.
APPLIED_CSV = ROOT / "applied.csv"
CSV_FIELDS = ["date", "company", "title", "location", "url", "source", "status"]
DONE = {"submitted", "manual"}


def canonical(url):
    """One posting, many links: drop the query string and a trailing /apply or /application."""
    return re.sub(r"/(apply|application)$", "", url.split("?")[0].rstrip("/"))


def _key(company, title):
    return re.sub(r"[^a-z0-9]", "", f"{company}{title}".lower())


def applied_rows(path=APPLIED_CSV):
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def already_applied(job, path=APPLIED_CSV):
    """The applied.csv row for this job (same URL, or same company + title), else None."""
    key, url = _key(job["company"], job["title"]), canonical(job["url"])
    for row in applied_rows(path):
        if row["status"] in DONE and (canonical(row["url"]) == url or _key(row["company"], row["title"]) == key):
            return row
    return None


def log_application(job, status, path=APPLIED_CSV):
    new = not path.exists()
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, CSV_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow({"date": date.today().isoformat(), "company": job["company"], "title": job["title"],
                         "location": "; ".join(job["locations"]), "url": job["url"], "source": job["source"],
                         "status": status})
