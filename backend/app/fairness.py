"""
Fairness auditor.

Runs AFTER allocation and inspects outcomes — it never influences the
matching engine itself. Hard fairness guarantees (the reservation minimums)
are enforced structurally by matching.py's seat buckets; this module is the
independent check that verifies the *result* looks fair in aggregate, the
same separation of concerns you'd want in any compliance-sensitive system
(the thing enforcing a rule shouldn't be the only thing checking it worked).

`four_fifths_rule_ratio` borrows the "four-fifths rule" (selection rate for
the lowest-outcome group should be at least 80% of the highest-outcome
group) from US EEOC adverse-impact guidance. It is used here purely as a
convenient, well-known heuristic threshold for flagging a lopsided outcome
for human review — it is NOT a claim that this is a legally mandated
threshold for the PM Internship Scheme or under Indian law. Treat it as a
dashboard tripwire, not a compliance certificate.
"""


def build_fairness_report(run_id: str, applicants: list, internships: list, results: list) -> dict:
    total = len(applicants)
    allocated_ids = {r.applicant_id for r in results if r.internship_id is not None}
    total_allocated = len(allocated_ids)
    total_unallocated = total - total_allocated
    overall_rate = round(total_allocated / total, 4) if total else 0.0

    by_cat: dict[str, dict] = {}
    for a in applicants:
        stat = by_cat.setdefault(a.category, {"total": 0, "allocated": 0})
        stat["total"] += 1
        if a.id in allocated_ids:
            stat["allocated"] += 1

    category_stats = []
    rates = []
    for cat, stat in sorted(by_cat.items()):
        rate = round(stat["allocated"] / stat["total"], 4) if stat["total"] else 0.0
        category_stats.append(
            {"category": cat, "total_applicants": stat["total"], "allocated": stat["allocated"], "allocation_rate": rate}
        )
        if stat["total"] > 0:
            rates.append(rate)

    if rates and max(rates) > 0:
        ratio = round(min(rates) / max(rates), 4)
    else:
        ratio = 1.0
    passes = ratio >= 0.8

    occupancy: dict[int, dict[str, int]] = {}
    for r in results:
        if r.internship_id is not None:
            bucket_counts = occupancy.setdefault(r.internship_id, {})
            bucket_counts[r.seat_type] = bucket_counts.get(r.seat_type, 0) + 1

    quota_fill = []
    for i in internships:
        quotas = {k: v for k, v in (i.quotas or {}).items() if v > 0}
        reserved_total = sum(quotas.values())
        open_capacity = max(0, i.total_capacity - reserved_total)
        buckets = {"OPEN": open_capacity, **quotas}
        filled = occupancy.get(i.id, {})
        quota_fill.append(
            {
                "internship_id": i.id,
                "title": i.title,
                "buckets": [
                    {"seat_type": b, "capacity": cap, "filled": filled.get(b, 0)}
                    for b, cap in buckets.items()
                ],
            }
        )

    return {
        "run_id": run_id,
        "total_applicants": total,
        "total_allocated": total_allocated,
        "total_unallocated": total_unallocated,
        "overall_allocation_rate": overall_rate,
        "by_category": category_stats,
        "four_fifths_rule_ratio": ratio,
        "four_fifths_rule_pass": passes,
        "quota_fill": quota_fill,
    }
