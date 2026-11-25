"""
Tests for interview_selection.py — the Stage 3 "which few interviews are
worth holding" logic that was the headline idea of the original proposal.
"""
from dataclasses import dataclass, field

from app import interview_selection, matching, scoring


@dataclass
class FakeApplicant:
    id: int
    skills: list
    cgpa: float | None
    category: str
    state: str | None
    preferred_locations: list
    preferences: list | None = None


@dataclass
class FakeInternship:
    id: int
    required_skills: list
    location: str
    state: str
    total_capacity: int = 1
    quotas: dict = field(default_factory=dict)
    title: str = "Test Internship"
    company: str = "Test Co"


def make_internship(iid, capacity=1, required_skills=None):
    return FakeInternship(
        id=iid, required_skills=required_skills or ["Python"], location="Pune", state="Maharashtra",
        total_capacity=capacity,
    )


def make_applicant(aid, cgpa=8.0, skills=None, state="Maharashtra", locations=None, preferences=None):
    return FakeApplicant(
        id=aid, skills=skills or ["Python"], cgpa=cgpa, category="GENERAL",
        state=state, preferred_locations=locations or ["Pune"], preferences=preferences,
    )


# ---------------------------------------------------------------------------
# scoring.confidence()
# ---------------------------------------------------------------------------

def test_confidence_is_1_when_every_component_has_real_data():
    i = make_internship(10)
    a = make_applicant(1, preferences=[i.id])  # explicit preference so that component isn't a neutral fallback
    _, components = scoring.compatibility_score(a, i)
    assert scoring.confidence(components) == 1.0


def test_confidence_drops_when_cgpa_and_location_are_missing():
    i = make_internship(10)
    a = FakeApplicant(
        id=2, skills=["Python"], cgpa=None, category="GENERAL", state=None, preferred_locations=[],
        preferences=[i.id],  # keep preference certain so only academic + location are being tested
    )
    _, components = scoring.compatibility_score(a, i)
    conf = scoring.confidence(components)
    # academic (0.20) + location (0.10) both fell back to neutral defaults
    assert conf == round(1.0 - (0.20 + 0.10), 4)
    assert components["academic"].is_uncertain is True
    assert components["location"].is_uncertain is True
    assert components["skill_match"].is_uncertain is False
    assert components["preference"].is_uncertain is False


# ---------------------------------------------------------------------------
# select_key_interviews()
# ---------------------------------------------------------------------------

def test_no_interviews_selected_when_every_bucket_has_free_capacity():
    """If nobody was turned away in the provisional match, there's nothing
    contested — scarce interview slots shouldn't be spent on empty seats."""
    applicants = [make_applicant(1), make_applicant(2)]
    internships = [make_internship(10, capacity=5)]
    scores = matching.compute_all_scores(applicants, internships)
    provisional = matching.run_matching(applicants, internships, scores=scores)
    candidates = interview_selection.select_key_interviews(applicants, internships, scores, provisional)
    assert candidates == []


def test_contested_low_confidence_pair_is_selected():
    """Two applicants, one seat, and the loser is missing data (so the AI
    isn't confident) and close to the cutoff — exactly the case an
    interview should be spent on."""
    strong = make_applicant(1, cgpa=9.0)
    borderline = FakeApplicant(
        id=2, skills=["Python"], cgpa=None, category="GENERAL", state=None, preferred_locations=[],
    )
    internships = [make_internship(10, capacity=1)]
    applicants = [strong, borderline]

    scores = matching.compute_all_scores(applicants, internships)
    provisional = matching.run_matching(applicants, internships, scores=scores)

    candidates = interview_selection.select_key_interviews(
        applicants, internships, scores, provisional, margin_window=0.5
    )
    assert len(candidates) >= 1
    ids = {c.applicant_id for c in candidates}
    assert 2 in ids  # the low-confidence, near-cutoff applicant is flagged


def test_max_interviews_caps_the_result():
    applicants = [
        FakeApplicant(id=n, skills=["Python"], cgpa=None, category="GENERAL", state=None, preferred_locations=[])
        for n in range(1, 8)
    ]
    internships = [make_internship(10, capacity=3)]
    scores = matching.compute_all_scores(applicants, internships)
    provisional = matching.run_matching(applicants, internships, scores=scores)
    candidates = interview_selection.select_key_interviews(
        applicants, internships, scores, provisional, max_interviews=2, margin_window=1.0
    )
    assert len(candidates) <= 2


def test_candidates_ranked_by_impact_descending():
    applicants = [
        FakeApplicant(id=n, skills=["Python"], cgpa=None, category="GENERAL", state=None, preferred_locations=[])
        for n in range(1, 6)
    ]
    internships = [make_internship(10, capacity=2)]
    scores = matching.compute_all_scores(applicants, internships)
    provisional = matching.run_matching(applicants, internships, scores=scores)
    candidates = interview_selection.select_key_interviews(
        applicants, internships, scores, provisional, margin_window=1.0
    )
    impacts = [c.impact for c in candidates]
    assert impacts == sorted(impacts, reverse=True)


# ---------------------------------------------------------------------------
# apply_interview_outcomes()
# ---------------------------------------------------------------------------

@dataclass
class FakeInterview:
    applicant_id: int
    internship_id: int
    outcome_score: float | None


def test_apply_interview_outcomes_overrides_only_completed_pairs():
    applicants = [make_applicant(1), make_applicant(2)]
    internships = [make_internship(10, capacity=1)]
    scores = matching.compute_all_scores(applicants, internships)

    interviews = [
        FakeInterview(applicant_id=1, internship_id=10, outcome_score=0.99),
        FakeInterview(applicant_id=2, internship_id=10, outcome_score=None),  # not conducted yet
    ]
    adjusted = interview_selection.apply_interview_outcomes(scores, interviews)

    assert adjusted[(1, 10)][0] == 0.99
    assert adjusted[(2, 10)][0] == scores[(2, 10)][0]  # untouched, no outcome recorded
    # original dict is not mutated
    assert scores[(1, 10)][0] != 0.99
