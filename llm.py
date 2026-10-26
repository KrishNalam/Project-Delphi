"""Every Claude call lives here, and each call's tokens and cost are logged to usage.csv.

DELPHI_MODEL writes what employers see (form answers). DELPHI_FAST_MODEL does the cheap bulk
work: ranking the daily list and pulling postings out of Quercus pages.
"""
import csv
import json
from datetime import datetime, timedelta
from functools import cache

import anthropic
from pydantic import BaseModel

from config import PROFILE, ROOT, env

MODEL = env("DELPHI_MODEL", "claude-opus-5-5")
FAST_MODEL = env("DELPHI_FAST_MODEL", "claude-haiku-5-5")
# $ per million tokens: (input, output, cache read). Cache writes cost 1.25x input.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
USAGE_CSV = ROOT / "usage.csv"
USAGE_FIELDS = ["time", "kind", "model", "input", "cache_write", "cache_read", "output", "cost"]


@cache
def _client():
    return anthropic.Anthropic()


def cost_of(model, input, cache_write, cache_read, output):
    price_in, price_out, price_read = PRICES[model]
    return (input * price_in + cache_write * price_in * 1.25 + cache_read * price_read + output * price_out) / 1e6


def _log(kind, model, usage, path=USAGE_CSV):
    tokens = {"input": usage.input_tokens, "cache_write": usage.cache_creation_input_tokens or 0,
              "cache_read": usage.cache_read_input_tokens or 0, "output": usage.output_tokens}
    new = not path.exists()
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, USAGE_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow({"time": datetime.now().isoformat(timespec="seconds"), "kind": kind, "model": model,
                         **tokens, "cost": round(cost_of(model, **tokens), 6)})


def _ask(kind, prompt, schema, model=MODEL, effort=None, cached=""):
    """`cached` is a stable prompt prefix (profile, resume, rules) that later calls read from cache at ~5-10%."""
    content = [{"type": "text", "text": prompt}]
    if cached:
        content.insert(0, {"type": "text", "text": cached, "cache_control": {"type": "ephemeral"}})
    kwargs = {"output_config": {"effort": effort}} if effort else {}
    if not model.startswith("claude-haiku"):  # Haiku has no server-side fallback
        # If a safety classifier declines, the API retries on Anthropic's recommended fallback model.
        kwargs |= {"extra_headers": {"anthropic-beta": "server-side-fallback-2026-07-01"},
                   "extra_body": {"fallbacks": "default"}}
    r = _client().messages.parse(model=model, max_tokens=16000, output_format=schema,
                                 messages=[{"role": "user", "content": content}], **kwargs)
    _log(kind, model, r.usage)  # priced at the requested model even if a fallback served it
    if r.stop_reason == "refusal" or r.parsed_output is None:
        raise RuntimeError(f"Claude returned no usable output (stop_reason={r.stop_reason})")
    return r.parsed_output


def usage_summary(path=USAGE_CSV):
    """Average and total spend per kind of call, for /cost."""
    rows = list(csv.DictReader(open(path, newline=""))) if path.exists() else []
    if not rows:
        return "No Claude calls logged yet."
    month_ago = (datetime.now() - timedelta(days=30)).isoformat()
    lines = [f"Last 30 days: ${sum(float(r['cost']) for r in rows if r['time'] >= month_ago):.2f}  "
             f"(all time ${sum(float(r['cost']) for r in rows):.2f}, {len(rows)} calls)"]
    for kind, name in [("rank", "Daily digest"), ("map_fields", "Per application"), ("extract", "Quercus scan")]:
        costs = [float(r["cost"]) for r in rows if r["kind"] == kind]
        if costs:
            lines.append(f"{name}: avg ${sum(costs) / len(costs):.4f} over {len(costs)}")
    return "\n".join(lines)


class Pick(BaseModel):
    index: int
    score: int
    why: str


class Ranking(BaseModel):
    picks: list[Pick]


def rank(jobs, n):
    """The n jobs this candidate is best qualified for, best first, each with a score and a one-line reason."""
    if not jobs:
        return []
    listing = "\n".join(f"[{i}] {j['title']} @ {j['company']} ({'; '.join(j['locations'])}) {j['description'][:200]}"
                        for i, j in enumerate(jobs))
    picks = _ask(
        "rank",
        f"Candidate resume:\n{PROFILE.get('resume_text', '')}\n\n"
        f"Work authorization: {PROFILE.get('work_auth', 'unknown')}. Rank down roles the candidate "
        "likely can't take because of it.\n\n"
        f"Below are internship postings. Pick the {n} this candidate is most qualified for and most likely to get "
        f"an interview at. Score each 0-10 and give a one-line reason (max 15 words). Best first.\n\n{listing}",
        Ranking, model=FAST_MODEL, effort="low").picks
    return [dict(jobs[p.index], score=p.score, why=p.why) for p in picks if 0 <= p.index < len(jobs)][:n]


class Posting(BaseModel):
    company: str
    title: str
    location: str
    url: str


class Postings(BaseModel):
    postings: list[Posting]


def extract_jobs(text):
    """Job/internship postings mentioned in free text (Quercus pages and announcements)."""
    return _ask("extract",
                "Extract every job, internship or co-op posting from these course posts. Links appear as "
                "'Title (url)'; use the posting's own link as url (empty string if none). Put the term type in the "
                "title when the post groups jobs that way, e.g. 'Software Engineer I (Co-op, Winter 2027)'. "
                "location: city and country if stated, else empty. Skip events, info sessions and workshops. "
                "Return an empty list if there are no postings.\n\n" + text,
                Postings, model=FAST_MODEL, effort="low").postings


class Fill(BaseModel):
    index: int
    value: str


class FormPlan(BaseModel):
    fills: list[Fill]
    unknown: list[int]


def map_fields(job, fields, answers):
    """What to put in each form field. Required fields we can't answer truthfully come back in `unknown`."""
    profile = {k: v for k, v in PROFILE.items() if k not in ("resume_text", "search_terms", "experience", "skills")}
    # Same for every application, so it's cached; job, answers and fields change and go after it.
    stable = (
        "You are filling out internship applications for this applicant.\n\n"
        f"Applicant profile: {json.dumps(profile, default=str)}\n\n"
        f"Resume:\n{PROFILE.get('resume_text', '')}\n\n"
        "Rules:\n"
        "- Only use facts from the profile, resume or earlier answers. Never invent facts.\n"
        "- Questions that restate a profile fact (graduation date, school, degree, work authorization) are "
        "answerable: reformat the fact to fit the field (e.g. April 2028 -> 04/2028 or 2028-04-30).\n"
        "- For fields with options, value must be exactly one of the options "
        "(checkbox groups: comma-separate several).\n"
        "- Self-identification questions with no profile answer: pick the decline/prefer-not-to-say option.\n"
        "- Short free-text questions like 'why this company': 2-3 honest sentences grounded in the resume.\n"
        "- GPA: fill it only when the field is required; leave optional GPA fields out.\n"
        "- Skip file fields. Skip optional fields you can't answer.\n"
        "- Put the index of every required field you can't answer from these facts in `unknown`."
    )
    return _ask(
        "map_fields",
        f"Job: {job['title']} at {job['company']}.\n\n"
        f"Answers the applicant gave to earlier application questions: {json.dumps(answers)}\n\n"
        f"Form fields: {json.dumps(fields)}",
        FormPlan, cached=stable)
