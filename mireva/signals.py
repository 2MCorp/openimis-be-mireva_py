"""Signal handlers for asynchronous MIREVA claim evaluation."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from claim.models import Claim

from mireva.models import MirevaClaimResult
from mireva.tasks import evaluate_claim_async


@receiver(post_save, sender=Claim)
def on_claim_saved(sender, instance: Claim, created: bool, **kwargs) -> None:
    """Queue claim fraud evaluation when an active claim reaches CHECKED status."""
    if instance.status != Claim.STATUS_CHECKED:
        return

    if MirevaClaimResult.objects.filter(claim=instance).exists():
        return

    evaluate_claim_async.delay(str(instance.uuid))
