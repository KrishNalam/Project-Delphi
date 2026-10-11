"""Local state: SQLite for jobs we've seen and settings, applied.csv for the application log."""
import json
import sqlite3

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


def is_seen(job_id):
    return DB.execute("select 1 from jobs where id = ?", (job_id,)).fetchone() is not None


def save_job(job):
    DB.execute("insert or ignore into jobs (id, data) values (?, ?)", (job["id"], json.dumps(job)))
    DB.commit()


def get_job(job_id):
    row = DB.execute("select data from jobs where id = ?", (job_id,)).fetchone()
    return json.loads(row[0]) if row else None


def set_status(job_id, status):
    DB.execute("update jobs set status = ? where id = ?", (status, job_id))
    DB.commit()
