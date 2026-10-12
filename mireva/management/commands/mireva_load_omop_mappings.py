"""Load source-code to OMOP concept mappings from a CSV file."""

from __future__ import annotations

import csv

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from mireva.mappers import OpenIMISToMirevaMapper
from mireva.models import OmopConceptMapping

DEFAULT_DOMAIN = {
    OmopConceptMapping.VOCABULARY_ICD10: OmopConceptMapping.DOMAIN_CONDITION,
    OmopConceptMapping.VOCABULARY_ITEM: OmopConceptMapping.DOMAIN_DRUG,
    OmopConceptMapping.VOCABULARY_SERVICE: OmopConceptMapping.DOMAIN_PROCEDURE,
}


class Command(BaseCommand):
    """Create or update OmopConceptMapping rows from CSV."""

    help = (
        "Load OMOP mappings from CSV with columns source_code, concept_id and optionally "
        "vocabulary, concept_name, domain. Rows with an empty concept_id are skipped, so the "
        "template written by mireva_mapping_coverage --export can be filled in gradually."
    )

    def add_arguments(self, parser) -> None:
        """Register CLI options."""
        parser.add_argument("csv_path")
        parser.add_argument(
            "--vocabulary",
            choices=list(DEFAULT_DOMAIN),
            help="Vocabulary for rows that have no 'vocabulary' column value.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Validate and report without writing.")

    def handle(self, *args, **options) -> None:
        """Validate every row first, then write them all in one transaction."""
        valid_domains = {d for d, _ in OmopConceptMapping.DOMAIN_CHOICES}
        rows, skipped, errors = [], 0, []
        with open(options["csv_path"], newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or not {"source_code", "concept_id"} <= set(reader.fieldnames):
                raise CommandError("CSV needs at least the columns: source_code, concept_id")
            for line, row in enumerate(reader, start=2):
                raw_id = (row.get("concept_id") or "").strip()
                if not raw_id:
                    skipped += 1
                    continue
                vocabulary = (row.get("vocabulary") or options["vocabulary"] or "").strip()
                if vocabulary not in DEFAULT_DOMAIN:
                    errors.append(f"line {line}: unknown or missing vocabulary '{vocabulary}'")
                    continue
                if not raw_id.isdigit() or int(raw_id) <= 0:
                    errors.append(f"line {line}: concept_id must be a positive integer, got '{raw_id}'")
                    continue
                concept_id = int(raw_id)
                if concept_id >= OpenIMISToMirevaMapper.LOCAL_CONCEPT_BASE:
                    errors.append(f"line {line}: concept_id {concept_id} is in the reserved local range")
                    continue
                domain = (row.get("domain") or DEFAULT_DOMAIN[vocabulary]).strip()
                if domain not in valid_domains:
                    errors.append(f"line {line}: unknown domain '{domain}'")
                    continue
                code = OpenIMISToMirevaMapper.normalize_code(vocabulary, row["source_code"])
                if not code:
                    errors.append(f"line {line}: empty source_code")
                    continue
                rows.append((code, vocabulary, concept_id, (row.get("concept_name") or "")[:255], domain))

        if errors:
            raise CommandError("No rows loaded.\n" + "\n".join(errors[:20]))
        if options["dry_run"]:
            self.stdout.write(f"Dry run: {len(rows)} valid rows, {skipped} skipped (no concept_id).")
            return

        created = updated = 0
        with transaction.atomic():
            for code, vocabulary, concept_id, name, domain in rows:
                _, was_created = OmopConceptMapping.objects.update_or_create(
                    source_code=code,
                    source_vocabulary=vocabulary,
                    defaults={"concept_id": concept_id, "concept_name": name, "domain": domain},
                )
                created += was_created
                updated += not was_created
        self.stdout.write(
            self.style.SUCCESS(f"Loaded mappings: {created} created, {updated} updated, {skipped} skipped.")
        )
