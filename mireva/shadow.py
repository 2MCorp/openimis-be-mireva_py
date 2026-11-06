"""Shadow mode: score claims without acting, then compare with what the payer decided."""

from __future__ import annotations

from collections import Counter

from claim.models import Claim

# Class indices match mireva_core.constants (APPROVE=0, REVIEW=1, REJECT=2).
OUTCOME_LABELS = {
    Claim.STATUS_PROCESSED: 0,
    Claim.STATUS_VALUATED: 0,
    Claim.STATUS_REJECTED: 2,
}


def outcome_label(claim_status: int) -> int | None:
    """The payer's final decision as a training label, or None while the claim is still open."""
    return OUTCOME_LABELS.get(claim_status)


def confusion(rows: list[tuple[str, int]]) -> dict[str, Counter]:
    """Count MIREVA decisions against payer outcomes: ``{decision: Counter({label: n})}``."""
    matrix: dict[str, Counter] = {}
    for decision, label in rows:
        matrix.setdefault(decision, Counter())[label] += 1
    return matrix
