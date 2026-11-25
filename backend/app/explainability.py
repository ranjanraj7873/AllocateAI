"""
Explainability layer.

Deliberately NOT a post-hoc explanation model (no SHAP/LIME approximating a
black box). Because scoring.compatibility_score already returns a labeled,
weighted component breakdown, the "explanation" is that breakdown formatted
for a human — exact, not approximate. This is a direct consequence of
choosing an interpretable-by-construction scoring model in scoring.py: you
get faithful explanations for free instead of having to approximate them
after the fact.
"""
import os

from .scoring import ScoredComponent

DEFAULT_SUMMARY_MODEL = "claude-haiku-4-5-20251001"


def generate_ai_summary(narrative: str, applicant_name: str) -> str | None:
    """Optional Claude-powered rewrite of the deterministic narrative into a
    short, warm, plain-English summary for the applicant-facing view.

    The underlying decision is always made by the deterministic, auditable
    scoring + matching engine above — this function never changes the
    outcome, it only rephrases the *exact* facts already computed into
    friendlier language. Returns None (not an error) if no API key is
    configured or the call fails for any reason; callers should treat a
    None return as "no AI summary available for this run" and fall back to
    showing `narrative` alone, which is already a complete explanation.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        model = os.environ.get("CLAUDE_PARSER_MODEL", DEFAULT_SUMMARY_MODEL)
        prompt = (
            "Rewrite the explanation below as at most 2 short, warm, plain-English "
            "sentences addressed to the applicant. Keep every number and fact exactly "
            "as given — do not invent, drop, or round any detail. Output only the "
            "rewritten sentences, no markdown, no preamble, no quotation marks.\n\n"
            f"Applicant: {applicant_name}\n"
            f"Explanation: {narrative}"
        )
        response = client.messages.create(
            model=model,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        return text or None
    except Exception:
        return None


def _seat_type_sentence(seat_type: str) -> str:
    if seat_type == "OPEN":
        return "allocated on open merit (no reservation category was used)"
    return f"allocated under the {seat_type} reservation quota"


def build_allocation_explanation(
    overall_score: float,
    seat_type: str,
    components: dict[str, ScoredComponent],
    internship_title: str,
    company: str,
) -> dict:
    ranked = sorted(components.items(), key=lambda kv: -kv[1].contribution)
    top_factors = "; ".join(f"{name.replace('_', ' ')} ({c.detail})" for name, c in ranked[:2])

    narrative = (
        f"Matched to {internship_title} at {company} with an overall compatibility "
        f"score of {overall_score:.2f} (out of 1.00), {_seat_type_sentence(seat_type)}. "
        f"Strongest contributing factors: {top_factors}."
    )

    return {
        "overall_score": overall_score,
        "seat_type": seat_type,
        "components": {
            name: {
                "raw_score": c.raw_score,
                "weight": c.weight,
                "contribution": c.contribution,
                "detail": c.detail,
            }
            for name, c in components.items()
        },
        "narrative": narrative,
    }


def build_unallocated_explanation(min_eligible_score: float) -> dict:
    narrative = (
        "No allocation was made this cycle. Either every internship this applicant was "
        f"eligible for (score above the {min_eligible_score:.2f} minimum threshold) had all "
        "its open and quota seats filled by candidates who ranked at least as strongly, or no "
        "internship met the minimum eligibility score. This applicant will roll over "
        "automatically into the next allocation round if one is configured."
    )
    return {
        "overall_score": 0.0,
        "seat_type": None,
        "components": {},
        "narrative": narrative,
    }
