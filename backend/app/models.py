"""
ORM models.

Design notes:
- `Applicant` rows are now created ONLY by a real registered user uploading
  a resume (see resume_parsing.py + POST /api/resume/upload) — there is no
  synthetic-applicant generator anymore. `user_id` is the source of truth
  for "who is this."
- `category` (the reservation category — SC/ST/OBC/EWS/PWD/GENERAL) is
  ALWAYS self-declared by the applicant, never inferred from resume text.
  That's a legal/fairness requirement, not just a technical choice: a
  reservation category is a certified status, not something a parser should
  guess at. The parser never touches this field.
- `Internship.quotas` stores {category: seat_count}; uncovered seats are
  implicitly "OPEN" (open merit). Quota values are illustrative — see
  seed.py — and only apply to `is_open_market=False` (scheme) postings;
  externally aggregated postings (Greenhouse/Lever/pasted) don't carry
  government reservation categories, so they default to no quotas and are
  excluded from the batch allocation round entirely.
"""
from sqlalchemy import Column, Integer, String, Float, JSON, ForeignKey, DateTime, Boolean, Text
from sqlalchemy.orm import relationship

from .database import Base
from .timeutil import utcnow


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    name = Column(String, nullable=False)
    is_admin = Column(Boolean, nullable=False, default=False)
    role = Column(String, nullable=False, default="applicant")  # "applicant" | "admin" | "company"
    company_name = Column(String, nullable=True)  # set only when role == "company"
    created_at = Column(DateTime, default=utcnow)

    applicant = relationship("Applicant", back_populates="user", uselist=False)


class Session(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True, index=True)
    token = Column(String, unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=utcnow)
    expires_at = Column(DateTime, nullable=False)

    user = relationship("User")


class Applicant(Base):
    __tablename__ = "applicants"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)

    name = Column(String, nullable=False)
    email = Column(String, nullable=False)
    skills = Column(JSON, nullable=False, default=list)
    cgpa = Column(Float, nullable=True)  # 0-10 scale; not every resume states one
    category = Column(String, nullable=False, default="GENERAL")  # always self-declared
    state = Column(String, nullable=True)
    preferred_locations = Column(JSON, nullable=False, default=list)
    preferences = Column(JSON, nullable=True)

    raw_resume_text = Column(Text, nullable=True)
    resume_filename = Column(String, nullable=True)
    parsed_at = Column(DateTime, nullable=True)
    profile_confirmed = Column(Boolean, nullable=False, default=False)

    user = relationship("User", back_populates="applicant")


class Internship(Base):
    __tablename__ = "internships"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    company = Column(String, nullable=False)
    sector = Column(String, nullable=False, default="General")
    required_skills = Column(JSON, nullable=False, default=list)
    location = Column(String, nullable=False)
    state = Column(String, nullable=False, default="")
    total_capacity = Column(Integer, nullable=False, default=1)
    quotas = Column(JSON, nullable=False, default=dict)

    source = Column(String, nullable=False, default="manual_scheme")
    external_id = Column(String, nullable=True, index=True)
    is_open_market = Column(Boolean, nullable=False, default=False)
    apply_url = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    posted_at = Column(DateTime, default=utcnow)


class Allocation(Base):
    __tablename__ = "allocations"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String, index=True, nullable=False)
    applicant_id = Column(Integer, ForeignKey("applicants.id"), nullable=False)
    internship_id = Column(Integer, ForeignKey("internships.id"), nullable=True)
    seat_type = Column(String, nullable=True)
    score = Column(Float, nullable=True)
    explanation = Column(JSON, nullable=True)
    ai_summary = Column(Text, nullable=True)  # optional Claude-rewritten plain-English summary
    created_at = Column(DateTime, default=utcnow)

    applicant = relationship("Applicant")
    internship = relationship("Internship")


class Interview(Base):
    """
    One (applicant, internship, bucket) pair flagged by
    interview_selection.select_key_interviews() as worth an actual
    interview — because it's both near a capacity cutoff AND something the
    AI's own confidence score says it isn't sure about (see scoring.py /
    interview_selection.py docstrings). `cycle_id` ties a batch of
    interviews to the allocation cycle that produced them (POST
    /api/allocate/prepare) so the matching finalize step (POST
    /api/allocate/finalize) knows exactly which outcomes to apply.

    `outcome_score` stays null until an admin records a result via POST
    /api/interviews/{id}/outcome; until then, the final match uses the
    original AI-only score for that pair, same as if no interview had been
    selected at all.
    """
    __tablename__ = "interviews"

    id = Column(Integer, primary_key=True, index=True)
    cycle_id = Column(String, index=True, nullable=False)
    applicant_id = Column(Integer, ForeignKey("applicants.id"), nullable=False)
    internship_id = Column(Integer, ForeignKey("internships.id"), nullable=False)
    bucket = Column(String, nullable=False)

    ai_score = Column(Float, nullable=False)
    confidence = Column(Float, nullable=False)
    margin = Column(Float, nullable=False)
    reason = Column(String, nullable=False)

    status = Column(String, nullable=False, default="PENDING")  # PENDING | COMPLETED
    outcome_score = Column(Float, nullable=True)
    notes = Column(String, nullable=True)

    created_at = Column(DateTime, default=utcnow)
    completed_at = Column(DateTime, nullable=True)

    applicant = relationship("Applicant")
    internship = relationship("Internship")


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    internship_id = Column(Integer, ForeignKey("internships.id"), nullable=False)
    score = Column(Float, nullable=False)
    message = Column(String, nullable=False)
    created_at = Column(DateTime, default=utcnow)
    read_at = Column(DateTime, nullable=True)

    internship = relationship("Internship")
