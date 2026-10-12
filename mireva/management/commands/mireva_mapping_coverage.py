"""Report how much of the codes used by active claims have an OMOP mapping."""

from __future__ import annotations

import csv
from collections import Counter

from django.core.management.base import BaseCommand
from django.db.models import Count

from claim.models import Claim, ClaimItem, ClaimService

from mireva.mappers import OpenIMISToMirevaMapper
from mireva.models import OmopConceptMapping


class Command(BaseCommand):
    """Print mapping coverage per vocabulary and optionally export unmapped codes."""

    help = (
        "Report OMOP mapping coverage for codes used on active claims. "
        "Use --export to write the unmapped codes as a CSV template to fill in."
    )

    def add_arguments(self, parser) -> None:
        """Register CLI options."""
        parser.add_argument("--top", type=int, default=10, help="Unmapped codes to print per vocabulary.")
        parser.add_argument("--export", help="Write all unmapped codes to this CSV path.")

    def handle(self, *args, **options) -> None:
        """Compute and print coverage."""
        mapper = OpenIMISToMirevaMapper()
        usage = {
            OmopConceptMapping.VOCABULARY_ICD10: self._diagnosis_usage(),
            OmopConceptMapping.VOCABULARY_ITEM: self._item_usage(),
            OmopConceptMapping.VOCABULARY_SERVICE: self._service_usage(),
        }
        export_rows = []
        for vocabulary, codes in usage.items():
            mapped = {
                code: info
                for code, info in codes.items()
                if mapper.normalize_code(vocabulary, code) in mapper._mappings(vocabulary)
            }
            unmapped = {code: info for code, info in codes.items() if code not in mapped}
            total_uses = sum(n for n, _ in codes.values())
            mapped_uses = sum(n for n, _ in mapped.values())
            pct_codes = 100 * len(mapped) / len(codes) if codes else 0.0
            pct_uses = 100 * mapped_uses / total_uses if total_uses else 0.0
            self.stdout.write(
                f"{vocabulary}: {len(mapped)}/{len(codes)} codes mapped ({pct_codes:.1f}%), "
                f"{pct_uses:.1f}% of usages"
            )
            ranked = sorted(unmapped.items(), key=lambda kv: -kv[1][0])
            for code, (uses, name) in ranked[: options["top"]]:
                self.stdout.write(f"    unmapped {code:<8} used {uses:>5}x  {name}")
            export_rows += [
                {"source_code": code, "vocabulary": vocabulary, "source_name": name,
                 "usage_count": uses, "concept_id": "", "concept_name": "", "domain": ""}
                for code, (uses, name) in ranked
            ]

        if options["export"]:
            with open(options["export"], "w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["source_code", "vocabulary", "source_name", "usage_count",
                                "concept_id", "concept_name", "domain"],
                )
                writer.writeheader()
                writer.writerows(export_rows)
            self.stdout.write(
                self.style.SUCCESS(f"Wrote {len(export_rows)} unmapped codes to {options['export']}")
            )

    @staticmethod
    def _diagnosis_usage() -> dict[str, tuple[int, str]]:
        """Return {code: (usage count, name)} across the five claim diagnosis slots."""
        counts: Counter = Counter()
        names: dict[str, str] = {}
        for field in ("icd", "icd_1", "icd_2", "icd_3", "icd_4"):
            rows = (
                Claim.objects.filter(validity_to__isnull=True, **{f"{field}__isnull": False})
                .values_list(f"{field}__code", f"{field}__name")
                .annotate(n=Count("id"))
            )
            for code, name, n in rows:
                counts[code] += n
                names[code] = name
        return {code: (n, names[code]) for code, n in counts.items()}

    @staticmethod
    def _item_usage() -> dict[str, tuple[int, str]]:
        """Return {code: (usage count, name)} for claim items."""
        rows = (
            ClaimItem.objects.filter(validity_to__isnull=True, claim__validity_to__isnull=True)
            .values_list("item__code", "item__name")
            .annotate(n=Count("id"))
        )
        return {code: (n, name) for code, name, n in rows}

    @staticmethod
    def _service_usage() -> dict[str, tuple[int, str]]:
        """Return {code: (usage count, name)} for claim services."""
        rows = (
            ClaimService.objects.filter(validity_to__isnull=True, claim__validity_to__isnull=True)
            .values_list("service__code", "service__name")
            .annotate(n=Count("id"))
        )
        return {code: (n, name) for code, name, n in rows}
