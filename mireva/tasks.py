"""Celery tasks for MIREVA claim evaluation."""

import logging
from typing import Any

from celery import shared_task

from mireva.services import MirevaService

logger = logging.getLogger(__name__)

try:
    from mireva_sdk.exceptions import MirevaConnectionError, MirevaValidationError
except ImportError:  # pragma: no cover - fallback for alternate SDK exports
    from mireva_sdk import MirevaConnectionError, MirevaValidationError  # type: ignore


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def evaluate_claim_async(self: Any, claim_uuid: str) -> None:
    """Evaluate a claim with MIREVA and persist the normalized decision."""
    try:
        MirevaService().evaluate_claim(claim_uuid)
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
