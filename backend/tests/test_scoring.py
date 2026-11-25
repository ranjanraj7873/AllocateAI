from dataclasses import dataclass, field

from app import scoring


@dataclass
class FakeApplicant:
    id: int
    skills: list
    cgpa: float
    category: str
    state: str
    preferred_locations: list
    preferences: list | None = None


@dataclass
class FakeInternship:
    id: int
    required_skills: list
    location: str
    state: str
    total_capacity: int = 5
    quotas: dict = field(default_factory=dict)


def test_skill_match_full_overlap():
    score, detail = scoring.skill_match(["Python", "SQL", "Git"], ["python", "sql"])
    assert score == 1.0
    assert "2 of 2" in detail


def test_skill_match_partial_overlap():
    score, _ = scoring.skill_match(["Python"], ["Python", "SQL", "Java", "AWS"])
    assert score == 0.25


def test_skill_match_no_requirements_is_trivially_satisfied():
    score, _ = scoring.skill_match(["Python"], [])
    assert score == 1.0


def test_academic_score_bounds():
    assert scoring.academic_score(10.0)[0] == 1.0
    assert scoring.academic_score(0.0)[0] == 0.0
    assert scoring.academic_score(7.5)[0] == 0.75


def test_academic_score_handles_missing_cgpa():
    score, detail = scoring.academic_score(None)
    assert score == 0.5
    assert "not stated" in detail.lower()


def test_location_score_preferred_city_beats_same_state_beats_other_state():
    preferred = scoring.location_score("Karnataka", ["Bengaluru"], "Bengaluru", "Karnataka")[0]
    same_state = scoring.location_score("Karnataka", ["Mysuru"], "Bengaluru", "Karnataka")[0]
    other_state = scoring.location_score("Karnataka", ["Mysuru"], "Chennai", "Tamil Nadu")[0]
    assert preferred > same_state > other_state


def test_location_score_handles_missing_applicant_location_data():
    score, detail = scoring.location_score(None, [], "Chennai", "Tamil Nadu")
    assert score == 0.5
    assert "did not specify" in detail.lower()


def test_preference_score_ranked_beats_unranked_beats_not_listed():
    ranked_first = scoring.preference_score([5, 3, 1], 5)[0]
    ranked_last = scoring.preference_score([5, 3, 1], 1)[0]
    not_listed = scoring.preference_score([5, 3, 1], 99)[0]
    neutral = scoring.preference_score(None, 99)[0]
    assert ranked_first > ranked_last > not_listed
    assert neutral == 0.5


def test_compatibility_score_is_weighted_sum_of_components():
    applicant = FakeApplicant(
        id=1, skills=["Python", "SQL"], cgpa=8.0, category="GENERAL",
        state="Karnataka", preferred_locations=["Bengaluru"], preferences=None,
    )
    internship = FakeInternship(
        id=10, required_skills=["Python", "SQL"], location="Bengaluru", state="Karnataka",
    )
    overall, components = scoring.compatibility_score(applicant, internship)
    expected = round(sum(c.contribution for c in components.values()), 4)
    assert overall == expected
    assert set(components) == {"skill_match", "preference", "academic", "location"}
    # weights must sum to 1 so overall stays in [0, 1]
    assert round(sum(c.weight for c in components.values()), 4) == 1.0
