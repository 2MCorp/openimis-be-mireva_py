"""Sync this site's codes with the organization's OMOP mappings held by MIREVA.

Push: every active diagnosis, item and service code is registered with MIREVA, where
it waits unmapped until someone maps it. Pull: the mapped codes are copied into
``OmopConceptMapping``, which the mapper reads. Local-only mappings are left alone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterator

from medical.models import Diagnosis, Item, Service

from mireva.mappers import OpenIMISToMirevaMapper
from mireva.models import OmopConceptMapping

logger = logging.getLogger(__name__)

BATCH_SIZE = 2000

# vocabulary -> (queryset of active codes, name field, OMOP domain)
_SOURCES = (
    (OmopConceptMapping.VOCABULARY_ICD10, Diagnosis, "name", OmopConceptMapping.DOMAIN_CONDITION),
    (OmopConceptMapping.VOCABULARY_ITEM, Item, "name", OmopConceptMapping.DOMAIN_DRUG),
    (OmopConceptMapping.VOCABULARY_SERVICE, Service, "name", OmopConceptMapping.DOMAIN_PROCEDURE),
)
_DOMAIN_BY_VOCABULARY = {vocabulary: domain for vocabulary, _, _, domain in _SOURCES}


@dataclass
class ConceptSyncResult:
    pushed: int = 0
    created_remote: int = 0
    unmapped_remote: int = 0
    pulled: int = 0
    created_local: int = 0
    updated_local: int = 0


def collect_codes() -> Iterator[dict[str, Any]]:
    """Yield one registration entry per distinct active code, normalized as the mapper does."""
    for vocabulary, model, name_field, _ in _SOURCES:
        seen: set[str] = set()
        rows = model.objects.filter(validity_to__isnull=True).values_list("code", name_field)
        for raw_code, name in rows.iterator():
            code = OpenIMISToMirevaMapper.normalize_code(vocabulary, raw_code)
            if not code or code in seen:
                continue
            seen.add(code)
            yield {"vocabulary": vocabulary, "source_code": code, "source_name": (name or "")[:512] or None}


def _batches(entries: Iterator[dict[str, Any]]) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for entry in entries:
        batch.append(entry)
        if len(batch) == BATCH_SIZE:
            yield batch
            batch = []
    if batch:
        yield batch


def push_codes(client: Any, result: ConceptSyncResult) -> None:
    for batch in _batches(collect_codes()):
        response = client.sync_concepts(batch)
        result.pushed += len(batch)
        result.created_remote += response.get("created", 0)
        result.unmapped_remote = response.get("unmapped", 0)


def pull_mappings(client: Any, result: ConceptSyncResult) -> None:
    existing = {
        (m.source_vocabulary, m.source_code): m for m in OmopConceptMapping.objects.all()
    }
    for remote in client.get_concepts():
        vocabulary = remote["vocabulary"]
        code = remote["source_code"]
        domain = remote.get("domain") or _DOMAIN_BY_VOCABULARY.get(vocabulary)
        if domain is None or remote.get("concept_id") is None:
            continue
        result.pulled += 1
        local = existing.get((vocabulary, code))
        fields = {
            "concept_id": remote["concept_id"],
            "concept_name": (remote.get("concept_name") or remote.get("source_name") or "")[:255],
            "domain": domain,
        }
        if local is None:
            OmopConceptMapping.objects.create(source_code=code, source_vocabulary=vocabulary, **fields)
            result.created_local += 1
        elif any(getattr(local, k) != v for k, v in fields.items()):
            for k, v in fields.items():
                setattr(local, k, v)
            local.save(update_fields=list(fields))
            result.updated_local += 1


def sync_concepts(client: Any) -> ConceptSyncResult:
    """Run one push + pull cycle against MIREVA."""
    result = ConceptSyncResult()
    push_codes(client, result)
    pull_mappings(client, result)
    logger.info("MIREVA concept sync: %s", result)
    return result
