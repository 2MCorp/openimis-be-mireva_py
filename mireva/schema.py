"""GraphQL schema for querying MIREVA results and submitting manual reviews."""

from __future__ import annotations

from typing import Any

import graphene
from django.utils import timezone
from graphene_django import DjangoObjectType

from claim.models import Claim

from mireva.models import MirevaClaimResult


class MirevaClaimResultType(DjangoObjectType):
    """GraphQL type exposing persisted MIREVA evaluation fields for a claim."""

    claim_id = graphene.String()

    class Meta:
        model = MirevaClaimResult
        fields = "__all__"

    def resolve_claim_id(self, info: Any) -> str:
        """Return the claim UUID as a string field."""
        return str(self.claim.uuid)


class Query(graphene.ObjectType):
    """GraphQL query endpoints for MIREVA module."""

    mireva_claim_result = graphene.Field(
        MirevaClaimResultType,
        claim_uuid=graphene.String(required=True),
    )
    mireva_claim_results = graphene.List(MirevaClaimResultType)

    def resolve_mireva_claim_result(
        self, info: Any, claim_uuid: str
    ) -> MirevaClaimResult | None:
        """Fetch MIREVA result by claim UUID, or return None when not found."""
        return (
            MirevaClaimResult.objects.select_related("claim", "reviewed_by")
            .filter(claim__uuid=claim_uuid)
            .first()
        )

    def resolve_mireva_claim_results(self, info: Any) -> list[MirevaClaimResult]:
        """Fetch all persisted MIREVA claim results ordered by evaluation time."""
        return list(
            MirevaClaimResult.objects.select_related("claim", "reviewed_by").order_by(
                "-evaluated_at"
            )
        )


class SubmitMirevaReview(graphene.Mutation):
    """Mutation to record reviewer decision and update claim status accordingly."""

    class Arguments:
        claim_uuid = graphene.String(required=True)
        review_decision = graphene.String(required=True)
        review_note = graphene.String(required=False)

    Output = MirevaClaimResultType

    @classmethod
    def mutate(
        cls,
        root: Any,
        info: Any,
        claim_uuid: str,
        review_decision: str,
        review_note: str | None = None,
    ) -> MirevaClaimResult:
        """Persist review metadata and update claim status based on final decision."""
        user = getattr(info.context, "user", None)
        if user is None or not user.is_authenticated:
            raise Exception("Authentication is required.")

        result = MirevaClaimResult.objects.select_related("claim").get(claim__uuid=claim_uuid)
        result.reviewed_by = user
        result.review_decision = review_decision
        result.review_note = review_note
        result.reviewed_at = timezone.now()
        result.save(
            update_fields=["reviewed_by", "review_decision", "review_note", "reviewed_at"]
        )

        claim = result.claim
        if review_decision == MirevaClaimResult.DECISION_APPROVE:
            claim.status = Claim.STATUS_PROCESSED
            claim.save(update_fields=["status"])
        elif review_decision == MirevaClaimResult.DECISION_REJECT:
            claim.status = Claim.STATUS_REJECTED
            claim.save(update_fields=["status"])

        return result


class Mutation(graphene.ObjectType):
    """GraphQL mutation endpoints for MIREVA module."""

    submit_mireva_review = SubmitMirevaReview.Field()
