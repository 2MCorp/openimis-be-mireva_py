"""Notify reviewers when MIREVA sends a claim to manual review."""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)

MAX_REASONS_IN_BODY = 3


def reviewer_rights() -> list[int]:
    """Rights a user needs to be notified: by default those required to submit a review."""
    configured = getattr(settings, "MIREVA_REVIEWER_RIGHTS", None)
    if configured:
        return [int(right) for right in configured]
    from claim.apps import ClaimConfig

    return [int(right) for right in ClaimConfig.gql_mutation_update_claims_perms]


def district_scoping_enabled() -> bool:
    """Whether reviewers are limited to the districts assigned to them (opt-in)."""
    return bool(getattr(settings, "MIREVA_SCOPE_REVIEWERS_BY_DISTRICT", False))


def find_reviewers(district_id: int | None = None) -> list[Any]:
    """Return active users whose valid, unblocked roles carry a reviewer right.

    With ``MIREVA_SCOPE_REVIEWERS_BY_DISTRICT`` and a known ``district_id``, a reviewer who has
    district assignments is kept only if one of them is that district. A reviewer with no
    assignments is unrestricted, which matches how openIMIS row security treats such users.
    """
    from core.models import User

    users = list(
        User.objects.filter(
            validity_to__isnull=True,
            i_user__validity_to__isnull=True,
            i_user__user_roles__validity_to__isnull=True,
            i_user__user_roles__role__validity_to__isnull=True,
            i_user__user_roles__role__is_blocked=False,
            i_user__user_roles__role__rights__validity_to__isnull=True,
            i_user__user_roles__role__rights__right_id__in=reviewer_rights(),
        ).distinct()
    )
    if district_id is None or not district_scoping_enabled():
        return users

    from location.models import UserDistrict

    assigned: dict[int, set[int]] = {}
    for user_id, location_id in UserDistrict.objects.filter(
        validity_to__isnull=True, user_id__in=[u.i_user_id for u in users]
    ).values_list("user_id", "location_id"):
        assigned.setdefault(user_id, set()).add(location_id)
    return [u for u in users if not assigned.get(u.i_user_id) or district_id in assigned[u.i_user_id]]


def notify_reviewers(
    claim_uuid: str,
    claim_code: str,
    prediction: dict[str, Any],
    downgraded: bool,
    district_id: int | None = None,
) -> int:
    """Send an in-app notification to every reviewer and return how many were created.

    Never raises: the claim decision is already made, and a missing notification
    module or a failed push must not undo it.
    """
    try:
        from notification.services import notify
    except ImportError:
        logger.info("Notification module not installed; MIREVA review of %s not announced", claim_uuid)
        return 0

    try:
        reviewers = find_reviewers(district_id)
        if not reviewers:
            logger.warning("No reviewers hold %s; MIREVA review of claim %s is unannounced",
                           reviewer_rights(), claim_uuid)
            return 0
        reasons = [
            r.get("title") or r.get("reason_id")
            for r in (prediction.get("reasons") or [])
            if isinstance(r, dict) and r.get("source") != "ai"
        ][:MAX_REASONS_IN_BODY]
        risk = prediction.get("risk_level") or "LOW"
        lines = [
            "MIREVA risk: {{ risk }} (fraud score {{ score }}).",
            "The model alone recommended rejection; a person must decide." if downgraded
            else "The claim needs a manual decision.",
        ]
        if reasons:
            lines.append("Signals: {{ reasons }}.")
        with transaction.atomic():  # savepoint: a failure here leaves the caller's transaction intact
            created = notify(
                reviewers,
                title="Claim {{ code }} needs review (MIREVA)",
                body=" ".join(lines),
                params={
                    "code": claim_code,
                    "risk": risk,
                    "score": f"{float(prediction.get('fraud_score') or 0.0):.2f}",
                    "reasons": ", ".join(str(r) for r in reasons),
                },
                category="claim",
                severity="error" if risk == "HIGH" else "warning",
                link={"entity_type": "Claim", "entity_id": str(claim_uuid)},
                source="mireva",
                dedupe_key=f"mireva-review:{claim_uuid}",
            )
        return len(created)
    except Exception:
        logger.exception("Could not notify reviewers about claim %s", claim_uuid)
        return 0


def notify_reviewers_after_commit(
    claim_uuid: str,
    claim_code: str,
    prediction: dict[str, Any],
    downgraded: bool,
    district_id: int | None = None,
) -> None:
    """Schedule the notification for after the surrounding transaction commits."""
    transaction.on_commit(
        lambda: notify_reviewers(claim_uuid, claim_code, prediction, downgraded, district_id)
    )
