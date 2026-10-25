"""Every Claude call lives here. Set DELPHI_MODEL in .env to change the model."""
import json
from functools import cache

import anthropic
from pydantic import BaseModel

from config import PROFILE, env

MODEL = env("DELPHI_MODEL", "claude-opus-5-5")


@cache
def _client():
    return anthropic.Anthropic()


def _ask(prompt, schema):
    r = _client().messages.parse(
        model=MODEL, max_tokens=16000, output_format=schema,
        messages=[{"role": "user", "content": prompt}],
        # If a safety classifier declines, the API retries on Anthropic's recommended fallback model.
        extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
        extra_body={"fallbacks": "default"},
    )
    if r.stop_reason == "refusal" or r.parsed_output is None:
        raise RuntimeError(f"Claude returned no usable output (stop_reason={r.stop_reason})")
    return r.parsed_output


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
    listing = "\n".join(f"[{i}] {j['title']} @ {j['company']} ({'; '.join(j['locations'])}) {j['description'][:400]}"
                        for i, j in enumerate(jobs))
    picks = _ask(
        f"Candidate resume:\n{PROFILE.get('resume_text', '')}\n\n"
        f"Work authorization: {PROFILE.get('work_auth', 'unknown')}. Rank down roles the candidate "
        "likely can't take because of it.\n\n"
        f"Below are internship postings. Pick the {n} this candidate is most qualified for and most likely to get "
        f"an interview at. Score each 0-10 and give a one-line reason (max 15 words). Best first.\n\n{listing}",
        Ranking).picks
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
    return _ask("Extract every job or internship posting from these course pages. Use the application link as url "
                "(empty string if there is none). Return an empty list if there are no postings.\n\n" + text,
                Postings).postings


class Fill(BaseModel):
    index: int
    value: str


class FormPlan(BaseModel):
    fills: list[Fill]
    unknown: list[int]


def map_fields(job, fields, answers):
    """What to put in each form field. Required fields we can't answer truthfully come back in `unknown`."""
    profile = {k: v for k, v in PROFILE.items() if k not in ("resume_text", "search_terms")}
    return _ask(
        f"You are filling out a job application for {job['title']} at {job['company']}.\n\n"
        f"Applicant profile: {json.dumps(profile, default=str)}\n\n"
        f"Resume:\n{PROFILE.get('resume_text', '')}\n\n"
        f"Answers the applicant gave to earlier application questions: {json.dumps(answers)}\n\n"
        f"Form fields: {json.dumps(fields)}\n\n"
        "Rules:\n"
        "- Only use facts from the profile, resume or earlier answers. Never invent facts.\n"
        "- Questions that restate a profile fact (graduation date, school, degree, work authorization) are "
        "answerable: reformat the fact to fit the field (e.g. April 2028 -> 04/2028 or 2028-04-30).\n"
        "- For fields with options, value must be exactly one of the options "
        "(checkbox groups: comma-separate several).\n"
        "- Self-identification questions with no profile answer: pick the decline/prefer-not-to-say option.\n"
        "- Short free-text questions like 'why this company': 2-3 honest sentences grounded in the resume.\n"
        "- Skip file fields. Skip optional fields you can't answer.\n"
        "- Put the index of every required field you can't answer from these facts in `unknown`.",
        FormPlan)
