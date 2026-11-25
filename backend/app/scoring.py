"""
Compatibility scoring engine.

This is Stage 1 of the two-stage design: turn (applicant, internship) into a
single interpretable compatibility score in [0, 1], plus a component-level
breakdown that the explainability layer and the fairness auditor both reuse
directly — the explanation is not reverse-engineered after the fact, it's a
byproduct of how the score was built.

Why hand-weighted features instead of a black-box model?
For a live, first-cycle government system there is no historical label
("was this allocation good?") to train a supervised model against yet, and a
black box is hard to defend to an applicant who was rejected. So Stage 1
uses transparent, interpretable features combined with explicit weights. As
real acceptance/completion/performance outcomes accumulate cycle over cycle,
`WEIGHTS` below is exactly the artifact you would replace with a learned
model (e.g. logistic regression over the same four features, or a gradient-
boosted model over a richer feature set) — the interface
(`compatibility_score`) doesn't need to change, only its internals.

Every sub-score returns (value_in_[0,1], human_readable_detail) so the
explanation is generated for free, not bolted on afterwards.

Confidence / uncertainty
-------------------------
Each component also carries an `is_uncertain` flag, set directly from
whether its *inputs* were actually present (missing CGPA, no stated
location/preferences, no required skills listed) rather than parsed out of
the detail string — that keeps the flag tied to the real data condition,
not to incidental wording. `confidence()` turns those flags into a single
[0, 1] number: 1.0 minus the total weight of components that leaned on a
neutral fallback. This is what Stage 3 (interview_selection.py) uses to
decide which few (applicant, internship) pairs are worth spending a scarce
interview slot on: a borderline pair the AI is *not* confident about is
exactly the case an interview can usefully resolve; a borderline pair with
high confidence is just... borderline, and an interview won't tell you
anything an outcome-history model couldn't already.
"""
from dataclasses import dataclass

# Tunable, and this is exactly the thing you would replace with learned
# weights once you have outcome data. Kept as named constants (not magic
# numbers) so a reviewer can see the policy at a glance.
WEIGHTS = {
    "skill_match": 0.40,
    "preference": 0.30,
    "academic": 0.20,
    "location": 0.10,
}


def skill_match(applicant_skills: list[str], required_skills: list[str]) -> tuple[float, str]:
    """Fraction of required skills the applicant covers.

    Deliberately simple (set overlap) rather than embedding similarity: it's
    deterministic, needs no model weights to download, and — crucially — is
    trivially auditable ("show me which skills matched"). Swap in a sentence-
    embedding cosine-similarity here if you want fuzzy matching (e.g. "React"
    vs "React.js"); the rest of the pipeline is unaffected either way.
    """
    a = {s.strip().lower() for s in applicant_skills}
    r = {s.strip().lower() for s in required_skills}
    if not r:
        return 1.0, "Internship specifies no required skills"
    matched = a & r
    score = len(matched) / len(r)
    matched_list = ", ".join(sorted(matched)) if matched else "none"
    detail = f"{len(matched)} of {len(r)} required skills matched ({matched_list})"
    return round(score, 4), detail


def academic_score(cgpa: float | None) -> tuple[float, str]:
    if cgpa is None:
        return 0.5, "CGPA not stated on resume (neutral score used)"
    score = max(0.0, min(1.0, cgpa / 10.0))
    return round(score, 4), f"CGPA {cgpa:.2f}/10"


def location_score(applicant_state: str | None, applicant_preferred_locations: list[str] | None,
                    internship_location: str, internship_state: str) -> tuple[float, str]:
    if not applicant_state and not applicant_preferred_locations:
        return 0.5, "Applicant did not specify a location preference (neutral score used)"
    locs = {l.strip().lower() for l in (applicant_preferred_locations or [])}
    if internship_location.strip().lower() in locs:
        return 1.0, f"{internship_location} is one of the applicant's preferred locations"
    if applicant_state and applicant_state.strip().lower() == internship_state.strip().lower():
        return 0.5, f"Same state ({internship_state}) though not the exact preferred city"
    return 0.15, f"Different state ({internship_state}) than the applicant's home state ({applicant_state or 'unspecified'})"


def preference_score(preferences: list[int] | None, internship_id: int) -> tuple[float, str]:
    if not preferences:
        return 0.5, "Applicant submitted no explicit preference ranking (neutral score used)"
    if internship_id not in preferences:
        return 0.1, "This internship was not among the applicant's ranked choices"
    rank = preferences.index(internship_id) + 1
    score = max(0.0, 1.0 - (rank - 1) / len(preferences))
    return round(score, 4), f"Ranked #{rank} of {len(preferences)} choices submitted"


@dataclass
class ScoredComponent:
    raw_score: float
    weight: float
    contribution: float
    detail: str
    is_uncertain: bool = False


def compatibility_score(applicant, internship, weights: dict = None) -> tuple[float, dict[str, ScoredComponent]]:
    """
    applicant / internship: ORM objects (or any object exposing the same
    attributes) — see models.Applicant / models.Internship.
    Returns (overall_score_0_to_1, {component_name: ScoredComponent}).
    """
    w = weights or WEIGHTS

    s_skill, d_skill = skill_match(applicant.skills, internship.required_skills)
    s_acad, d_acad = academic_score(applicant.cgpa)
    s_loc, d_loc = location_score(
        applicant.state, applicant.preferred_locations, internship.location, internship.state
    )
    s_pref, d_pref = preference_score(applicant.preferences, internship.id)

    raw = {
        "skill_match": (s_skill, d_skill),
        "preference": (s_pref, d_pref),
        "academic": (s_acad, d_acad),
        "location": (s_loc, d_loc),
    }

    # Uncertainty is derived from the actual inputs being absent, not from
    # string-matching the detail text — the flag reflects a real data
    # condition (nothing was stated) rather than incidental wording.
    uncertain = {
        "skill_match": not internship.required_skills,
        "preference": not applicant.preferences,
        "academic": applicant.cgpa is None,
        "location": not applicant.state and not applicant.preferred_locations,
    }

    components = {
        name: ScoredComponent(
            raw_score=score, weight=w[name], contribution=round(score * w[name], 4),
            detail=detail, is_uncertain=uncertain[name],
        )
        for name, (score, detail) in raw.items()
    }
    overall = round(sum(c.contribution for c in components.values()), 4)
    return overall, components


def confidence(components: dict[str, ScoredComponent]) -> float:
    """1.0 minus the total weight of components that leaned on a neutral
    fallback because their real input was missing. A pair where every
    component had real data behind it gets confidence 1.0; a pair where,
    say, CGPA (weight 0.20) and location (weight 0.10) were both missing
    gets confidence 0.70 — the score is still computed and usable, but the
    AI is explicitly less sure about it. This is the number Stage 3
    (interview_selection.py) uses to decide which borderline pairs are
    worth an actual interview instead of being trusted at face value."""
    uncertain_weight = sum(c.weight for c in components.values() if c.is_uncertain)
    return round(max(0.0, 1.0 - uncertain_weight), 4)
