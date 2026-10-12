"""Celery tasks for MIREVA claim evaluation."""

import logging
from typing import Any

from celery import shared_task

from claim.models import Claim

from mireva.services import MirevaService

logger = logging.getLogger(__name__)

try:
    from mireva_sdk.exceptions import MirevaConnectionError, MirevaValidationError
except ImportError:  # pragma: no cover - fallback for alternate SDK exports
    from mireva_sdk import MirevaConnectionError, MirevaValidationError  # type: ignore


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def evaluate_claim_async(self: Any, claim_uuid: str, skip_existing: bool = False) -> None:
    """Evaluate a claim with MIREVA and persist the normalized decision."""
    try:
        MirevaService().evaluate_claim(claim_uuid, skip_existing=skip_existing)
    except ValueError:
        logger.exception("MIREVA configuration error for claim %s", claim_uuid)
    except MirevaConnectionError as exc:
        logger.warning("MIREVA connection error for claim %s: %s", claim_uuid, exc)
        raise self.retry(exc=exc)
    except MirevaValidationError as exc:
        logger.error(
            "MIREVA validation error for claim %s. detail=%s",
            claim_uuid,
            getattr(exc, "detail", None),
        )
    except Exception as exc:  # pragma: no cover - defensive retry path
        logger.exception("Unexpected MIREVA task error for claim %s", claim_uuid)
        raise self.retry(exc=exc)


def queue_unevaluated_claims(limit: int = 100) -> int:
    """Queue CHECKED claims that have no MIREVA result yet and return how many were queued.

    Recovers claims whose signal-time queueing failed (broker down) or whose task
    exhausted its retries. Duplicate tasks are harmless because they use skip_existing.
    """
    claim_uuids = list(
        Claim.objects.filter(
            validity_to__isnull=True,
            status=Claim.STATUS_CHECKED,
            mireva_result__isnull=True,
        )
        .order_by("id")
        .values_list("uuid", flat=True)[: max(int(limit), 0)]
    )
    queued = 0
    for claim_uuid in claim_uuids:
        try:
            evaluate_claim_async.delay(str(claim_uuid), skip_existing=True)
            queued += 1
        except Exception as exc:
            logger.warning("MIREVA sweep could not queue claim %s: %s", claim_uuid, exc)
            break  # broker is down; the next sweep retries the rest
    return queued


@shared_task
def sweep_unevaluated_claims(limit: int = 100) -> int:
    """Periodic task wrapper around queue_unevaluated_claims (schedule with Celery beat)."""
    queued = queue_unevaluated_claims(limit)
    if queued:
        logger.info("MIREVA sweep queued %s unevaluated CHECKED claims", queued)
    return queued
