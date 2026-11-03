"""Per-line attribution: which claim lines MIREVA's rules pointed at."""

from __future__ import annotations

from typing import Any, Iterable

MAX_LINES_IN_TEXT = 5


def flagged_lines(flags: Iterable[Any] | None) -> dict[str, list[str]]:
    """Map each line id named by a rule hit to the rule ids that named it.

    ``flags`` is the stored list of fired rules. Hits without ``line_ids`` (claim-level rules) and
    entries from older results that have no such key are ignored. Rule ids keep their first-seen order.
    """
    out: dict[str, list[str]] = {}
    for hit in flags or []:
        if not isinstance(hit, dict):
            continue
        for line_id in hit.get("line_ids") or []:
            rules = out.setdefault(str(line_id), [])
            rule_id = str(hit.get("rule_id"))
            if rule_id not in rules:
                rules.append(rule_id)
    return out


def describe_flagged_lines(flags: Iterable[Any] | None, limit: int = MAX_LINES_IN_TEXT) -> str:
    """Short text such as ``item:7 (price_above_tariff), service:2 (category_ineligible) and 1 more``."""
    lines = flagged_lines(flags)
    parts = [f"{line} ({', '.join(rules)})" for line, rules in list(lines.items())[:limit]]
    if len(lines) > limit:
        parts.append(f"and {len(lines) - limit} more")
    return ", ".join(parts)
