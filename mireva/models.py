from django.db import models
from django.utils.translation import gettext_lazy as _


class MirevaClaimResult(models.Model):
    """Stores the MIREVA fraud evaluation result for a single claim."""

    DECISION_APPROVE = "APPROVE"
    DECISION_REVIEW = "REVIEW"
    DECISION_REJECT = "REJECT"
    DECISION_CHOICES = (
        (DECISION_APPROVE, _("Approve")),
        (DECISION_REVIEW, _("Review")),
        (DECISION_REJECT, _("Reject")),
    )

    RISK_LOW = "LOW"
    RISK_MEDIUM = "MEDIUM"
    RISK_HIGH = "HIGH"
    RISK_LEVEL_CHOICES = (
        (RISK_LOW, _("Low")),
        (RISK_MEDIUM, _("Medium")),
        (RISK_HIGH, _("High")),
    )

    claim = models.OneToOneField(
        "claim.Claim", on_delete=models.CASCADE, related_name="mireva_result"
    )
    decision = models.CharField(max_length=10, choices=DECISION_CHOICES)
    fraud_score = models.FloatField()
    risk_level = models.CharField(max_length=10, choices=RISK_LEVEL_CHOICES)
    confidence = models.FloatField()
    reasons = models.JSONField(default=list)
    flags = models.JSONField(default=list)
    model_version = models.CharField(max_length=64)
    evaluated_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        "core.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="mireva_reviews",
    )
    review_decision = models.CharField(
        max_length=10, choices=DECISION_CHOICES, null=True, blank=True
    )
    review_note = models.TextField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "mireva_claim_result"


class OmopConceptMapping(models.Model):
    """Maps source diagnosis/item/service codes to OMOP concept identifiers."""

    VOCABULARY_ICD10 = "ICD-10"
    VOCABULARY_ITEM = "openIMIS-ITEM"
    VOCABULARY_SERVICE = "openIMIS-SERVICE"
    SOURCE_VOCABULARY_CHOICES = (
        (VOCABULARY_ICD10, VOCABULARY_ICD10),
        (VOCABULARY_ITEM, VOCABULARY_ITEM),
        (VOCABULARY_SERVICE, VOCABULARY_SERVICE),
    )

    DOMAIN_CONDITION = "condition"
    DOMAIN_DRUG = "drug"
    DOMAIN_PROCEDURE = "procedure"
    DOMAIN_CHOICES = (
        (DOMAIN_CONDITION, DOMAIN_CONDITION),
        (DOMAIN_DRUG, DOMAIN_DRUG),
        (DOMAIN_PROCEDURE, DOMAIN_PROCEDURE),
    )

    source_code = models.CharField(max_length=50)
    source_vocabulary = models.CharField(
        max_length=50, choices=SOURCE_VOCABULARY_CHOICES
    )
    concept_id = models.IntegerField()
    concept_name = models.CharField(max_length=255)
    domain = models.CharField(max_length=50, choices=DOMAIN_CHOICES)

    class Meta:
        db_table = "mireva_omop_mapping"
        unique_together = ("source_code", "source_vocabulary")
        indexes = [
            models.Index(fields=["source_code", "source_vocabulary"]),
        ]
