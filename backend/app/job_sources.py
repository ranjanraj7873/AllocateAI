"""
Job ingestion.

Honest scoping: there is no reliable way to "scrape any company website" —
most block scrapers outright, and every career page has different markup.
What actually works at scale is pulling from applicant-tracking-system (ATS)
public JSON APIs that don't require auth for read access. Greenhouse and
Lever both publish one, and thousands of real companies post through them.
`ManualPasteSource` covers everything else: paste a job description, it gets
parsed the same way a resume does.

Every source implements `fetch_postings() -> list[RawPosting]`. Orchestration
(`ingest_postings`) is source-agnostic: normalize, dedupe against
`(source, external_id)`, persist, then run the notify pipeline — which
reuses the SAME scoring.compatibility_score used by the allocation engine,
so "does this posting match this applicant" is answered identically whether
the applicant is being scored for the batch allocation round or for a live
alert.
"""
import re
from dataclasses import dataclass

import httpx

from . import models
from .resume_parsing import extract_skills
from .scoring import compatibility_score

NOTIFICATION_THRESHOLD = 0.45


@dataclass
class RawPosting:
    external_id: str | None
    title: str
    company: str
    location: str
    description: str
    apply_url: str | None


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


class GreenhouseSource:
    """Real integration: https://developers.greenhouse.io/job-board.html
    No API key required. `board_token` is the company's slug in their
    Greenhouse board URL, e.g. boards.greenhouse.io/stripe -> "stripe"."""

    source_name = "greenhouse"

    def __init__(self, board_token: str, http_client: httpx.Client | None = None):
        self.board_token = board_token
        self.http_client = http_client or httpx.Client(timeout=15)

    def fetch_postings(self) -> list[RawPosting]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{self.board_token}/jobs"
        resp = self.http_client.get(url, params={"content": "true"})
        resp.raise_for_status()
        data = resp.json()
        postings = []
        for job in data.get("jobs", []):
            postings.append(RawPosting(
                external_id=str(job.get("id")),
                title=job.get("title") or "Untitled role",
                company=job.get("company_name") or self.board_token.replace("-", " ").title(),
                location=(job.get("location") or {}).get("name") or "Not specified",
                description=_strip_html(job.get("content") or ""),
                apply_url=job.get("absolute_url"),
            ))
        return postings


class LeverSource:
    """Real integration: https://api.lever.co/v0/postings/{company}?mode=json
    No API key required. `company_slug` is the company's slug in their Lever
    board URL, e.g. jobs.lever.co/acme -> "acme"."""

    source_name = "lever"

    def __init__(self, company_slug: str, http_client: httpx.Client | None = None):
        self.company_slug = company_slug
        self.http_client = http_client or httpx.Client(timeout=15)

    def fetch_postings(self) -> list[RawPosting]:
        url = f"https://api.lever.co/v0/postings/{self.company_slug}"
        resp = self.http_client.get(url, params={"mode": "json"})
        resp.raise_for_status()
        data = resp.json()
        postings = []
        for job in data:
            categories = job.get("categories") or {}
            description = _strip_html(job.get("descriptionPlain") or job.get("description") or "")
            postings.append(RawPosting(
                external_id=str(job.get("id")),
                title=job.get("text") or "Untitled role",
                company=self.company_slug.replace("-", " ").title(),
                location=categories.get("location") or "Not specified",
                description=description,
                apply_url=job.get("hostedUrl") or job.get("applyUrl"),
            ))
        return postings


class ManualPasteSource:
    """Fallback for any company not on Greenhouse/Lever: paste the posting
    text and it's parsed the same way a resume is (skill-keyword extraction
    over the same vocabulary)."""

    source_name = "manual_paste"

    def __init__(self, raw_text: str, company: str, location: str = "Not specified", title: str | None = None):
        self.raw_text = raw_text
        self.company = company
        self.location = location
        self.title = title

    def fetch_postings(self) -> list[RawPosting]:
        title = self.title or next((ln.strip() for ln in self.raw_text.splitlines() if ln.strip()), "Untitled role")
        return [RawPosting(
            external_id=None,
            title=title[:150],
            company=self.company,
            location=self.location,
            description=self.raw_text,
            apply_url=None,
        )]


def _maybe_notify(db, applicant: models.Applicant, internship: models.Internship, threshold: float) -> bool:
    overall, components = compatibility_score(applicant, internship)
    if overall < threshold or components["skill_match"].raw_score <= 0:
        return False
    already = (
        db.query(models.Notification)
        .filter_by(user_id=applicant.user_id, internship_id=internship.id)
        .first()
    )
    if already:
        return False
    db.add(models.Notification(
        user_id=applicant.user_id,
        internship_id=internship.id,
        score=overall,
        message=f"New match: {internship.title} at {internship.company} — {overall:.0%} compatibility",
    ))
    return True


def notify_users_for_new_internship(db, internship: models.Internship, threshold: float = NOTIFICATION_THRESHOLD) -> int:
    """Run whenever a new posting is added — checks it against every
    registered applicant and creates a notification for anyone above
    threshold with at least some skill overlap."""
    applicants = db.query(models.Applicant).all()
    sent = sum(1 for a in applicants if _maybe_notify(db, a, internship, threshold))
    db.commit()
    return sent


def notify_applicant_for_existing_internships(db, applicant: models.Applicant, threshold: float = NOTIFICATION_THRESHOLD) -> int:
    """The symmetric case — run whenever someone uploads or updates their
    resume, so they immediately see matches against everything already
    posted, not just things posted after they signed up."""
    internships = db.query(models.Internship).all()
    sent = sum(1 for i in internships if _maybe_notify(db, applicant, i, threshold))
    db.commit()
    return sent


def ingest_postings(db, source, default_sector: str = "General") -> dict:
    """Fetch, dedupe, persist, and notify. Returns a summary dict."""
    postings = source.fetch_postings()
    created = []

    for p in postings:
        if p.external_id:
            exists = (
                db.query(models.Internship)
                .filter_by(source=source.source_name, external_id=p.external_id)
                .first()
            )
            if exists:
                continue

        required_skills = extract_skills(p.description) or extract_skills(p.title)
        internship = models.Internship(
            title=p.title,
            company=p.company,
            sector=default_sector,
            required_skills=required_skills,
            location=p.location,
            state="",
            total_capacity=1,
            quotas={},
            source=source.source_name,
            external_id=p.external_id,
            is_open_market=True,
            apply_url=p.apply_url,
            description=p.description[:5000],
        )
        db.add(internship)
        created.append(internship)

    db.commit()
    for internship in created:
        db.refresh(internship)

    notifications_sent = sum(notify_users_for_new_internship(db, i) for i in created)

    return {
        "postings_fetched": len(postings),
        "new_postings": len(created),
        "notifications_sent": notifications_sent,
    }
