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


def find_reviewers() -> list[Any]:
    """Return active users whose valid, unblocked roles carry a reviewer right."""
    from core.models import User

    return list(
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


def notify_reviewers(
    claim_uuid: str, claim_code: str, prediction: dict[str, Any], downgraded: bool
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
        reviewers = find_reviewers()
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
    claim_uuid: str, claim_code: str, prediction: dict[str, Any], downgraded: bool
) -> None:
    """Schedule the notification for after the surrounding transaction commits."""
    transaction.on_commit(
        lambda: notify_reviewers(claim_uuid, claim_code, prediction, downgraded)
    )
