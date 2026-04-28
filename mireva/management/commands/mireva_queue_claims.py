"""Queue claim UUIDs for asynchronous MIREVA evaluation."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from claim.models import Claim

from mireva.tasks import evaluate_claim_async


class Command(BaseCommand):
    """Queue selected claims for MIREVA evaluation without claim save() calls."""

    help = "Queue claims for MIREVA evaluation from ENTERED/CHECKED/PROCESSED/VALUATED."

    STATUS_MAP = {
        "entered": Claim.STATUS_ENTERED,
        "checked": Claim.STATUS_CHECKED,
        "processed": Claim.STATUS_PROCESSED,
        "valuated": Claim.STATUS_VALUATED,
    }

    def add_arguments(self, parser) -> None:
        """Register CLI arguments for selecting claims."""
        parser.add_argument(
            "--status",
            choices=list(self.STATUS_MAP.keys()),
            default="checked",
            help="Claim status to queue from.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=20,
            help="Maximum number of claims to queue.",
        )
        parser.add_argument(
            "--only-without-result",
            action="store_true",
            help="Queue only claims that do not yet have MirevaClaimResult.",
        )

    def handle(self, *args, **options) -> None:
        """Fetch claim UUIDs and enqueue Celery tasks."""
        status_key = options["status"]
        limit = max(int(options["limit"]), 0)
        only_without_result = bool(options["only_without_result"])

        queryset = Claim.objects.filter(
            validity_to__isnull=True,
            status=self.STATUS_MAP[status_key],
        ).order_by("-date_from")

        if only_without_result:
            queryset = queryset.filter(mireva_result__isnull=True)

        uuids = list(queryset.values_list("uuid", flat=True)[:limit])
        if not uuids:
            self.stdout.write(self.style.WARNING("No claims matched queue criteria."))
            return

        for claim_uuid in uuids:
            evaluate_claim_async.delay(str(claim_uuid))

        self.stdout.write(
            self.style.SUCCESS(
                f"Queued {len(uuids)} claims from status '{status_key}' for MIREVA."
            )
        )
