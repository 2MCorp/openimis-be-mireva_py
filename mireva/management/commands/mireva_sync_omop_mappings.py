"""Sync this site's codes with the organization's OMOP mappings in MIREVA."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from mireva.concept_sync import sync_concepts
from mireva.services import MirevaService


class Command(BaseCommand):
    """Run the OMOP mapping sync once (same logic as the periodic task)."""

    help = "Register this site's codes with MIREVA and pull the organization's OMOP mappings."

    def handle(self, *args, **options) -> None:
        """Run one push + pull cycle and report it."""
        result = sync_concepts(MirevaService().client)
        self.stdout.write(
            self.style.SUCCESS(
                f"Pushed {result.pushed} codes ({result.created_remote} new, "
                f"{result.unmapped_remote} still unmapped in MIREVA); pulled {result.pulled} mappings "
                f"({result.created_local} created, {result.updated_local} updated locally)."
            )
        )
