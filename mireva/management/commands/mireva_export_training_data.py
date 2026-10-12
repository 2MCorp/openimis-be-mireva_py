"""Export human review decisions as labelled training data and report agreement."""

from __future__ import annotations

import json
from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from mireva.models import MirevaClaimResult, MirevaEvaluation

# Class indices must match mireva_core.constants (APPROVE=0, REVIEW=1, REJECT=2).
LABELS = {
    MirevaClaimResult.DECISION_APPROVE: 0,
    MirevaClaimResult.DECISION_REVIEW: 1,
    MirevaClaimResult.DECISION_REJECT: 2,
}


class Command(BaseCommand):
    """Write reviewed claims as JSONL: the request MIREVA saw plus the human label."""

    help = (
        "Export reviewer decisions as JSONL (payload + label). Labels are APPROVE=0 and "
        "REJECT=2; a reviewer 'REVIEW' means undecided and is skipped unless --include-review. "
        "Also prints how often reviewers agreed with MIREVA, per model version."
    )

    def add_arguments(self, parser) -> None:
        """Register CLI options."""
        parser.add_argument("--output", help="JSONL file to write. Omit to only print the report.")
        parser.add_argument("--include-review", action="store_true")

    def handle(self, *args, **options) -> None:
        """Build the report and write the dataset."""
        results = (
            MirevaClaimResult.objects.filter(review_decision__isnull=False)
            .select_related("claim", "reviewed_by")
            .order_by("reviewed_at")
        )
        agreement: dict[str, Counter] = {}
        rows, no_payload, skipped_review = [], 0, 0
        for result in results:
            counts = agreement.setdefault(result.model_version, Counter())
            counts["reviewed"] += 1
            counts["agreed" if result.review_decision == result.decision else "overridden"] += 1
            if result.review_decision == MirevaClaimResult.DECISION_REVIEW and not options["include_review"]:
                skipped_review += 1
                continue
            evaluation = (
                MirevaEvaluation.objects.filter(claim=result.claim, request_payload__isnull=False)
                .order_by("-evaluated_at", "-id")
                .first()
            )
            if evaluation is None:
                no_payload += 1
                continue
            rows.append(
                {
                    "claim_id": str(result.claim.uuid),
                    "label": LABELS[result.review_decision],
                    "mireva_decision": result.decision,
                    "model_version": evaluation.model_version,
                    "fraud_score": evaluation.fraud_score,
                    "reviewer": getattr(result.reviewed_by, "username", None),
                    "reviewed_at": result.reviewed_at.isoformat() if result.reviewed_at else None,
                    "review_note": result.review_note,
                    "payload": evaluation.request_payload,
                }
            )

        self.stdout.write("Reviewer agreement by model version:")
        for version, counts in sorted(agreement.items()):
            rate = 100 * counts["overridden"] / counts["reviewed"]
            self.stdout.write(
                f"  {version}: {counts['reviewed']} reviewed, {counts['agreed']} agreed, "
                f"{counts['overridden']} overridden ({rate:.1f}%)"
            )
        if not agreement:
            self.stdout.write("  (no reviewed claims yet)")
        if skipped_review:
            self.stdout.write(f"Skipped {skipped_review} reviewer 'REVIEW' (undecided) rows.")
        if no_payload:
            self.stdout.write(
                self.style.WARNING(f"Skipped {no_payload} reviewed claims with no stored request payload.")
            )

        if options["output"]:
            if not rows:
                raise CommandError("Nothing to export.")
            with open(options["output"], "w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, default=str) + "\n")
            self.stdout.write(self.style.SUCCESS(f"Wrote {len(rows)} labelled claims to {options['output']}"))
