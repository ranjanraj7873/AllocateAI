"""
Demo data for the SCHEME internship track only.

This module used to also generate synthetic applicants. It no longer does —
applicants now only ever come from a real registered user uploading their
resume (see resume_parsing.py + POST /api/resume/upload). This module only
seeds sample government-scheme internships (the reservation-quota track),
purely so the allocation engine has something to run against in a demo
without needing real government postings entered by hand.

Deliberately additive, never destructive: it does NOT delete existing
applicants, users, notifications, or previously-seeded internships — wiping
those on every demo click would be actively harmful once real people have
real accounts and real notification history.
"""
import random

from sqlalchemy.orm import Session

from .models import Internship

SECTOR_SKILLS: dict[str, list[str]] = {
    "Software Engineering": ["Python", "Java", "SQL", "Git", "REST APIs", "Data Structures", "Cloud Basics"],
    "Data Science": ["Python", "SQL", "Statistics", "Pandas", "Machine Learning", "Data Visualization"],
    "Digital Marketing": ["SEO", "Content Writing", "Social Media", "Google Analytics", "Email Marketing"],
    "Finance": ["Excel", "Financial Modeling", "Accounting", "SQL", "Power BI"],
    "Mechanical Design": ["AutoCAD", "SolidWorks", "Manufacturing Processes", "MATLAB"],
    "Human Resources": ["Recruitment", "Communication", "MS Office", "Employee Engagement"],
    "Electronics": ["Circuit Design", "Embedded C", "PCB Design", "MATLAB"],
    "Public Policy": ["Research", "Report Writing", "Data Analysis", "Communication"],
}

COMPANY_PREFIX = ["Nova", "Bharat", "Skyline", "Green Grid", "Metro", "Sunrise", "Vertex", "Indus", "Orbit", "Pinnacle"]
COMPANY_SUFFIX = ["Systems", "Textiles Ltd", "Analytics", "Energy", "Logistics", "Technologies", "Industries", "Solutions"]

STATES = ["Maharashtra", "Karnataka", "Delhi", "Tamil Nadu", "Uttar Pradesh", "West Bengal", "Gujarat", "Rajasthan", "Telangana", "Kerala"]
CITIES_BY_STATE: dict[str, list[str]] = {
    "Maharashtra": ["Mumbai", "Pune"],
    "Karnataka": ["Bengaluru", "Mysuru"],
    "Delhi": ["New Delhi"],
    "Tamil Nadu": ["Chennai", "Coimbatore"],
    "Uttar Pradesh": ["Lucknow", "Noida"],
    "West Bengal": ["Kolkata"],
    "Gujarat": ["Ahmedabad", "Surat"],
    "Rajasthan": ["Jaipur"],
    "Telangana": ["Hyderabad"],
    "Kerala": ["Kochi", "Thiruvananthapuram"],
}


def illustrative_quota_split(capacity: int, rng: random.Random) -> dict[str, int]:
    """Commonly cited central-government reservation percentages, for demo
    data only — see models.py docstring. Not a statement of actual scheme
    policy for any given cycle."""
    if capacity < 4:
        return {}
    quotas = {}
    for cat, frac in [("SC", 0.15), ("ST", 0.075), ("OBC", 0.27), ("EWS", 0.10)]:
        seats = int(capacity * frac)
        if seats > 0:
            quotas[cat] = seats
    return quotas


def generate_demo_internships(db: Session, count: int = 12, seed: int = 42) -> dict:
    rng = random.Random(seed)
    created = 0

    for _ in range(count):
        sector = rng.choice(list(SECTOR_SKILLS))
        skill_pool = SECTOR_SKILLS[sector]
        required = rng.sample(skill_pool, k=min(len(skill_pool), rng.randint(3, 5)))
        state = rng.choice(STATES)
        city = rng.choice(CITIES_BY_STATE[state])
        company = f"{rng.choice(COMPANY_PREFIX)} {rng.choice(COMPANY_SUFFIX)}"
        capacity = rng.randint(4, 15)
        quotas = illustrative_quota_split(capacity, rng)

        db.add(Internship(
            title=f"{sector} Intern",
            company=company,
            sector=sector,
            required_skills=required,
            location=city,
            state=state,
            total_capacity=capacity,
            quotas=quotas,
            source="demo_scheme",
            is_open_market=False,
        ))
        created += 1

    db.commit()
    return {"internships_created": created}
