from typing import Literal, Optional
from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict, EmailStr, model_validator

RESERVATION_CATEGORIES = Literal["GENERAL", "OBC", "SC", "ST", "EWS", "PWD"]


# --- auth --------------------------------------------------------------

class RegisterIn(BaseModel):
    name: str
    email: EmailStr
    password: str = Field(min_length=8)
    role: Literal["applicant", "company"] = "applicant"
    company_name: Optional[str] = None

    @model_validator(mode="after")
    def _company_name_required_for_company_role(self):
        if self.role == "company" and not (self.company_name or "").strip():
            raise ValueError("company_name is required when role is 'company'")
        return self


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    token: str
    name: str
    email: str
    is_admin: bool
    role: str
    company_name: Optional[str] = None


# --- applicant profile ---------------------------------------------------

class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    name: str
    email: str
    skills: list[str]
    cgpa: Optional[float]
    category: str
    state: Optional[str]
    preferred_locations: list[str]
    resume_filename: Optional[str]
    parsed_at: Optional[datetime]
    profile_confirmed: bool


class ProfileUpdate(BaseModel):
    category: Optional[RESERVATION_CATEGORIES] = None
    state: Optional[str] = None
    preferred_locations: Optional[list[str]] = None
    skills: Optional[list[str]] = None
    cgpa: Optional[float] = Field(default=None, ge=0, le=10)
    profile_confirmed: Optional[bool] = None


class ResumeUploadResult(BaseModel):
    profile: ProfileOut
    matches_found: int


# --- internships ---------------------------------------------------------

class InternshipIn(BaseModel):
    title: str
    company: str
    sector: str = "General"
    required_skills: list[str]
    location: str
    state: str = ""
    total_capacity: int = Field(gt=0, default=1)
    quotas: dict[str, int] = {}


class InternshipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    company: str
    sector: str
    required_skills: list[str]
    location: str
    state: str
    total_capacity: int
    quotas: dict[str, int]
    source: str
    is_open_market: bool
    apply_url: Optional[str]
    posted_at: datetime


class ManualPasteIn(BaseModel):
    raw_text: str = Field(min_length=20)
    company: str
    location: str = "Not specified"
    title: Optional[str] = None


class CompanyInternshipIn(BaseModel):
    """Same shape as InternshipIn but without `company` — a company user can
    only ever post under their own registered company name, enforced
    server-side, never taken from client input."""
    title: str
    sector: str = "General"
    required_skills: list[str]
    location: str
    total_capacity: int = Field(gt=0, default=1)


class IngestSourceIn(BaseModel):
    source: str  # "greenhouse" or "lever"
    identifier: str  # board_token or company_slug


class IngestResult(BaseModel):
    postings_fetched: int
    new_postings: int
    notifications_sent: int


# --- notifications ---------------------------------------------------------

class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    internship_id: int
    internship_title: str
    company: str
    score: float
    message: str
    created_at: datetime
    read_at: Optional[datetime]


# --- interviews (Stage 3: scarce-interview-slot selection) -----------------

class InterviewOut(BaseModel):
    id: int
    cycle_id: str
    applicant_id: int
    applicant_name: str
    internship_id: int
    internship_title: str
    company: str
    bucket: str
    ai_score: float
    confidence: float
    margin: float
    reason: str
    status: str
    outcome_score: Optional[float]
    notes: Optional[str]
    created_at: datetime
    completed_at: Optional[datetime]


class InterviewOutcomeIn(BaseModel):
    outcome_score: float = Field(ge=0, le=1)
    notes: Optional[str] = None


class PrepareCycleOut(BaseModel):
    cycle_id: str
    total_applicants: int
    total_internships: int
    provisional_allocated: int
    provisional_unallocated: int
    interviews_selected: int


class FinalizeCycleOut(BaseModel):
    run_id: str
    cycle_id: str
    interviews_used: int
    interviews_completed: int
    total_applicants: int
    allocated: int
    unallocated: int
    stability_violations: list[str]


# --- allocation / matching (unchanged shape) --------------------------------

class ComponentBreakdown(BaseModel):
    raw_score: float
    weight: float
    contribution: float
    detail: str


class Explanation(BaseModel):
    overall_score: float
    seat_type: Optional[str]
    components: dict[str, ComponentBreakdown]
    narrative: str
    interview_note: Optional[str] = None


class AllocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    applicant_id: int
    applicant_name: str
    category: str
    internship_id: Optional[int]
    internship_title: Optional[str]
    company: Optional[str]
    seat_type: Optional[str]
    score: Optional[float]
    explanation: Optional[Explanation]
    ai_summary: Optional[str] = None


class FairnessCategoryStat(BaseModel):
    category: str
    total_applicants: int
    allocated: int
    allocation_rate: float


class FairnessReport(BaseModel):
    run_id: str
    total_applicants: int
    total_allocated: int
    total_unallocated: int
    overall_allocation_rate: float
    by_category: list[FairnessCategoryStat]
    four_fifths_rule_ratio: float
    four_fifths_rule_pass: bool
    quota_fill: list[dict]
