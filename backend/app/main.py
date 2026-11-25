"""
API layer.

Thin by design: every endpoint wires HTTP to a module that does the real
work (auth, resume_parsing, job_sources, matching, explainability,
fairness). That separation is what lets each piece be unit-tested
independently of FastAPI.
"""
import os
import pathlib
import uuid

import httpx
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc
from sqlalchemy.orm import Session

from . import (
    auth, explainability, fairness, interview_selection, job_sources, matching, models,
    resume_parsing, schemas, seed as seed_module,
)
from .database import Base, engine, get_db
from .timeutil import utcnow

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="AllocateAI",
    description="AI that turns scarce interview slots into the right opportunity for "
                "every student — real accounts, resume upload, stable quota-aware "
                "allocation, and automatic job-match alerts.",
    version="2.0.0",
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

FRONTEND_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "frontend"


@app.get("/", include_in_schema=False)
def serve_index():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/api/health")
def health():
    ai_enabled = bool(os.environ.get("ANTHROPIC_API_KEY"))
    parser_mode = "claude" if ai_enabled else "heuristic"
    return {
        "status": "ok",
        "product": "AllocateAI",
        "resume_parser": parser_mode,
        "ai_summary_enabled": ai_enabled,
    }


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

def _should_be_admin(email: str, db: Session) -> bool:
    if db.query(models.User).count() == 0:
        return True  # bootstrap: first registered account on a fresh install is admin
    admin_emails = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
    return email.lower() in admin_emails


def get_current_user_and_session(
    authorization: str | None = Header(default=None), db: Session = Depends(get_db)
) -> tuple[models.User, models.Session]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")
    token = authorization.removeprefix("Bearer ")
    session = db.query(models.Session).filter_by(token=token).first()
    if not session or session.expires_at < utcnow():
        raise HTTPException(401, "Session expired or invalid — please log in again")
    user = db.get(models.User, session.user_id)
    if not user:
        raise HTTPException(401, "User not found")
    return user, session


def get_current_user(pair: tuple = Depends(get_current_user_and_session)) -> models.User:
    return pair[0]


def require_admin(user: models.User = Depends(get_current_user)) -> models.User:
    if not user.is_admin:
        raise HTTPException(403, "Admin privileges required")
    return user


def require_company(user: models.User = Depends(get_current_user)) -> models.User:
    if user.role != "company":
        raise HTTPException(403, "Company account required")
    return user


@app.post("/api/auth/register", response_model=schemas.TokenOut)
def register(payload: schemas.RegisterIn, db: Session = Depends(get_db)):
    if db.query(models.User).filter_by(email=payload.email.lower()).first():
        raise HTTPException(409, "An account with this email already exists")

    is_admin = _should_be_admin(payload.email, db)
    role = "admin" if is_admin else payload.role

    user = models.User(
        email=payload.email.lower(),
        name=payload.name,
        password_hash=auth.hash_password(payload.password),
        is_admin=is_admin,
        role=role,
        company_name=payload.company_name.strip() if role == "company" and payload.company_name else None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    session = models.Session(token=auth.new_session_token(), user_id=user.id, expires_at=auth.session_expiry())
    db.add(session)
    db.commit()

    return schemas.TokenOut(
        token=session.token, name=user.name, email=user.email, is_admin=user.is_admin,
        role=user.role, company_name=user.company_name,
    )


@app.post("/api/auth/login", response_model=schemas.TokenOut)
def login(payload: schemas.LoginIn, db: Session = Depends(get_db)):
    user = db.query(models.User).filter_by(email=payload.email.lower()).first()
    if not user or not auth.verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "Incorrect email or password")

    session = models.Session(token=auth.new_session_token(), user_id=user.id, expires_at=auth.session_expiry())
    db.add(session)
    db.commit()

    return schemas.TokenOut(
        token=session.token, name=user.name, email=user.email, is_admin=user.is_admin,
        role=user.role, company_name=user.company_name,
    )


@app.post("/api/auth/logout")
def logout(pair: tuple = Depends(get_current_user_and_session), db: Session = Depends(get_db)):
    _, session = pair
    db.delete(session)
    db.commit()
    return {"status": "logged out"}


@app.get("/api/auth/me", response_model=schemas.TokenOut)
def whoami(user: models.User = Depends(get_current_user)):
    return schemas.TokenOut(
        token="", name=user.name, email=user.email, is_admin=user.is_admin,
        role=user.role, company_name=user.company_name,
    )


# --------------------------------------------------------------------------
# Resume upload + profile
# --------------------------------------------------------------------------

@app.post("/api/resume/upload", response_model=schemas.ResumeUploadResult)
async def upload_resume(
    file: UploadFile = File(...),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    raw_bytes = await file.read()
    if len(raw_bytes) > 8 * 1024 * 1024:
        raise HTTPException(413, "Resume file is too large (max 8MB)")

    try:
        text = resume_parsing.extract_text(file.filename or "resume.txt", raw_bytes)
    except ValueError as e:
        raise HTTPException(400, str(e))

    if not text.strip():
        raise HTTPException(422, "Could not extract any text from this file — try a different format")

    parsed = resume_parsing.parse_resume_text(text)

    applicant = db.query(models.Applicant).filter_by(user_id=user.id).first()
    if applicant is None:
        applicant = models.Applicant(user_id=user.id, category="GENERAL", skills=[], preferred_locations=[])
        db.add(applicant)

    applicant.name = parsed.name or user.name
    applicant.email = parsed.email or user.email
    applicant.skills = parsed.skills
    if parsed.cgpa is not None:
        applicant.cgpa = parsed.cgpa
    if parsed.state is not None:
        applicant.state = parsed.state
    applicant.raw_resume_text = text[:20000]
    applicant.resume_filename = file.filename
    applicant.parsed_at = utcnow()
    # NOTE: category is never touched here — always self-declared by the
    # applicant via PUT /api/profile/me, never inferred from the resume.

    db.commit()
    db.refresh(applicant)

    matches_found = job_sources.notify_applicant_for_existing_internships(db, applicant)

    return schemas.ResumeUploadResult(profile=applicant, matches_found=matches_found)


@app.get("/api/profile/me", response_model=schemas.ProfileOut)
def get_my_profile(user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    applicant = db.query(models.Applicant).filter_by(user_id=user.id).first()
    if not applicant:
        raise HTTPException(404, "No resume uploaded yet — POST /api/resume/upload first")
    return applicant


@app.put("/api/profile/me", response_model=schemas.ProfileOut)
def update_my_profile(
    payload: schemas.ProfileUpdate,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    applicant = db.query(models.Applicant).filter_by(user_id=user.id).first()
    if not applicant:
        raise HTTPException(404, "No resume uploaded yet — POST /api/resume/upload first")

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(applicant, field, value)

    db.commit()
    db.refresh(applicant)
    return applicant


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

@app.get("/api/notifications/me", response_model=list[schemas.NotificationOut])
def list_my_notifications(user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = (
        db.query(models.Notification)
        .filter_by(user_id=user.id)
        .order_by(desc(models.Notification.created_at))
        .all()
    )
    return [
        {
            "id": n.id,
            "internship_id": n.internship_id,
            "internship_title": n.internship.title,
            "company": n.internship.company,
            "score": n.score,
            "message": n.message,
            "created_at": n.created_at,
            "read_at": n.read_at,
        }
        for n in rows
    ]


@app.post("/api/notifications/{notification_id}/read")
def mark_notification_read(
    notification_id: int, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
):
    n = db.query(models.Notification).filter_by(id=notification_id, user_id=user.id).first()
    if not n:
        raise HTTPException(404, "Notification not found")
    n.read_at = utcnow()
    db.commit()
    return {"status": "read"}


# --------------------------------------------------------------------------
# Internships / job board
# --------------------------------------------------------------------------

@app.get("/api/internships", response_model=list[schemas.InternshipOut])
def list_internships(db: Session = Depends(get_db)):
    return db.query(models.Internship).order_by(desc(models.Internship.posted_at)).all()


@app.post("/api/internships/manual", response_model=schemas.InternshipOut)
def add_manual_internship(
    payload: schemas.InternshipIn, admin: models.User = Depends(require_admin), db: Session = Depends(get_db)
):
    internship = models.Internship(**payload.model_dump(), source="manual_scheme", is_open_market=False)
    db.add(internship)
    db.commit()
    db.refresh(internship)
    job_sources.notify_users_for_new_internship(db, internship)
    return internship


@app.post("/api/internships/paste", response_model=schemas.IngestResult)
def paste_internship(
    payload: schemas.ManualPasteIn, admin: models.User = Depends(require_admin), db: Session = Depends(get_db)
):
    source = job_sources.ManualPasteSource(
        raw_text=payload.raw_text, company=payload.company, location=payload.location, title=payload.title
    )
    return job_sources.ingest_postings(db, source)


@app.post("/api/internships/ingest", response_model=schemas.IngestResult)
def ingest_internships(
    payload: schemas.IngestSourceIn, admin: models.User = Depends(require_admin), db: Session = Depends(get_db)
):
    if payload.source == "greenhouse":
        source = job_sources.GreenhouseSource(payload.identifier)
    elif payload.source == "lever":
        source = job_sources.LeverSource(payload.identifier)
    else:
        raise HTTPException(400, "source must be 'greenhouse' or 'lever'")

    try:
        return job_sources.ingest_postings(db, source)
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Upstream API returned {e.response.status_code} — check the identifier and try again")
    except httpx.HTTPError:
        raise HTTPException(502, "Could not reach the upstream job API — check your network connection")


@app.post("/api/seed-internships")
def seed_demo_internships(
    count: int = Query(12, ge=1, le=200),
    seed_value: int = Query(42),
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Adds sample SCHEME internships for demo purposes. Never touches
    applicants/users/notifications — those are always real."""
    return seed_module.generate_demo_internships(db, count=count, seed=seed_value)


# --------------------------------------------------------------------------
# Company / employer console
#
# A company account only ever sees and acts on postings under its own
# registered company name — enforced server-side, never taken from client
# input. This is the third stakeholder named in the pitch (alongside
# students and the placement team/admin): a company can post its own
# openings, see the few interviews the AI flagged as worth holding for
# those postings, record outcomes for them, and see its own final results
# once an admin finalizes an allocation cycle.
# --------------------------------------------------------------------------

@app.get("/api/company/internships", response_model=list[schemas.InternshipOut])
def list_company_internships(company: models.User = Depends(require_company), db: Session = Depends(get_db)):
    return (
        db.query(models.Internship)
        .filter(models.Internship.company == company.company_name)
        .order_by(desc(models.Internship.posted_at))
        .all()
    )


@app.post("/api/company/internships", response_model=schemas.InternshipOut)
def add_company_internship(
    payload: schemas.CompanyInternshipIn, company: models.User = Depends(require_company), db: Session = Depends(get_db)
):
    """A company posts its own opening. Set to participate in the same
    scoring → interview-selection → final-match pipeline as scheme
    internships (quotas={} means every seat is open-merit — no reservation
    categories apply to a company's own posting), so a company's postings
    get genuinely AI-matched and can surface real interviews for the
    company to act on, not just a notification-only external listing.
    Always attributed to the caller's own registered company name."""
    internship = models.Internship(
        title=payload.title,
        company=company.company_name,
        sector=payload.sector,
        required_skills=payload.required_skills,
        location=payload.location,
        state="",
        total_capacity=payload.total_capacity,
        quotas={},
        source="company_posted",
        is_open_market=False,
    )
    db.add(internship)
    db.commit()
    db.refresh(internship)
    job_sources.notify_users_for_new_internship(db, internship)
    return internship


@app.get("/api/company/interviews", response_model=list[schemas.InterviewOut])
def list_company_interviews(
    cycle_id: str | None = Query(None, description="Defaults to the most recently prepared cycle"),
    company: models.User = Depends(require_company),
    db: Session = Depends(get_db),
):
    company_internship_ids = [
        i.id for i in db.query(models.Internship.id).filter(models.Internship.company == company.company_name).all()
    ]
    if not company_internship_ids:
        return []

    query = db.query(models.Interview).filter(models.Interview.internship_id.in_(company_internship_ids))
    if cycle_id:
        query = query.filter_by(cycle_id=cycle_id)
    else:
        latest = (
            db.query(models.Interview.cycle_id)
            .filter(models.Interview.internship_id.in_(company_internship_ids))
            .order_by(desc(models.Interview.created_at))
            .first()
        )
        if not latest:
            return []
        query = query.filter_by(cycle_id=latest[0])
    rows = query.order_by(models.Interview.id).all()
    return [_interview_to_out(r) for r in rows]


@app.get("/api/company/allocations")
def company_allocations(company: models.User = Depends(require_company), db: Session = Depends(get_db)):
    """The company's own slice of the most recent finalized/allocated run —
    only rows for internships under this company's name."""
    latest = db.query(models.Allocation.run_id).order_by(desc(models.Allocation.created_at)).first()
    if not latest:
        return []
    run_id = latest[0]
    rows = (
        db.query(models.Allocation)
        .join(models.Internship, models.Allocation.internship_id == models.Internship.id)
        .filter(models.Allocation.run_id == run_id, models.Internship.company == company.company_name)
        .all()
    )
    return [
        {
            "applicant_id": row.applicant_id,
            "applicant_name": row.applicant.name,
            "internship_id": row.internship_id,
            "internship_title": row.internship.title,
            "seat_type": row.seat_type,
            "score": row.score,
        }
        for row in rows
    ]


# --------------------------------------------------------------------------
# Allocation engine (scheme track only)
#
# Two ways to run a cycle:
#
#  * POST /api/allocate — the original one-shot path. Scores everyone, runs
#    the stable match directly off AI-only scores, done. Useful for a quick
#    demo run or as a fallback when the interview stage isn't needed.
#
#  * POST /api/allocate/prepare -> conduct the returned interviews -> POST
#    /api/interviews/{id}/outcome for each -> POST /api/allocate/finalize.
#    This is the full four-stage pipeline from the proposal: score every
#    pair, provisionally match to find contested seats, pick the few
#    (applicant, internship) pairs where an interview could actually change
#    the outcome (interview_selection.py), then run the FINAL stable match
#    using interview outcomes wherever they exist and the original AI score
#    everywhere else.
# --------------------------------------------------------------------------

@app.post("/api/allocate")
def allocate(admin: models.User = Depends(require_admin), db: Session = Depends(get_db)):
    applicants = db.query(models.Applicant).all()
    internships = db.query(models.Internship).filter_by(is_open_market=False).all()
    if not applicants:
        raise HTTPException(400, "No applicants yet — real users need to register and upload a resume first")
    if not internships:
        raise HTTPException(400, "No scheme internships found — add some via /api/internships/manual or /api/seed-internships")

    scores = matching.compute_all_scores(applicants, internships)
    results = matching.run_matching(applicants, internships, scores=scores)
    violations = matching.verify_stability(applicants, internships, results, scores)

    run_id = str(uuid.uuid4())
    internships_by_id = {i.id: i for i in internships}
    applicants_by_id = {a.id: a for a in applicants}

    for r in results:
        if r.internship_id is not None:
            _, components = scores[(r.applicant_id, r.internship_id)]
            internship = internships_by_id[r.internship_id]
            explanation = explainability.build_allocation_explanation(
                r.score, r.seat_type, components, internship.title, internship.company
            )
        else:
            explanation = explainability.build_unallocated_explanation(matching.DEFAULT_MIN_ELIGIBLE_SCORE)

        applicant_name = applicants_by_id[r.applicant_id].name
        ai_summary = explainability.generate_ai_summary(explanation["narrative"], applicant_name)

        db.add(models.Allocation(
            run_id=run_id,
            applicant_id=r.applicant_id,
            internship_id=r.internship_id,
            seat_type=r.seat_type,
            score=r.score,
            explanation=explanation,
            ai_summary=ai_summary,
        ))
    db.commit()

    report = fairness.build_fairness_report(run_id, applicants, internships, results)

    return {
        "run_id": run_id,
        "total_applicants": report["total_applicants"],
        "allocated": report["total_allocated"],
        "unallocated": report["total_unallocated"],
        "stability_violations": violations,
    }


def _load_applicants_and_scheme_internships(db: Session):
    applicants = db.query(models.Applicant).all()
    internships = db.query(models.Internship).filter_by(is_open_market=False).all()
    if not applicants:
        raise HTTPException(400, "No applicants yet — real users need to register and upload a resume first")
    if not internships:
        raise HTTPException(400, "No scheme internships found — add some via /api/internships/manual or /api/seed-internships")
    return applicants, internships


def _interview_to_out(iv: models.Interview) -> dict:
    return {
        "id": iv.id,
        "cycle_id": iv.cycle_id,
        "applicant_id": iv.applicant_id,
        "applicant_name": iv.applicant.name,
        "internship_id": iv.internship_id,
        "internship_title": iv.internship.title,
        "company": iv.internship.company,
        "bucket": iv.bucket,
        "ai_score": iv.ai_score,
        "confidence": iv.confidence,
        "margin": iv.margin,
        "reason": iv.reason,
        "status": iv.status,
        "outcome_score": iv.outcome_score,
        "notes": iv.notes,
        "created_at": iv.created_at,
        "completed_at": iv.completed_at,
    }


@app.post("/api/allocate/prepare", response_model=schemas.PrepareCycleOut)
def prepare_allocation_cycle(
    max_interviews: int = Query(20, ge=1, le=200),
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Stage 1 + 2a + 3: score every pair, run a provisional match to find
    contested seats, then select the few pairs worth an interview. Returns
    a cycle_id — conduct the listed interviews, record outcomes via POST
    /api/interviews/{id}/outcome, then POST /api/allocate/finalize with the
    same cycle_id."""
    applicants, internships = _load_applicants_and_scheme_internships(db)

    scores = matching.compute_all_scores(applicants, internships)
    provisional = matching.run_matching(applicants, internships, scores=scores)
    candidates = interview_selection.select_key_interviews(
        applicants, internships, scores, provisional, max_interviews=max_interviews
    )

    cycle_id = str(uuid.uuid4())
    for c in candidates:
        db.add(models.Interview(
            cycle_id=cycle_id,
            applicant_id=c.applicant_id,
            internship_id=c.internship_id,
            bucket=c.bucket,
            ai_score=c.score,
            confidence=c.confidence,
            margin=c.margin,
            reason=c.reason,
        ))
    db.commit()

    provisional_allocated = sum(1 for r in provisional if r.internship_id is not None)
    return {
        "cycle_id": cycle_id,
        "total_applicants": len(applicants),
        "total_internships": len(internships),
        "provisional_allocated": provisional_allocated,
        "provisional_unallocated": len(applicants) - provisional_allocated,
        "interviews_selected": len(candidates),
    }


@app.get("/api/interviews", response_model=list[schemas.InterviewOut])
def list_interviews(
    cycle_id: str | None = Query(None, description="Defaults to the most recently prepared cycle"),
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    query = db.query(models.Interview)
    if cycle_id:
        query = query.filter_by(cycle_id=cycle_id)
    else:
        latest = db.query(models.Interview.cycle_id).order_by(desc(models.Interview.created_at)).first()
        if not latest:
            return []
        query = query.filter_by(cycle_id=latest[0])
    rows = query.order_by(models.Interview.id).all()
    return [_interview_to_out(r) for r in rows]


@app.post("/api/interviews/{interview_id}/outcome", response_model=schemas.InterviewOut)
def submit_interview_outcome(
    interview_id: int,
    payload: schemas.InterviewOutcomeIn,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Record what actually happened in the interview. `outcome_score` is
    the same [0, 1] scale as the AI's compatibility score — an interviewer
    confirming the AI's estimate should submit roughly the same number; an
    interview that revealed the applicant is a much better (or worse) fit
    than the resume alone suggested should submit accordingly. This is the
    number that overrides the AI-only estimate for this pair when the cycle
    is finalized.

    Either the placement-team admin, or the company that owns this
    particular internship, may record the outcome — a real interview
    happens between the applicant and the company, so the company is the
    other party who'd actually know the result."""
    iv = db.get(models.Interview, interview_id)
    if not iv:
        raise HTTPException(404, "Interview not found")
    is_owning_company = user.role == "company" and user.company_name == iv.internship.company
    if not (user.is_admin or is_owning_company):
        raise HTTPException(403, "Admin privileges, or the owning company account, required")
    iv.outcome_score = payload.outcome_score
    iv.notes = payload.notes
    iv.status = "COMPLETED"
    iv.completed_at = utcnow()
    db.commit()
    db.refresh(iv)
    return _interview_to_out(iv)


@app.post("/api/allocate/finalize", response_model=schemas.FinalizeCycleOut)
def finalize_allocation_cycle(
    cycle_id: str, admin: models.User = Depends(require_admin), db: Session = Depends(get_db)
):
    """Stage 2b + 4: run the FINAL stable match. Interview outcomes recorded
    for this cycle_id override the AI-only score for exactly those pairs;
    every other pair — the vast majority, since interview slots are scarce
    by design — still uses the original AI score from scoring.py. Then runs
    the same stability check and fairness audit as the quick /api/allocate
    path, so a cycle that went through interviews is held to the identical
    fairness/stability bar as one that didn't."""
    applicants, internships = _load_applicants_and_scheme_internships(db)

    scores = matching.compute_all_scores(applicants, internships)
    cycle_interviews = db.query(models.Interview).filter_by(cycle_id=cycle_id).all()
    adjusted_scores = interview_selection.apply_interview_outcomes(scores, cycle_interviews)
    interview_by_pair = {(iv.applicant_id, iv.internship_id): iv for iv in cycle_interviews if iv.outcome_score is not None}

    results = matching.run_matching(applicants, internships, scores=adjusted_scores)
    violations = matching.verify_stability(applicants, internships, results, adjusted_scores)

    run_id = cycle_id  # reuse the cycle_id as the run_id so allocations trace back to the interviews used
    internships_by_id = {i.id: i for i in internships}
    applicants_by_id = {a.id: a for a in applicants}

    for r in results:
        if r.internship_id is not None:
            _, components = adjusted_scores[(r.applicant_id, r.internship_id)]
            internship = internships_by_id[r.internship_id]
            explanation = explainability.build_allocation_explanation(
                r.score, r.seat_type, components, internship.title, internship.company
            )
            interview = interview_by_pair.get((r.applicant_id, r.internship_id))
            if interview is not None:
                explanation["interview_note"] = (
                    f"An interview was held for this match: the AI's pre-interview estimate was "
                    f"{interview.ai_score:.2f}; the recorded interview outcome ({interview.outcome_score:.2f}) "
                    f"was used for the final match instead."
                )
        else:
            explanation = explainability.build_unallocated_explanation(matching.DEFAULT_MIN_ELIGIBLE_SCORE)

        applicant_name = applicants_by_id[r.applicant_id].name
        ai_summary = explainability.generate_ai_summary(explanation["narrative"], applicant_name)

        db.add(models.Allocation(
            run_id=run_id,
            applicant_id=r.applicant_id,
            internship_id=r.internship_id,
            seat_type=r.seat_type,
            score=r.score,
            explanation=explanation,
            ai_summary=ai_summary,
        ))
    db.commit()

    report = fairness.build_fairness_report(run_id, applicants, internships, results)
    return {
        "run_id": run_id,
        "cycle_id": cycle_id,
        "interviews_used": len(cycle_interviews),
        "interviews_completed": len(interview_by_pair),
        "total_applicants": report["total_applicants"],
        "allocated": report["total_allocated"],
        "unallocated": report["total_unallocated"],
        "stability_violations": violations,
    }


@app.get("/api/allocations/latest", response_model=list[schemas.AllocationOut])
def latest_allocations(db: Session = Depends(get_db)):
    latest = db.query(models.Allocation.run_id).order_by(desc(models.Allocation.created_at)).first()
    if not latest:
        return []
    run_id = latest[0]
    rows = db.query(models.Allocation).filter(models.Allocation.run_id == run_id).all()
    return [
        {
            "applicant_id": row.applicant_id,
            "applicant_name": row.applicant.name,
            "category": row.applicant.category,
            "internship_id": row.internship_id,
            "internship_title": row.internship.title if row.internship else None,
            "company": row.internship.company if row.internship else None,
            "seat_type": row.seat_type,
            "score": row.score,
            "explanation": row.explanation,
            "ai_summary": row.ai_summary,
        }
        for row in rows
    ]


@app.post("/api/admin/reset-demo")
def reset_demo_data(admin: models.User = Depends(require_admin), db: Session = Depends(get_db)):
    """Wipes all demo data (applicants, internships, allocations, interviews,
    notifications) so the app can be re-run cleanly for another demo round.
    Never touches User/Session rows — every registered login (including the
    admin running this) stays intact."""
    deleted = {
        "notifications": db.query(models.Notification).delete(),
        "interviews": db.query(models.Interview).delete(),
        "allocations": db.query(models.Allocation).delete(),
        "applicants": db.query(models.Applicant).delete(),
        "internships": db.query(models.Internship).delete(),
    }
    db.commit()
    return {
        "status": "reset",
        "message": "Demo data cleared. Registered accounts and logins were not affected.",
        "deleted": deleted,
    }


@app.get("/api/fairness/latest", response_model=schemas.FairnessReport)
def latest_fairness(db: Session = Depends(get_db)):
    latest = db.query(models.Allocation.run_id).order_by(desc(models.Allocation.created_at)).first()
    if not latest:
        raise HTTPException(404, "No allocation run yet — POST /api/allocate first")
    run_id = latest[0]

    applicants = db.query(models.Applicant).all()
    internships = db.query(models.Internship).filter_by(is_open_market=False).all()
    rows = db.query(models.Allocation).filter(models.Allocation.run_id == run_id).all()
    results = [
        matching.MatchResult(row.applicant_id, row.internship_id, row.seat_type, row.score)
        for row in rows
    ]
    return fairness.build_fairness_report(run_id, applicants, internships, results)
