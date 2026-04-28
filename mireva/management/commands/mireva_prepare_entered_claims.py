"""Prepare claim test data in ENTERED state for MIREVA load tests."""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from claim.models import Claim


class Command(BaseCommand):
    """Move a sample of active claims into ENTERED state for testing."""

    help = "Prepare test claims by setting a sample of active claims to ENTERED."

    def add_arguments(self, parser) -> None:
        """Register CLI options for sample size and source statuses."""
        parser.add_argument(
            "--limit",
            type=int,
            default=20,
            help="Maximum number of claims to move to ENTERED state.",
        )

    def handle(self, *args, **options) -> None:
        """Set selected active claims to ENTERED state via bulk update."""
        limit = max(int(options["limit"]), 0)
        source_ids = list(
            Claim.objects.filter(
                validity_to__isnull=True,
                status__in=[
                    Claim.STATUS_CHECKED,
                    Claim.STATUS_PROCESSED,
                    Claim.STATUS_VALUATED,
                ],
            )
            .order_by("-date_from")
            .values_list("id", flat=True)[:limit]
        )

        if not source_ids:
            self.stdout.write(self.style.WARNING("No source claims found."))
            return

        with transaction.atomic():
            updated = Claim.objects.filter(id__in=source_ids).update(
                status=Claim.STATUS_ENTERED,
                review_status=Claim.REVIEW_IDLE,
                rejection_reason=None,
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Prepared {updated} claims in ENTERED state (limit={limit})."
            )
        )
