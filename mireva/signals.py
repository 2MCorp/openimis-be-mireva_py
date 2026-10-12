"""Signal handlers for asynchronous MIREVA claim evaluation."""

import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from claim.models import Claim

from mireva.models import MirevaClaimResult
from mireva.tasks import evaluate_claim_async

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Claim)
def on_claim_saved(sender, instance: Claim, created: bool, **kwargs) -> None:
    """Queue claim fraud evaluation when an active claim reaches CHECKED status."""
    if instance.status != Claim.STATUS_CHECKED:
        return

    if MirevaClaimResult.objects.filter(claim=instance).exists():
        return

    claim_uuid = str(instance.uuid)

    def _queue() -> None:
        try:
            evaluate_claim_async.delay(claim_uuid, skip_existing=True)
        except Exception as exc:
            # Claim submit must not fail when Celery/RabbitMQ is unavailable.
            logger.warning("MIREVA evaluation not queued for claim %s: %s", claim_uuid, exc)

    # Queue only after commit so the worker never sees an uncommitted claim.
    transaction.on_commit(_queue)
