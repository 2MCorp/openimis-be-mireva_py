"""Core service for evaluating claims with MIREVA and persisting results."""

from __future__ import annotations

import logging
import os
from typing import Any

from django.conf import settings

from claim.models import Claim

from mireva.mappers import OpenIMISToMirevaMapper
from mireva.models import MirevaClaimResult

logger = logging.getLogger(__name__)

try:
    from mireva_sdk import MirevaClient
except ImportError:  # pragma: no cover - fallback for alternate SDK layout
    from mireva_sdk.client import MirevaClient  # type: ignore


class MirevaService:
    """Coordinates claim mapping, prediction calls, and state updates."""

    def __init__(self) -> None:
        """Create SDK client and mapper using Django settings."""
        base_url = getattr(settings, "MIREVA_URL", None) or os.getenv("MIREVA_URL")
        api_key = getattr(settings, "MIREVA_API_KEY", None) or os.getenv("MIREVA_API_KEY", "")
        if not base_url:
            raise ValueError(
                "MIREVA_URL is not configured. Set it in Django settings or environment."
            )
        self.client = MirevaClient(
            base_url=base_url,
            api_key=api_key,
        )
        self.mapper = OpenIMISToMirevaMapper()

    def evaluate_claim(self, claim_uuid: str) -> MirevaClaimResult:
        """Evaluate a claim with MIREVA and apply resulting workflow actions."""
        claim = (
            Claim.objects.select_related("insuree", "health_facility")
            .prefetch_related("items__item", "services__service")
            .get(uuid=claim_uuid)
        )
        patient_history = self.mapper.fetch_patient_history(
            insuree_id=claim.insuree_id,
            exclude_claim_id=claim.id,
            limit=10,
        )
        provider_history = self.mapper.fetch_provider_history(hf_id=claim.health_facility_id)
        mireva_input = self.mapper.map_claim(claim, patient_history, provider_history)
        prediction = self.client.predict(mireva_input)
        normalized = self._normalize_prediction(prediction)

        result, _ = MirevaClaimResult.objects.update_or_create(
            claim=claim,
            defaults={
                "decision": normalized["decision"],
                "fraud_score": normalized["fraud_score"],
                "risk_level": normalized["risk_level"],
                "confidence": normalized["confidence"],
                "reasons": normalized["reasons"],
                "flags": normalized["flags"],
                "model_version": normalized["model_version"],
            },
        )
        self._apply_decision(claim, normalized["decision"], normalized["summary"], normalized)
        return result

    def _normalize_prediction(self, prediction: Any) -> dict[str, Any]:
        """Normalize SDK prediction payload supporting modern and legacy key shapes."""
        payload = self._to_dict(prediction)
        explanation = self._to_dict(payload.get("explanation")) if payload.get("explanation") else {}
        hard_rules = self._to_dict(payload.get("hard_rules")) if payload.get("hard_rules") else {}
        soft_rules = self._to_dict(payload.get("soft_rules")) if payload.get("soft_rules") else {}

        reasons = payload.get("reasons")
        if reasons is None:
            reasons = explanation.get("reasons", [])

        flags = payload.get("flags")
        if flags is None:
            flags = (hard_rules.get("rules_fired") or []) + (soft_rules.get("rules_fired") or [])

        return {
            "decision": payload.get("final_decision") or payload.get("decision"),
            "fraud_score": float(payload.get("fraud_score", 0.0)),
            "risk_level": payload.get("risk_level", MirevaClaimResult.RISK_LOW),
            "confidence": float(payload.get("confidence", 0.0)),
            "reasons": reasons or [],
            "flags": flags or [],
            "model_version": payload.get("model_version", "unknown"),
            "summary": explanation.get("summary") or payload.get("summary") or "",
        }

    def _apply_decision(
        self, claim: Claim, decision: str, summary: str, prediction: dict[str, Any]
    ) -> None:
        """Update claim workflow fields based on normalized MIREVA decision."""
        if decision == MirevaClaimResult.DECISION_APPROVE:
            claim.status = Claim.STATUS_PROCESSED
            claim.save(update_fields=["status"])
            return

        if decision == MirevaClaimResult.DECISION_REVIEW:
            claim.review_status = Claim.REVIEW_SELECTED
            claim.save(update_fields=["review_status"])
            self._notify_medical_officer(claim, prediction)
            return

        if decision == MirevaClaimResult.DECISION_REJECT:
            claim.status = Claim.STATUS_REJECTED
            # openIMIS Claim.rejection_reason is an integer code, not free text.
            claim.rejection_reason = self._extract_rejection_code(prediction)
            claim.explanation = summary or "Rejected by MIREVA risk decision."
            claim.save(update_fields=["status", "rejection_reason", "explanation"])

    def _notify_medical_officer(self, claim: Claim, prediction: dict[str, Any]) -> None:
        """Log review notification placeholder for future openIMIS notification integration."""
        # TODO: Integrate with openIMIS notification module for medical officer workflow.
        logger.info(
            "MIREVA review notification pending integration for claim %s, score=%s",
            claim.uuid,
            prediction.get("fraud_score"),
        )

    @staticmethod
    def _to_dict(value: Any) -> dict[str, Any]:
        """Convert SDK/pydantic objects into dictionaries for field-safe access."""
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        if hasattr(value, "model_dump"):
            return value.model_dump()
        if hasattr(value, "dict"):
            return value.dict()
        return {}

    @staticmethod
    def _extract_rejection_code(prediction: dict[str, Any]) -> int:
        """Return a numeric rejection reason code compatible with openIMIS Claim model."""
        raw_code = prediction.get("rejection_code")
        if raw_code is None:
            return 1
        try:
            return int(raw_code)
        except (TypeError, ValueError):
            return 1
