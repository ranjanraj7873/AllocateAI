"""
Matching engine (Stage 2).

Algorithm: candidate-proposing Gale-Shapley deferred acceptance, extended
with capacity ("hospital/residents" generalization) and per-internship seat
buckets for reservation quotas. This is the same family of algorithm used by
the US National Resident Matching Program to place medical residents, and by
several countries' school-choice systems — it is the standard, defensible
choice for large-scale many-to-one allocation with quotas, not a bespoke
heuristic. Its guarantees:

  * Terminates in a bounded number of proposals (each applicant proposes to
    each internship at most once).
  * Produces a matching with NO BLOCKING PAIR: no rejected applicant and
    internship both *prefer* each other over their current outcome. That is
    the formal definition of a "stable" allocation, and it's exactly the
    property you want to be able to defend if an applicant challenges an
    outcome ("could I have been placed somewhere better without bumping
    someone more deserving?" — the answer is provably no).
  * Is strategy-proof for the proposing side (applicants): an applicant can
    never benefit from misreporting their preferences. That matters for a
    government system — you don't want candidates gaming the system.

Seat buckets: each internship's total_capacity is split into named buckets
— "OPEN" (open merit, anyone eligible) plus one bucket per reservation
category with a nonzero quota. A reserved-category applicant is eligible for
BOTH the OPEN bucket and their own category bucket, and — matching real
seat-matrix policy — always tries OPEN first: reservation is a guaranteed
floor, never a ceiling.
"""
from collections import deque
from dataclasses import dataclass, field

from . import scoring

# Hard eligibility floor: below this compatibility score, an applicant is
# never proposed to / accepted by an internship, regardless of free
# capacity. Exposed as a module constant so the API layer and the
# explainability layer can both reference the same number instead of two
# copies drifting apart.
DEFAULT_MIN_ELIGIBLE_SCORE = 0.15


@dataclass
class MatchResult:
    applicant_id: int
    internship_id: int | None
    seat_type: str | None
    score: float | None


def compute_all_scores(applicants, internships) -> dict[tuple[int, int], tuple[float, dict]]:
    """Precompute the applicant x internship compatibility matrix once."""
    scores: dict[tuple[int, int], tuple[float, dict]] = {}
    for a in applicants:
        for i in internships:
            overall, components = scoring.compatibility_score(a, i)
            scores[(a.id, i.id)] = (overall, components)
    return scores


def _bucket_capacities(internship) -> dict[str, int]:
    quotas = {k: v for k, v in (internship.quotas or {}).items() if v > 0}
    reserved_total = sum(quotas.values())
    open_capacity = max(0, internship.total_capacity - reserved_total)
    buckets = {"OPEN": open_capacity}
    buckets.update(quotas)
    return buckets


def _eligible_buckets(applicant, bucket_capacities: dict[str, int]) -> list[str]:
    """OPEN first (merit-first), then the applicant's own category bucket."""
    order = []
    if "OPEN" in bucket_capacities:
        order.append("OPEN")
    if applicant.category in bucket_capacities and applicant.category != "OPEN":
        order.append(applicant.category)
    return order


# Public names for reuse outside this module (interview_selection.py needs
# the same bucket/eligibility logic to find contested seats — kept as thin
# wrappers so the private originals stay the single source of truth).
def bucket_capacities(internship) -> dict[str, int]:
    return _bucket_capacities(internship)


def eligible_buckets(applicant, capacities: dict[str, int]) -> list[str]:
    return _eligible_buckets(applicant, capacities)


def run_matching(
    applicants: list,
    internships: list,
    scores: dict[tuple[int, int], tuple[float, dict]] | None = None,
    min_eligible_score: float = DEFAULT_MIN_ELIGIBLE_SCORE,
) -> list[MatchResult]:
    """
    Run deferred acceptance to completion and return one MatchResult per
    applicant (internship_id is None if they end up unallocated).

    min_eligible_score: a hard floor below which an applicant is never
    considered for an internship at all, regardless of remaining capacity —
    this prevents "filling empty seats with anyone" and keeps every
    allocation defensible on the score breakdown. Tune per program.
    """
    if scores is None:
        scores = compute_all_scores(applicants, internships)

    internships_by_id = {i.id: i for i in internships}
    applicants_by_id = {a.id: a for a in applicants}

    bucket_capacity = {i.id: _bucket_capacities(i) for i in internships}
    held: dict[int, dict[str, list[int]]] = {
        i.id: {b: [] for b in bucket_capacity[i.id]} for i in internships
    }

    # Each applicant's ranked list of (internship_id, bucket) options — NOT
    # just internship ids. Expanding to bucket granularity is what makes the
    # "OPEN before quota" rule work correctly under deferred acceptance: when
    # a candidate is later bumped out of an OPEN seat, they must fall through
    # to trying their own category's quota seat at that SAME internship
    # before moving on to their next-preferred internship. If preferences
    # were tracked at the internship level only, a bumped candidate would
    # incorrectly skip the quota bucket entirely (that was a real bug caught
    # by this project's own test suite — see test_matching.py).
    #
    # Internships are ordered per the applicant's explicit preferences (or,
    # if none given, by descending compatibility score); within a single
    # internship, OPEN is always tried before the applicant's category
    # quota, per _eligible_buckets.
    all_internship_ids = [i.id for i in internships]
    pref_lists: dict[int, list[tuple[int, str]]] = {}
    for a in applicants:
        explicit = [iid for iid in (a.preferences or []) if iid in internships_by_id]
        explicit_set = set(explicit)
        remainder = sorted(
            (iid for iid in all_internship_ids if iid not in explicit_set),
            key=lambda iid: -scores[(a.id, iid)][0],
        )
        ordered_internships = explicit + remainder
        expanded: list[tuple[int, str]] = []
        for iid in ordered_internships:
            for bucket in _eligible_buckets(a, bucket_capacity[iid]):
                expanded.append((iid, bucket))
        pref_lists[a.id] = expanded

    proposal_index = {a.id: 0 for a in applicants}
    current_match: dict[int, tuple[int, str] | None] = {a.id: None for a in applicants}
    free = deque(a.id for a in applicants)

    # Each applicant can propose at most once per (internship, bucket) pair.
    max_proposals = sum(len(p) for p in pref_lists.values()) + len(applicants) + 10
    proposals_made = 0

    while free:
        a_id = free.popleft()
        prefs = pref_lists[a_id]
        idx = proposal_index[a_id]

        while idx < len(prefs) and scores[(a_id, prefs[idx][0])][0] < min_eligible_score:
            idx += 1

        if idx >= len(prefs):
            proposal_index[a_id] = idx
            continue  # exhausted every eligible (internship, bucket) option; stays unallocated

        internship_id, bucket = prefs[idx]
        proposal_index[a_id] = idx + 1
        proposals_made += 1
        if proposals_made > max_proposals:
            raise RuntimeError("Matching engine exceeded its proposal safety bound — check input data")

        applicant_score = scores[(a_id, internship_id)][0]
        capacity = bucket_capacity[internship_id][bucket]
        occupants = held[internship_id][bucket]
        placed = False

        if len(occupants) < capacity:
            occupants.append(a_id)
            occupants.sort(key=lambda x: -scores[(x, internship_id)][0])
            current_match[a_id] = (internship_id, bucket)
            placed = True
        else:
            weakest_id = occupants[-1]
            weakest_score = scores[(weakest_id, internship_id)][0]
            if applicant_score > weakest_score:
                occupants.pop()
                occupants.append(a_id)
                occupants.sort(key=lambda x: -scores[(x, internship_id)][0])
                current_match[a_id] = (internship_id, bucket)
                current_match[weakest_id] = None
                free.append(weakest_id)
                placed = True

        if not placed:
            free.append(a_id)  # will try the next (internship, bucket) option in their list

    results = []
    for a in applicants:
        match = current_match[a.id]
        if match is None:
            results.append(MatchResult(a.id, None, None, None))
        else:
            iid, bucket = match
            results.append(MatchResult(a.id, iid, bucket, scores[(a.id, iid)][0]))
    return results


def verify_stability(
    applicants: list,
    internships: list,
    results: list[MatchResult],
    scores: dict[tuple[int, int], tuple[float, dict]],
    min_eligible_score: float = DEFAULT_MIN_ELIGIBLE_SCORE,
) -> list[str]:
    """
    Brute-force check for blocking pairs: (applicant, internship) pairs where
    the applicant would strictly prefer that internship over their current
    outcome AND the internship would strictly prefer the applicant over its
    current weakest eligible occupant (or has a free eligible seat). Returns
    a list of human-readable violations — empty list means the matching is
    stable. Used by the test suite; cheap enough to also run as a post-
    allocation sanity check in production.
    """
    violations: list[str] = []
    result_by_applicant = {r.applicant_id: r for r in results}
    internships_by_id = {i.id: i for i in internships}

    occupancy: dict[int, dict[str, list[int]]] = {i.id: {} for i in internships}
    for r in results:
        if r.internship_id is not None:
            occupancy[r.internship_id].setdefault(r.seat_type, []).append(r.applicant_id)

    for a in applicants:
        current = result_by_applicant[a.id]
        current_score = current.score if current.internship_id else None

        for i in internships:
            candidate_score = scores[(a.id, i.id)][0]
            if candidate_score < min_eligible_score:
                continue
            if current.internship_id == i.id:
                continue
            if current_score is not None and candidate_score <= current_score:
                continue  # applicant doesn't even prefer this internship

            buckets = _bucket_capacities(i)
            for bucket in _eligible_buckets(a, buckets):
                capacity = buckets[bucket]
                occupants = occupancy[i.id].get(bucket, [])
                if len(occupants) < capacity:
                    violations.append(
                        f"Applicant {a.id} prefers internship {i.id} (bucket {bucket}) "
                        f"which has a free seat — blocking pair"
                    )
                    continue
                weakest_score = min(scores[(o, i.id)][0] for o in occupants)
                if candidate_score > weakest_score:
                    violations.append(
                        f"Applicant {a.id} (score {candidate_score}) outranks the weakest "
                        f"occupant of internship {i.id} bucket {bucket} (score {weakest_score}) "
                        f"— blocking pair"
                    )
    return violations
