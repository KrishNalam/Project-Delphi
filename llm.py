"""Every Claude call lives here. Set DELPHI_MODEL in .env to change the model."""
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
        f"Below are internship postings. Pick the {n} this candidate is most qualified for and most likely to get "
        f"an interview at. Score each 0-10 and give a one-line reason (max 15 words). Best first.\n\n{listing}",
        Ranking).picks
    return [dict(jobs[p.index], score=p.score, why=p.why) for p in picks if 0 <= p.index < len(jobs)][:n]
