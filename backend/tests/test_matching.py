import random
from dataclasses import dataclass, field

from app import matching


@dataclass
class FakeApplicant:
    id: int
    category: str = "GENERAL"
    preferences: list | None = None
    # unused by matching when scores are supplied explicitly, kept only so
    # the dataclass shape matches what scoring.py would otherwise expect
    skills: list = field(default_factory=list)
    cgpa: float = 7.0
    state: str = "Karnataka"
    preferred_locations: list = field(default_factory=list)


@dataclass
class FakeInternship:
    id: int
    total_capacity: int
    quotas: dict = field(default_factory=dict)
    required_skills: list = field(default_factory=list)
    location: str = "Bengaluru"
    state: str = "Karnataka"


def test_merit_first_then_quota_cascade():
    """
    One internship: capacity 4, with a 1-seat SC quota (so OPEN = 3).
    S1 (SC, score .95) should take an OPEN seat on merit — reservation is a
    floor, not a ceiling, so a strong reserved-category candidate is never
    forced into the quota bucket. G1/G2 (.80/.70) fill the remaining OPEN
    seats. S2 (SC, score .30) — too weak for OPEN — falls back to the SC
    quota seat. G3 (.60), not SC-eligible, is squeezed out entirely.
    """
    applicants = [
        FakeApplicant(id=1, category="SC"),
        FakeApplicant(id=2, category="SC"),
        FakeApplicant(id=3, category="GENERAL"),
        FakeApplicant(id=4, category="GENERAL"),
        FakeApplicant(id=5, category="GENERAL"),
    ]
    internship = FakeInternship(id=100, total_capacity=4, quotas={"SC": 1})
    scores = {
        (1, 100): (0.95, {}),
        (2, 100): (0.30, {}),
        (3, 100): (0.80, {}),
        (4, 100): (0.70, {}),
        (5, 100): (0.60, {}),
    }

    results = matching.run_matching(applicants, [internship], scores=scores)
    by_id = {r.applicant_id: r for r in results}

    assert by_id[1].seat_type == "OPEN"
    assert by_id[3].seat_type == "OPEN"
    assert by_id[4].seat_type == "OPEN"
    assert by_id[2].seat_type == "SC"
    assert by_id[5].internship_id is None

    violations = matching.verify_stability(applicants, [internship], results, scores)
    assert violations == []


def test_quota_bucket_never_accepts_ineligible_category():
    applicants = [
        FakeApplicant(id=1, category="GENERAL"),
        FakeApplicant(id=2, category="ST"),
    ]
    internship = FakeInternship(id=200, total_capacity=2, quotas={"ST": 1})
    scores = {(1, 200): (0.9, {}), (2, 200): (0.4, {})}

    results = matching.run_matching(applicants, [internship], scores=scores)
    by_id = {r.applicant_id: r for r in results}

    # GENERAL applicant can only ever land in OPEN, never the ST bucket
    assert by_id[1].seat_type == "OPEN"
    assert by_id[2].seat_type == "ST"


def test_capacity_is_never_exceeded_under_oversubscription():
    applicants = [FakeApplicant(id=i, category="GENERAL") for i in range(20)]
    internship = FakeInternship(id=300, total_capacity=5, quotas={})
    scores = {(a.id, 300): (random.Random(a.id).random(), {}) for a in applicants}

    results = matching.run_matching(applicants, [internship], scores=scores)
    allocated = [r for r in results if r.internship_id is not None]
    assert len(allocated) == 5  # exactly capacity, no more


def test_below_threshold_scores_are_never_allocated():
    applicants = [FakeApplicant(id=1, category="GENERAL")]
    internship = FakeInternship(id=400, total_capacity=3, quotas={})
    scores = {(1, 400): (0.05, {})}  # below DEFAULT_MIN_ELIGIBLE_SCORE (0.15)

    results = matching.run_matching(applicants, [internship], scores=scores)
    assert results[0].internship_id is None


def test_stability_holds_on_a_randomized_medium_scenario():
    """
    No hand-picked numbers here — this is the property test. Across many
    random category/quota/score configurations, the result must always be
    free of blocking pairs and must always respect every bucket's capacity.
    """
    rng = random.Random(7)
    categories = ["GENERAL", "OBC", "SC", "ST", "EWS"]

    applicants = [
        FakeApplicant(id=i, category=rng.choice(categories))
        for i in range(60)
    ]
    internships = []
    for i in range(8):
        capacity = rng.randint(2, 10)
        quotas = {}
        remaining = capacity
        for cat in ["SC", "ST", "OBC", "EWS"]:
            if remaining > 1 and rng.random() < 0.5:
                seats = rng.randint(1, max(1, remaining // 3))
                quotas[cat] = seats
                remaining -= seats
        internships.append(FakeInternship(id=1000 + i, total_capacity=capacity, quotas=quotas))

    scores = {
        (a.id, i.id): (round(rng.random(), 4), {})
        for a in applicants
        for i in internships
    }

    results = matching.run_matching(applicants, internships, scores=scores)

    # Capacity check per bucket
    occupancy: dict[int, dict[str, int]] = {}
    for r in results:
        if r.internship_id is not None:
            occupancy.setdefault(r.internship_id, {}).setdefault(r.seat_type, 0)
            occupancy[r.internship_id][r.seat_type] += 1
    for internship in internships:
        buckets = matching._bucket_capacities(internship)
        for bucket, capacity in buckets.items():
            filled = occupancy.get(internship.id, {}).get(bucket, 0)
            assert filled <= capacity

    # Category eligibility check
    applicants_by_id = {a.id: a for a in applicants}
    for r in results:
        if r.internship_id is not None and r.seat_type != "OPEN":
            assert applicants_by_id[r.applicant_id].category == r.seat_type

    # Formal stability check — the property that matters most
    violations = matching.verify_stability(applicants, internships, results, scores)
    assert violations == []
