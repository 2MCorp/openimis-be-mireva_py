"""Core service for evaluating claims with MIREVA and persisting results."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

from django.conf import settings
from django.db import transaction

from claim.models import Claim
from claim.validations import REJECTION_REASON_INVALID_CLAIM, REJECTION_REASON_NO_COVERAGE

from mireva.mappers import OpenIMISToMirevaMapper
from mireva.models import MirevaClaimResult, MirevaEvaluation
from mireva.notifications import notify_reviewers_after_commit

logger = logging.getLogger(__name__)

# openIMIS rejection reasons (claim.validations) for MIREVA hard rules. Anything not
# listed here is reported as "invalid claim" instead of the old, misleading code 1
# ("invalid item or service").
HARD_RULE_REJECTION_REASONS = {
    "visit_before_policy": REJECTION_REASON_NO_COVERAGE,
    "visit_after_policy": REJECTION_REASON_NO_COVERAGE,
    "amount_non_positive": REJECTION_REASON_INVALID_CLAIM,
    "patient_deceased_before_visit": REJECTION_REASON_INVALID_CLAIM,
    "duplicate_claim": REJECTION_REASON_INVALID_CLAIM,
    "male_pregnancy_related": REJECTION_REASON_INVALID_CLAIM,
}

try:
    from mireva_sdk import MirevaClient
except ImportError:  # pragma: no cover - fallback for alternate SDK layout
    from mireva_sdk.client import MirevaClient  # type: ignore


class MirevaService:
    """Coordinates claim mapping, prediction calls, and state updates."""

    # REJECT is applied to the claim automatically only for deterministic hard-rule
    # rejections by default; a model-only REJECT goes to a human reviewer instead.
    AUTO_REJECT_HARD_RULES = "hard_rules"
    AUTO_REJECT_ALWAYS = "always"
    # MIREVA accepts at most 50 prior claims per request.
    DEFAULT_HISTORY_LIMIT = 10
    MAX_HISTORY_LIMIT = 50

    def __init__(self) -> None:
        """Create SDK client and mapper using Django settings."""
        base_url = getattr(settings, "MIREVA_URL", None) or os.getenv("MIREVA_URL")
        api_key = getattr(settings, "MIREVA_API_KEY", None) or os.getenv("MIREVA_API_KEY", "")
        if not base_url:
            raise ValueError(
                "MIREVA_URL is not configured. Set it in Django settings or environment."
            )
        model_version = getattr(settings, "MIREVA_MODEL_VERSION", None) or os.getenv(
            "MIREVA_MODEL_VERSION"
        )
        client_kwargs: dict[str, Any] = {"base_url": base_url, "api_key": api_key}
        if model_version:
            # Otherwise the SDK default ("mireva-origin") is used for every claim.
            client_kwargs["model_version"] = model_version
        self.client = MirevaClient(**client_kwargs)
        auto_reject = getattr(settings, "MIREVA_AUTO_REJECT", None) or os.getenv(
            "MIREVA_AUTO_REJECT", self.AUTO_REJECT_HARD_RULES
        )
        if auto_reject not in (self.AUTO_REJECT_HARD_RULES, self.AUTO_REJECT_ALWAYS):
            raise ValueError(
                f"MIREVA_AUTO_REJECT must be '{self.AUTO_REJECT_HARD_RULES}' or "
                f"'{self.AUTO_REJECT_ALWAYS}', got '{auto_reject}'."
            )
        self.auto_reject = auto_reject
        store_payload = getattr(settings, "MIREVA_STORE_PAYLOAD", None)
        self.store_payload = True if store_payload is None else bool(store_payload)
        limit = getattr(settings, "MIREVA_PATIENT_HISTORY_LIMIT", None)
        if limit is None:
            limit = os.getenv("MIREVA_PATIENT_HISTORY_LIMIT", self.DEFAULT_HISTORY_LIMIT)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise ValueError(f"MIREVA_PATIENT_HISTORY_LIMIT must be an integer, got {limit!r}.") from None
        if not 1 <= limit <= self.MAX_HISTORY_LIMIT:
            raise ValueError(
                f"MIREVA_PATIENT_HISTORY_LIMIT must be between 1 and {self.MAX_HISTORY_LIMIT}, got {limit}."
            )
        self.patient_history_limit = limit
        derive_license = getattr(settings, "MIREVA_DERIVE_LICENSE_FROM_FACILITY_STATUS", None)
        if derive_license is None:
            derive_license = os.getenv("MIREVA_DERIVE_LICENSE_FROM_FACILITY_STATUS", "").lower() in ("1", "true", "yes")
        self.mapper = OpenIMISToMirevaMapper(derive_license=bool(derive_license))

    def evaluate_claim(self, claim_uuid: str, skip_existing: bool = False) -> MirevaClaimResult:
        """Evaluate a claim with MIREVA and apply resulting workflow actions.

        With ``skip_existing`` an already-evaluated claim is left untouched, which
        makes duplicate queued tasks harmless.
        """
        if skip_existing:
            existing = MirevaClaimResult.objects.filter(claim__uuid=claim_uuid).first()
            if existing is not None:
                return existing
        claim = (
            Claim.objects.select_related("insuree", "health_facility")
            .prefetch_related(
                "items__item", "items__policy", "services__service", "services__policy"
            )
            .get(uuid=claim_uuid)
        )
        patient_history = self.mapper.fetch_patient_history(
            insuree_id=claim.insuree_id,
            exclude_claim_id=claim.id,
            limit=self.patient_history_limit,
        )
        provider_history = self.mapper.fetch_provider_history(
            hf_id=claim.health_facility_id, exclude_claim_id=claim.id, claim=claim
        )
        mireva_input = self.mapper.map_claim(claim, patient_history, provider_history)
        prediction = self.client.predict(mireva_input)
        normalized = self._normalize_prediction(prediction)

        with transaction.atomic():
            # Lock the claim so concurrent evaluations apply their decision once.
            claim = Claim.objects.select_for_update().get(pk=claim.pk)
            if skip_existing:
                existing = MirevaClaimResult.objects.filter(claim=claim).first()
                if existing is not None:
                    return existing
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
            action = self._apply_decision(
                claim, normalized["decision"], normalized["summary"], normalized
            )
            MirevaEvaluation.objects.create(
                claim=claim,
                decision=normalized["decision"],
                action=action,
                fraud_score=normalized["fraud_score"],
                risk_level=normalized["risk_level"],
                confidence=normalized["confidence"],
                reasons=normalized["reasons"],
                flags=normalized["flags"],
                model_version=normalized["model_version"],
                payload_hash=self._payload_hash(mireva_input),
                request_payload=mireva_input if self.store_payload else None,
            )
        return result

    @staticmethod
    def _payload_hash(payload: dict[str, Any]) -> str:
        """Stable SHA-256 of the request payload, for reproducibility checks."""
        canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

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
            "hard_rule_ids": [
                r.get("rule_id") for r in (hard_rules.get("rules_fired") or []) if isinstance(r, dict)
            ],
            "hard_rejected": bool(
                hard_rules.get("decision") == "REJECT" or payload.get("skipped_model")
            ),
        }

    def _apply_decision(
        self, claim: Claim, decision: str, summary: str, prediction: dict[str, Any]
    ) -> str:
        """Update claim workflow fields from the decision and return the action taken."""
        if decision == MirevaClaimResult.DECISION_APPROVE:
            claim.status = Claim.STATUS_PROCESSED
            claim.save(update_fields=["status"])
            return MirevaEvaluation.ACTION_APPROVED

        downgraded = (
            decision == MirevaClaimResult.DECISION_REJECT
            and self.auto_reject != self.AUTO_REJECT_ALWAYS
            and not prediction.get("hard_rejected")
        )
        if downgraded:
            logger.info(
                "MIREVA REJECT for claim %s came from the model only; sending to review.",
                claim.uuid,
            )

        if decision == MirevaClaimResult.DECISION_REVIEW or downgraded:
            claim.review_status = Claim.REVIEW_SELECTED
            claim.save(update_fields=["review_status"])
            self._notify_medical_officer(claim, prediction, downgraded)
            return (
                MirevaEvaluation.ACTION_REVIEW_DOWNGRADED
                if downgraded
                else MirevaEvaluation.ACTION_REVIEW_SELECTED
            )

        if decision == MirevaClaimResult.DECISION_REJECT:
            claim.status = Claim.STATUS_REJECTED
            # openIMIS Claim.rejection_reason is an integer code, not free text.
            claim.rejection_reason = self._extract_rejection_code(prediction)
            claim.explanation = self._rejection_explanation(prediction, summary)
            claim.save(update_fields=["status", "rejection_reason", "explanation"])
            return MirevaEvaluation.ACTION_REJECTED
        raise ValueError(f"Unsupported MIREVA decision: {decision!r}")

    def _notify_medical_officer(
        self, claim: Claim, prediction: dict[str, Any], downgraded: bool = False
    ) -> None:
        """Announce the review to users holding the reviewer right (after commit)."""
        logger.info(
            "MIREVA review needed for claim %s, score=%s", claim.uuid, prediction.get("fraud_score")
        )
        notify_reviewers_after_commit(
            str(claim.uuid),
            claim.code,
            prediction,
            downgraded,
            district_id=getattr(claim.health_facility, "location_id", None),
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
        """Return the openIMIS rejection reason code for a MIREVA rejection.

        An explicit numeric ``rejection_code`` from the service wins; otherwise the
        first fired hard rule is mapped, and anything else is "invalid claim".
        """
        raw_code = prediction.get("rejection_code")
        if raw_code is not None:
            try:
                return int(raw_code)
            except (TypeError, ValueError):
                pass
        for rule_id in prediction.get("hard_rule_ids") or []:
            if rule_id in HARD_RULE_REJECTION_REASONS:
                return HARD_RULE_REJECTION_REASONS[rule_id]
        return REJECTION_REASON_INVALID_CLAIM

    @staticmethod
    def _rejection_explanation(prediction: dict[str, Any], summary: str) -> str:
        """Human-readable rejection text: the hard-rule details when there are any."""
        details = [
            r.get("detail")
            for r in (prediction.get("reasons") or [])
            if isinstance(r, dict) and r.get("source") == "hard_rule" and r.get("detail")
        ]
        if details:
            return "Rejected by MIREVA hard rules: " + "; ".join(details)
        return summary or "Rejected by MIREVA risk decision."
