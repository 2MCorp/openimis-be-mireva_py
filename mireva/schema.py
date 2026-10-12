"""GraphQL schema for querying MIREVA results and submitting manual reviews."""

from __future__ import annotations

from typing import Any

import graphene
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from graphene_django import DjangoObjectType

from claim.apps import ClaimConfig
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


def _require_perms(info: Any, perms: list[str]) -> None:
    """Raise PermissionDenied unless the request user holds all given rights."""
    user = getattr(info.context, "user", None)
    if user is None or not user.is_authenticated or not user.has_perms(perms):
        raise PermissionDenied(_("unauthorized"))


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
        _require_perms(info, ClaimConfig.gql_query_claims_perms)
        return (
            MirevaClaimResult.objects.select_related("claim", "reviewed_by")
            .filter(claim__uuid=claim_uuid)
            .first()
        )

    def resolve_mireva_claim_results(self, info: Any) -> list[MirevaClaimResult]:
        """Fetch all persisted MIREVA claim results ordered by evaluation time."""
        _require_perms(info, ClaimConfig.gql_query_claims_perms)
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
        _require_perms(info, ClaimConfig.gql_mutation_update_claims_perms)
        user = info.context.user

        valid_decisions = {choice for choice, _label in MirevaClaimResult.DECISION_CHOICES}
        if review_decision not in valid_decisions:
            raise ValueError(
                f"Invalid review_decision '{review_decision}'. "
                f"Expected one of: {', '.join(sorted(valid_decisions))}."
            )

        with transaction.atomic():
            try:
                result = MirevaClaimResult.objects.select_related("claim").get(
                    claim__uuid=claim_uuid
                )
            except MirevaClaimResult.DoesNotExist:
                raise ValueError(f"No MIREVA result exists for claim {claim_uuid}.")

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
            else:
                # REVIEW keeps the claim selected for manual review.
                claim.review_status = Claim.REVIEW_SELECTED
                claim.save(update_fields=["review_status"])

        return result


class Mutation(graphene.ObjectType):
    """GraphQL mutation endpoints for MIREVA module."""

    submit_mireva_review = SubmitMirevaReview.Field()
