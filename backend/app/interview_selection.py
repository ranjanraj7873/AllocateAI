"""
Interview selection — Stage 3 of the AllocateAI pipeline.

This is the piece that turns "we scored everyone" into "we know which
handful of interviews are actually worth holding." Interview slots are the
scarce resource the whole project exists to protect: an obviously-strong
pair doesn't need one (they're getting the seat either way), an
obviously-weak pair doesn't need one either (no interview outcome will make
them beat a stronger candidate) — the only pairs where an interview can
change the final allocation are the ones that are BOTH:

  1. Near a capacity cutoff — the provisional match (run once, using AI
     scores alone) shows this pair sitting right at the boundary of who
     gets a seat and who doesn't, for a specific internship's bucket.
  2. Not something the AI is confident about — some of the score leaned on
     a neutral fallback (missing CGPA, no stated location/preferences)
     rather than real signal (see scoring.confidence()).

A pair that's borderline AND low-confidence is exactly the case where
spending a scarce interview slot can flip — and therefore improve — the
final outcome. A pair that's borderline but high-confidence is just a close
race the AI already has enough evidence to call; a pair that's low-
confidence but nowhere near a cutoff doesn't matter either way. This module
intersects both conditions on purpose, instead of ranking on either alone.

Pipeline shape (see main.py's /api/allocate/prepare and /api/allocate/finalize):

  Stage 1 (scoring.py)      -> compatibility_score for every pair
  Stage 2a (matching.py)    -> a PROVISIONAL match, AI scores only
  Stage 3 (this module)     -> select_key_interviews() over the provisional
                                match's cutoffs + Stage 1's confidence
  [ admin conducts the selected interviews, records an outcome ]
  Stage 2b (matching.py)    -> the FINAL match, using apply_interview_outcomes()
                                to swap in observed outcomes where available
  Stage 4 (fairness.py)     -> the same independent audit as before
"""
from dataclasses import dataclass

from . import matching, scoring

# Default width of the "contested zone" around a bucket's cutoff score.
# A pair within this distance of the cutoff is close enough that an
# interview outcome could plausibly move it across the line. Tunable —
# widening it interviews more borderline pairs at the cost of using more
# scarce slots on pairs that were never going to be close anyway.
DEFAULT_MARGIN_WINDOW = 0.12


@dataclass
class InterviewCandidate:
    applicant_id: int
    internship_id: int
    bucket: str
    score: float
    confidence: float
    margin: float  # distance from the bucket's cutoff score; smaller = more contested
    impact: float  # ranking key — higher means more worth an interview slot
    reason: str


def _bucket_cutoffs(
    scores: dict, provisional_results: list, bucket_capacity: dict[int, dict[str, int]]
) -> dict[tuple[int, str], float]:
    """For every (internship, bucket) that is completely FULL in the
    provisional match, the cutoff is the weakest occupant's score — anyone
    scoring at or above it who didn't get in is a near-miss; anyone who got
    in near that line could plausibly be displaced by a stronger showing
    elsewhere. A bucket with any free seat isn't contested at all (that's
    the "empty seats" problem the proposal names separately, not a scarce-
    interview one) and is deliberately excluded here, regardless of how many
    people currently occupy it."""
    occupancy: dict[tuple[int, str], list[int]] = {}
    for r in provisional_results:
        if r.internship_id is not None:
            occupancy.setdefault((r.internship_id, r.seat_type), []).append(r.applicant_id)

    cutoffs = {}
    for (internship_id, bucket), occupant_ids in occupancy.items():
        capacity = bucket_capacity.get(internship_id, {}).get(bucket, 0)
        if len(occupant_ids) < capacity:
            continue  # free seat remaining — not contested
        cutoffs[(internship_id, bucket)] = min(scores[(aid, internship_id)][0] for aid in occupant_ids)
    return cutoffs


def select_key_interviews(
    applicants: list,
    internships: list,
    scores: dict[tuple[int, int], tuple[float, dict]],
    provisional_results: list,
    max_interviews: int = 20,
    margin_window: float = DEFAULT_MARGIN_WINDOW,
) -> list[InterviewCandidate]:
    """
    Returns up to `max_interviews` (applicant, internship, bucket) triples
    worth an actual interview, ranked by impact = (1 - confidence) *
    (margin_window - margin). Both factors are required to be positive for
    a pair to be considered at all — see the module docstring for why.
    """
    internships_by_id = {i.id: i for i in internships}
    bucket_capacity = {i.id: matching.bucket_capacities(i) for i in internships}
    cutoffs = _bucket_cutoffs(scores, provisional_results, bucket_capacity)

    candidates: list[InterviewCandidate] = []
    for a in applicants:
        for i in internships:
            overall, components = scores[(a.id, i.id)]
            if overall < matching.DEFAULT_MIN_ELIGIBLE_SCORE:
                continue
            for bucket in matching.eligible_buckets(a, bucket_capacity[i.id]):
                cutoff = cutoffs.get((i.id, bucket))
                if cutoff is None:
                    continue  # this bucket had a free seat provisionally — not contested
                margin = abs(overall - cutoff)
                if margin > margin_window:
                    continue

                conf = scoring.confidence(components)
                impact = round((1 - conf) * (margin_window - margin), 6)
                if impact <= 0:
                    continue  # either fully confident, or right at the edge of the window

                reason = (
                    f"Score {overall:.2f} sits within {margin:.2f} of the {bucket} cutoff "
                    f"({cutoff:.2f}) for {internships_by_id[i.id].title} — only "
                    f"{conf:.0%} confident because part of the score relied on missing "
                    f"data rather than a stated value."
                )
                candidates.append(InterviewCandidate(
                    applicant_id=a.id,
                    internship_id=i.id,
                    bucket=bucket,
                    score=overall,
                    confidence=conf,
                    margin=round(margin, 4),
                    impact=impact,
                    reason=reason,
                ))

    candidates.sort(key=lambda c: -c.impact)
    return candidates[:max_interviews]


def apply_interview_outcomes(scores: dict, interviews: list) -> dict:
    """
    Returns a NEW scores dict where every completed interview's
    outcome_score overrides the pre-interview AI-only estimate for that
    exact (applicant, internship) pair — so the final match reflects what
    was actually observed for the pairs that got interviewed, and falls
    back to the original AI score for every pair that didn't (which is
    most of them, by design: interviews are scarce).

    `interviews` is any iterable of objects exposing applicant_id,
    internship_id, and outcome_score (None if not yet conducted) — this
    works directly against the Interview ORM model without importing it,
    keeping this module free of a models.py dependency.
    """
    adjusted = dict(scores)
    for interview in interviews:
        if getattr(interview, "outcome_score", None) is not None:
            key = (interview.applicant_id, interview.internship_id)
            if key in adjusted:
                _, components = adjusted[key]
                adjusted[key] = (interview.outcome_score, components)
    return adjusted
