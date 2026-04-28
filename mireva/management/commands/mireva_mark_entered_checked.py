"""Move ENTERED claims to CHECKED for MIREVA evaluation testing."""

from __future__ import annotations

import random

from django.core.management.base import BaseCommand
from django.db import transaction

from claim.models import Claim


class Command(BaseCommand):
    """Transition active ENTERED claims into CHECKED status."""

    help = (
        "Set active ENTERED claims to CHECKED. "
        "Use --trigger-signal to save each claim and fire post_save hooks."
    )

    def add_arguments(self, parser) -> None:
        """Register CLI options controlling how claim updates are executed."""
        parser.add_argument(
            "--trigger-signal",
            action="store_true",
            help="Save claims one-by-one so Django post_save signals are fired.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Optional cap for number of claims to process (0 means all).",
        )
        parser.add_argument(
            "--assign-random-admin",
            action="store_true",
            help="Assign a random non-null ClaimAdminId from existing claims before saving.",
        )

    def handle(self, *args, **options) -> None:
        """Update claims and report how many transitions succeeded."""
        trigger_signal = bool(options["trigger_signal"])
        assign_random_admin = bool(options["assign_random_admin"])
        limit = max(int(options["limit"]), 0)

        queryset = Claim.objects.filter(
            validity_to__isnull=True, status=Claim.STATUS_ENTERED
        ).order_by("id")
        if limit > 0:
            queryset = queryset[:limit]

        if not trigger_signal:
            claim_ids = list(queryset.values_list("id", flat=True))
            if not claim_ids:
                self.stdout.write(self.style.WARNING("No ENTERED claims found."))
                return

            with transaction.atomic():
                updated = Claim.objects.filter(id__in=claim_ids).update(
                    status=Claim.STATUS_CHECKED
                )
            self.stdout.write(
                self.style.SUCCESS(
                    f"Updated {updated} ENTERED claims to CHECKED (bulk mode)."
                )
            )
            return

        updated = 0
        failed = 0
        random_admin_id = (
            self._get_or_create_random_claim_admin_id(queryset) if assign_random_admin else None
        )
        if assign_random_admin and random_admin_id is None:
            self.stdout.write(
                self.style.WARNING(
                    "Could not resolve/create a ClaimAdmin. Proceeding without admin reassignment."
                )
            )
        for claim in queryset.iterator():
            try:
                if random_admin_id is not None:
                    claim.admin_id = random_admin_id
                claim.status = Claim.STATUS_CHECKED
                update_fields = ["status"]
                if random_admin_id is not None:
                    update_fields.append("admin")
                claim.save(update_fields=update_fields)
                updated += 1
            except Exception as exc:  # pragma: no cover - runtime data dependent
                failed += 1
                self.stderr.write(f"Claim {claim.id} failed: {exc}")

        self.stdout.write(
            self.style.SUCCESS(
                f"Signal mode finished. updated={updated}, failed={failed}."
            )
        )

    def _get_or_create_random_claim_admin_id(self, claims_queryset) -> int | None:
        """Get a random non-null ClaimAdminId from existing claims.

        This avoids direct access to the ClaimAdmin table, which may be absent
        in some local environments even when Claim rows contain ClaimAdminId.
        """
        del claims_queryset
        admin_ids = list(
            Claim.objects.filter(validity_to__isnull=True, admin_id__isnull=False)
            .values_list("admin_id", flat=True)
            .distinct()
        )
        if not admin_ids:
            self.stdout.write(
                self.style.WARNING(
                    "No ClaimAdminId values found on claims; cannot assign random admin."
                )
            )
            return None
        return random.choice(admin_ids)
