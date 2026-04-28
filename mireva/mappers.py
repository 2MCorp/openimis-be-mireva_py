"""Mapping utilities from openIMIS claims to MIREVA payload format."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db.models import Avg, Count, Q
from django.utils import timezone

from claim.models import Claim

from mireva.models import OmopConceptMapping


class OpenIMISToMirevaMapper:
    """Converts openIMIS claim data and aggregates into MIREVA inputs."""

    VISIT_TYPE_MAP = {"O": 9202, "I": 9201, "E": 9203}
    GENDER_MAP = {"M": 8507, "F": 8532}

    def map_claim(
        self,
        claim: Claim,
        patient_history: list[dict[str, Any]],
        provider_history: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Map a claim to MIREVA's OMOP-normalized request payload."""
        insuree = claim.insuree
        diagnoses = [
            {
                "concept_id": self._icd_to_omop(self._diagnosis_to_code(code)),
                "source_code": self._diagnosis_to_code(code),
            }
            for code in [claim.icd, claim.icd_1, claim.icd_2, claim.icd_3, claim.icd_4]
            if code is not None
        ]
        drugs = [
            {
                "concept_id": self._item_to_omop(item.item.code),
                "quantity": self._to_float(item.qty_provided),
                "days_supply": 0,
            }
            for item in claim.items.filter(validity_to__isnull=True).select_related("item")
        ]
        procedures = [
            {"concept_id": self._service_to_omop(service.service.code)}
            for service in claim.services.filter(validity_to__isnull=True).select_related(
                "service"
            )
        ]
        return {
            "claim_id": str(claim.uuid),
            "visit": {
                "visit_concept_id": self.VISIT_TYPE_MAP.get(claim.visit_type, 9202),
                "visit_start_date": claim.date_from.isoformat(),
                "visit_type": claim.visit_type,
            },
            "patient": {
                "patient_id": str(insuree.uuid),
                "age": int(self._resolve_callable(insuree.age) or 0),
                "gender_concept_id": self.GENDER_MAP.get(insuree.gender, 0),
            },
            "diagnoses": diagnoses,
            "drugs": drugs,
            "procedures": procedures,
            "claimed_amount": self._to_float(claim.claimed),
            "patient_history": patient_history,
            "provider_history": provider_history or {},
        }

    def fetch_patient_history(
        self, insuree_id: int, exclude_claim_id: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return mapped history claims for the same patient excluding current claim."""
        claims = (
            Claim.objects.filter(
                insuree_id=insuree_id,
                validity_to__isnull=True,
                status__in=[
                    Claim.STATUS_CHECKED,
                    Claim.STATUS_PROCESSED,
                    Claim.STATUS_VALUATED,
                ],
            )
            .exclude(id=exclude_claim_id)
            .select_related("insuree", "health_facility")
            .prefetch_related("items__item", "services__service")
            .order_by("-date_from")[:limit]
        )
        return [self.map_claim(history_claim, [], None) for history_claim in claims]

    def fetch_provider_history(self, hf_id: int) -> dict[str, Any]:
        """Aggregate provider-level claim metrics used for model context."""
        now = timezone.now().date()
        last_30_days_start = now - timedelta(days=30)

        base_queryset = Claim.objects.filter(
            health_facility_id=hf_id,
            validity_to__isnull=True,
        )
        aggregate_result = base_queryset.aggregate(
            total_claims=Count("id"),
            avg_claim_amount=Avg("claimed"),
            claims_last_30_days=Count(
                "id", filter=Q(date_from__gte=last_30_days_start)
            ),
            rejected_claims=Count("id", filter=Q(status=Claim.STATUS_REJECTED)),
        )
        total_claims = int(aggregate_result["total_claims"] or 0)
        rejected_claims = int(aggregate_result["rejected_claims"] or 0)
        rejection_rate = (
            round(rejected_claims / total_claims, 4) if total_claims > 0 else 0.0
        )

        return {
            "facility_concept_id": hf_id,
            "claims_last_30_days": int(aggregate_result["claims_last_30_days"] or 0),
            "avg_claim_amount": float(aggregate_result["avg_claim_amount"] or 0.0),
            "rejection_rate": rejection_rate,
        }

    def _icd_to_omop(self, icd_code: str) -> int:
        """Resolve ICD-10 source code to OMOP concept id, defaulting to 0."""
        return (
            OmopConceptMapping.objects.filter(
                source_code=icd_code,
                source_vocabulary=OmopConceptMapping.VOCABULARY_ICD10,
            )
            .values_list("concept_id", flat=True)
            .first()
            or 0
        )

    def _item_to_omop(self, item_code: str) -> int:
        """Resolve openIMIS item code to OMOP concept id, defaulting to 0."""
        return (
            OmopConceptMapping.objects.filter(
                source_code=item_code,
                source_vocabulary=OmopConceptMapping.VOCABULARY_ITEM,
            )
            .values_list("concept_id", flat=True)
            .first()
            or 0
        )

    def _service_to_omop(self, service_code: str) -> int:
        """Resolve openIMIS service code to OMOP concept id, defaulting to 0."""
        return (
            OmopConceptMapping.objects.filter(
                source_code=service_code,
                source_vocabulary=OmopConceptMapping.VOCABULARY_SERVICE,
            )
            .values_list("concept_id", flat=True)
            .first()
            or 0
        )

    @staticmethod
    def _to_float(value: Any) -> float:
        """Convert nullable numeric values to float, defaulting missing values to 0.0."""
        if value is None:
            return 0.0
        return float(value)

    @staticmethod
    def _resolve_callable(value: Any) -> Any:
        """Evaluate zero-argument callables and return plain values."""
        return value() if callable(value) else value

    @staticmethod
    def _diagnosis_to_code(diagnosis: Any) -> str:
        """Extract diagnosis source code string from FK objects or plain strings."""
        if diagnosis is None:
            return ""
        if isinstance(diagnosis, str):
            return diagnosis
        return str(
            getattr(diagnosis, "code", None)
            or getattr(diagnosis, "id", None)
            or diagnosis
        )
