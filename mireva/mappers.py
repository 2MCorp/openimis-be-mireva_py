"""Mapping utilities from openIMIS claims to MIREVA payload format."""

from __future__ import annotations

import logging
import zlib
from datetime import timedelta
from typing import Any

from django.db.models import Avg, Count, Q
from django.utils import timezone

from claim.models import Claim
from insuree.models import InsureeStatus

from mireva.models import MirevaClaimResult, OmopConceptMapping


logger = logging.getLogger(__name__)


class OpenIMISToMirevaMapper:
    """Converts openIMIS claim data and aggregates into MIREVA inputs."""

    LOCAL_CONCEPT_BASE = 2_000_000_000
    LOCAL_CONCEPT_SPAN = 100_000_000
    VISIT_TYPE_MAP = {"O": 9202, "I": 9201, "E": 9203}
    VISIT_TYPE_LABELS = {"O": "outpatient", "I": "inpatient", "E": "emergency"}
    GENDER_MAP = {"M": 8507, "F": 8532}

    def __init__(self, derive_license: bool = False) -> None:
        self._mapping_cache: dict[str, dict[str, int]] = {}
        self.derive_license = derive_license

    def map_claim(
        self,
        claim: Claim,
        patient_history: list[dict[str, Any]],
        provider_history: dict[str, Any] | None,
        with_tariffs: bool = True,
    ) -> dict[str, Any]:
        """Map a claim to MIREVA's OMOP-normalized request payload.

        ``with_tariffs=False`` skips the price-list lookups; history claims use it because
        MIREVA only compares the scored claim's prices with the tariff.
        """
        insuree = claim.insuree
        item_tariffs, service_tariffs = self._tariffs(claim) if with_tariffs else ({}, {})
        flags = self._eligibility_checker(claim) if with_tariffs else (lambda detail: {})
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
                **self._prices(item.price_asked, item_tariffs.get(item.item_id)),
                **flags(item.item),
                **({"line_id": f"item:{item.id}"} if with_tariffs else {}),
            }
            for item in claim.items.filter(validity_to__isnull=True).select_related("item")
        ]
        procedures = [
            {
                "concept_id": self._service_to_omop(service.service.code),
                "quantity": self._to_float(service.qty_provided) or 1.0,
                **self._prices(service.price_asked, service_tariffs.get(service.service_id)),
                **flags(service.service),
                **({"line_id": f"service:{service.id}"} if with_tariffs else {}),
            }
            for service in claim.services.filter(validity_to__isnull=True).select_related(
                "service"
            )
        ]
        policy_start_date, policy_end_date = self._policy_period(claim)
        payload = {
            "claim_id": str(claim.uuid),
            "visit": {
                "visit_concept_id": self.VISIT_TYPE_MAP.get(claim.visit_type, 9202),
                "visit_start_date": claim.date_from.isoformat(),
                "visit_type": self.VISIT_TYPE_LABELS.get(claim.visit_type, "outpatient"),
            },
            "patient": {
                "patient_id": str(insuree.uuid),
                "age": int(self._resolve_callable(insuree.age) or 0),
                "gender_concept_id": self.GENDER_MAP.get(insuree.gender, 0),
                "poverty": getattr(getattr(insuree, "family", None), "poverty", None),
            },
            "diagnoses": diagnoses,
            "drugs": drugs,
            "procedures": procedures,
            "claimed_amount": self._to_float(claim.claimed),
            "patient_history": patient_history,
            "provider_history": provider_history or {},
        }
        deceased_date = self._deceased_date(insuree)
        if deceased_date:
            payload["patient_deceased_date"] = deceased_date.isoformat()
        if policy_start_date:
            payload["policy_start_date"] = policy_start_date.isoformat()
        if policy_end_date:
            payload["policy_end_date"] = policy_end_date.isoformat()
        if claim.date_claimed:
            payload["submission_date"] = claim.date_claimed.isoformat()
        health_facility_uuid = getattr(claim.health_facility, "uuid", None)
        if health_facility_uuid:
            payload["provider_id"] = str(health_facility_uuid)
        payload["facility"] = {
            "level": getattr(claim.health_facility, "level", None) or None,
            "care_type": getattr(claim.health_facility, "care_type", None) or None,
        }
        if self.derive_license:
            payload["provider_license_valid"] = self._facility_licensed(claim.health_facility)
        return payload

    def _eligibility_checker(self, claim: Claim):
        """Return ``f(item_or_service) -> flags`` using openIMIS's own category and care-type rules.

        ``category_eligible`` is False when the item/service is limited to categories the insuree
        does not belong to; ``care_type_compatible`` is False when its care type does not fit the
        facility (mirrors ``claim.validations.validate_claimdetail_care_type``, without its side
        effect of setting a rejection reason). A flag is omitted when it cannot be determined.
        """
        try:
            from claim.utils import get_claim_target_date
            from core.utils import patient_category_mask

            target_date = get_claim_target_date(claim)
            mask = patient_category_mask(claim.insuree, target_date)
        except Exception:  # noqa: BLE001 - never block an evaluation on an optional signal
            logger.warning("Could not derive eligibility context for claim %s", claim.uuid, exc_info=True)
            return lambda detail: {}
        facility_type = getattr(claim.health_facility, "care_type", None) or "B"
        inpatient = target_date != claim.date_from

        def flags(detail: Any) -> dict[str, bool]:
            out: dict[str, bool] = {}
            category = getattr(detail, "patient_category", None)
            if category is not None:
                out["category_eligible"] = category == 0 or (category & mask) == mask
            care_type = getattr(detail, "care_type", None)
            if care_type:
                out["care_type_compatible"] = not (
                    (facility_type == "O" and inpatient)
                    or (facility_type == "O" and care_type == "I")
                    or (facility_type == "I" and care_type == "O")
                )
            return out

        return flags

    # openIMIS has no licence field; a facility that is inactive or delisted is the closest signal.
    UNLICENSED_FACILITY_STATUSES = ("IN", "DE")
    LICENSED_FACILITY_STATUS = "AC"

    @classmethod
    def _facility_licensed(cls, facility: Any) -> bool | None:
        """True for an active facility, False for inactive or delisted, None when unknown."""
        status = getattr(facility, "status", None)
        if status == cls.LICENSED_FACILITY_STATUS:
            return True
        if status in cls.UNLICENSED_FACILITY_STATUSES:
            return False
        return None

    @classmethod
    def _prices(cls, price_asked: Any, tariff: Any) -> dict[str, float]:
        """Per-unit price asked and reference tariff; keys are omitted when unknown."""
        out: dict[str, float] = {}
        if price_asked is not None:
            out["unit_price"] = cls._to_float(price_asked)
        if tariff is not None:
            out["tariff_unit_price"] = cls._to_float(tariff)
        return out

    def _tariffs(self, claim: Claim) -> tuple[dict[int, Any], dict[int, Any]]:
        """Reference unit price per item and service id.

        The facility's price-list override wins over the catalogue price, as in openIMIS
        valuation. Without the ``medical_pricelist`` module only catalogue prices are used.
        """
        items = list(claim.items.filter(validity_to__isnull=True).select_related("item"))
        services = list(claim.services.filter(validity_to__isnull=True).select_related("service"))
        item_tariffs = {i.item_id: i.item.price for i in items if i.item.price is not None}
        service_tariffs = {s.service_id: s.service.price for s in services if s.service.price is not None}
        facility = claim.health_facility
        try:
            from medical_pricelist.models import ItemsPricelistDetail, ServicesPricelistDetail
        except ImportError:
            return item_tariffs, service_tariffs
        if getattr(facility, "items_pricelist_id", None) and item_tariffs:
            rows = ItemsPricelistDetail.objects.filter(
                items_pricelist_id=facility.items_pricelist_id,
                item_id__in=list(item_tariffs),
                validity_to__isnull=True,
                price_overrule__isnull=False,
            ).values_list("item_id", "price_overrule")
            item_tariffs.update(dict(rows))
        if getattr(facility, "services_pricelist_id", None) and service_tariffs:
            rows = ServicesPricelistDetail.objects.filter(
                services_pricelist_id=facility.services_pricelist_id,
                service_id__in=list(service_tariffs),
                validity_to__isnull=True,
                price_overrule__isnull=False,
            ).values_list("service_id", "price_overrule")
            service_tariffs.update(dict(rows))
        return item_tariffs, service_tariffs

    @staticmethod
    def _deceased_date(insuree: Any) -> Any:
        """Return the insuree's status date when their status is DEAD, else None."""
        if getattr(insuree, "status", None) != InsureeStatus.DEAD:
            return None
        return getattr(insuree, "status_date", None)

    @staticmethod
    def _policy_period(claim: Claim) -> tuple[Any, Any]:
        """Return the earliest start and latest expiry of policies covering the claim lines."""
        policies = [
            line.policy
            for line in [*claim.items.all(), *claim.services.all()]
            if getattr(line, "policy", None) is not None
        ]
        starts = [p.effective_date or p.start_date for p in policies if p.start_date]
        ends = [p.expiry_date for p in policies if p.expiry_date]
        return (min(starts) if starts else None, max(ends) if ends else None)

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
            .prefetch_related(
                "items__item", "items__policy", "services__service", "services__policy"
            )
            .order_by("-date_from")[:limit]
        )
        return [self.map_claim(history_claim, [], None, with_tariffs=False) for history_claim in claims]

    def fetch_provider_history(
        self, hf_id: int, exclude_claim_id: int | None = None
    ) -> dict[str, Any]:
        """Aggregate provider-level claim metrics used for model context.

        The claim being scored is excluded, and rejections that MIREVA itself
        issued are not counted, so the model never learns from its own output.
        """
        now = timezone.now().date()
        last_30_days_start = now - timedelta(days=30)

        base_queryset = Claim.objects.filter(
            health_facility_id=hf_id,
            validity_to__isnull=True,
        )
        if exclude_claim_id is not None:
            base_queryset = base_queryset.exclude(id=exclude_claim_id)
        aggregate_result = base_queryset.aggregate(
            total_claims=Count("id"),
            avg_claim_amount=Avg("claimed"),
            claims_last_30_days=Count(
                "id", filter=Q(date_from__gte=last_30_days_start)
            ),
            rejected_claims=Count(
                "id",
                filter=Q(status=Claim.STATUS_REJECTED)
                & ~Q(mireva_result__decision=MirevaClaimResult.DECISION_REJECT),
            ),
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
        """Resolve an ICD-10 source code to an OMOP concept id."""
        return self.concept_id(OmopConceptMapping.VOCABULARY_ICD10, icd_code)

    def _item_to_omop(self, item_code: str) -> int:
        """Resolve an openIMIS item code to an OMOP concept id."""
        return self.concept_id(OmopConceptMapping.VOCABULARY_ITEM, item_code)

    def _service_to_omop(self, service_code: str) -> int:
        """Resolve an openIMIS service code to an OMOP concept id."""
        return self.concept_id(OmopConceptMapping.VOCABULARY_SERVICE, service_code)

    def concept_id(self, vocabulary: str, source_code: str) -> int:
        """Return the mapped OMOP concept id, else a stable site-specific id.

        A shared fallback of 0 would make every unmapped code look identical, which
        corrupts frequency rules and the model vocabulary. OMOP reserves ids from
        2,000,000,000 for local concepts, so unmapped codes get a deterministic id
        in that range until a real mapping is loaded.
        """
        code = self.normalize_code(vocabulary, source_code)
        if not code:
            return 0
        return self._mappings(vocabulary).get(code) or self.local_concept_id(vocabulary, code)

    @staticmethod
    def normalize_code(vocabulary: str, source_code: Any) -> str:
        """Canonical form of a source code (ICD-10 dots removed, upper-cased)."""
        code = str(source_code or "").strip().upper()
        if vocabulary == OmopConceptMapping.VOCABULARY_ICD10:
            code = code.replace(".", "")
        return code

    @classmethod
    def local_concept_id(cls, vocabulary: str, normalized_code: str) -> int:
        """Deterministic site-specific concept id for a code without a mapping."""
        digest = zlib.crc32(f"{vocabulary}:{normalized_code}".encode("utf-8"))
        return cls.LOCAL_CONCEPT_BASE + digest % cls.LOCAL_CONCEPT_SPAN

    def _mappings(self, vocabulary: str) -> dict[str, int]:
        """Load one vocabulary's mappings once per mapper instance."""
        if vocabulary not in self._mapping_cache:
            self._mapping_cache[vocabulary] = {
                self.normalize_code(vocabulary, code): concept_id
                for code, concept_id in OmopConceptMapping.objects.filter(
                    source_vocabulary=vocabulary
                ).values_list("source_code", "concept_id")
            }
        return self._mapping_cache[vocabulary]

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
