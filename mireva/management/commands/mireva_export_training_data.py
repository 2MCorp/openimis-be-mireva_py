"""Export human review decisions as labelled training data and report agreement."""

from __future__ import annotations

import json
from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from mireva.models import MirevaClaimResult, MirevaEvaluation
from mireva.shadow import confusion, outcome_label

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
        parser.add_argument(
            "--shadow",
            action="store_true",
            help="Use shadow-mode evaluations and label each claim with the payer's own final "
            "decision (processed/valuated = 0, rejected = 2) instead of MIREVA reviewer decisions. "
            "Claims that are still open are skipped.",
        )

    def handle(self, *args, **options) -> None:
        """Build the report and write the dataset."""
        if options["shadow"]:
            return self.handle_shadow(options)
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

    def handle_shadow(self, options) -> None:
        """Export shadow-mode evaluations labelled by the payer's final decision."""
        latest: dict[int, MirevaEvaluation] = {}
        for evaluation in (
            MirevaEvaluation.objects.filter(action=MirevaEvaluation.ACTION_SHADOW)
            .select_related("claim")
            .order_by("evaluated_at", "id")
        ):
            latest[evaluation.claim_id] = evaluation  # keep the newest per claim

        rows, matrices, open_claims, no_payload = [], {}, 0, 0
        for evaluation in latest.values():
            label = outcome_label(evaluation.claim.status)
            if label is None:
                open_claims += 1
                continue
            matrices.setdefault(evaluation.model_version, []).append((evaluation.decision, label))
            if evaluation.request_payload is None:
                no_payload += 1
                continue
            rows.append(
                {
                    "claim_id": str(evaluation.claim.uuid),
                    "label": label,
                    "mireva_decision": evaluation.decision,
                    "model_version": evaluation.model_version,
                    "fraud_score": evaluation.fraud_score,
                    "payload": evaluation.request_payload,
                }
            )

        self.stdout.write("Shadow mode: MIREVA decision vs the payer's final decision (APPROVE=0, REJECT=2):")
        for version, pairs in sorted(matrices.items()):
            self.stdout.write(f"  {version}: {len(pairs)} adjudicated claims")
            for decision, counts in sorted(confusion(pairs).items()):
                self.stdout.write(
                    f"    MIREVA {decision:7} -> payer approved {counts.get(0, 0)}, payer rejected {counts.get(2, 0)}"
                )
        if not matrices:
            self.stdout.write("  (no adjudicated shadow claims yet)")
        if open_claims:
            self.stdout.write(f"Skipped {open_claims} claims the payer has not decided yet.")
        if no_payload:
            self.stdout.write(
                self.style.WARNING(f"Skipped {no_payload} claims with no stored request payload.")
            )
        if options["output"]:
            if not rows:
                raise CommandError("Nothing to export.")
            with open(options["output"], "w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, default=str) + "\n")
            self.stdout.write(self.style.SUCCESS(f"Wrote {len(rows)} labelled claims to {options['output']}"))
