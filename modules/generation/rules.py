"""
Rule engine helpers for task generation.

Rules are a lightweight, portable representation of task constraints. They can
be generated from invariants or authored externally, then compiled back into
invariants for validator evaluation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from modules.models import Invariant


@dataclass
class RuleToken:
    token_id: str
    value: Any
    kind: str = "literal"

    def to_dict(self) -> Dict[str, Any]:
        return {"value": self.value, "kind": self.kind}


@dataclass
class Rule:
    group: str
    resource_type: str
    field: str
    op: str
    token: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "group": self.group,
            "resource_type": self.resource_type,
            "field": self.field,
            "op": self.op,
            "token": self.token,
        }


@dataclass
class RuleSet:
    version: str = "v1"
    tokens: Dict[str, RuleToken] = field(default_factory=dict)
    rules: List[Rule] = field(default_factory=list)
    source: str = "invariants"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "tokens": {token_id: token.to_dict() for token_id, token in self.tokens.items()},
            "rules": [rule.to_dict() for rule in self.rules],
        }


def _token_key(value: Any) -> Tuple[str, str]:
    """
    Stable key for token reuse across rules.
    """
    if value is None:
        return ("none", "")
    if isinstance(value, (bool, int, float)):
        return (type(value).__name__, str(value))
    if isinstance(value, str):
        return ("str", value)
    try:
        encoded = json.dumps(value, sort_keys=True)
        return ("json", encoded)
    except Exception:
        return ("repr", str(value))


def build_rules_from_invariants(invariants: Iterable[Invariant]) -> RuleSet:
    """
    Create a ruleset that faithfully represents the provided invariants.
    """
    ruleset = RuleSet()
    token_map: Dict[Tuple[str, str], str] = {}
    token_index = 0

    for idx, invariant in enumerate(invariants):
        group = f"inv_{idx + 1}"
        for field, value in (invariant.match or {}).items():
            op = (invariant.comparison_rule or {}).get(field, "exact_match")
            if op not in ("exact_match", "starts_with", "ends_with"):
                op = "exact_match"

            key = _token_key(value)
            token_id = token_map.get(key)
            if token_id is None:
                token_index += 1
                token_id = f"token_{token_index}"
                token_map[key] = token_id
                ruleset.tokens[token_id] = RuleToken(token_id=token_id, value=value)

            ruleset.rules.append(
                Rule(
                    group=group,
                    resource_type=invariant.resource_type,
                    field=field,
                    op=op,
                    token=token_id,
                )
            )

    return ruleset


def compile_rules_to_invariants(ruleset: RuleSet) -> List[Invariant]:
    """
    Compile a ruleset into validator invariants.
    """
    grouped: Dict[str, Dict[str, Any]] = {}
    for rule in ruleset.rules:
        entry = grouped.setdefault(rule.group, {"resource_type": rule.resource_type, "match": {}, "comparison": {}})
        # Keep the first resource_type; ignore conflicting ones if mis-specified.
        entry["resource_type"] = entry.get("resource_type") or rule.resource_type
        token = ruleset.tokens.get(rule.token)
        value = token.value if token is not None else None
        entry["match"][rule.field] = value
        entry["comparison"][rule.field] = rule.op

    invariants: List[Invariant] = []
    for group_id in sorted(grouped.keys()):
        entry = grouped[group_id]
        invariants.append(
            Invariant(
                resource_type=str(entry.get("resource_type") or ""),
                match=dict(entry.get("match") or {}),
                comparison_rule=dict(entry.get("comparison") or {}),
            )
        )

    return invariants


def ruleset_from_dict(payload: Dict[str, Any]) -> RuleSet:
    version = str(payload.get("version") or "v1")
    source = str(payload.get("source") or "external")
    tokens_raw = payload.get("tokens") or {}
    rules_raw = payload.get("rules") or []

    tokens: Dict[str, RuleToken] = {}
    for token_id, token_payload in tokens_raw.items():
        if not isinstance(token_payload, dict):
            continue
        tokens[str(token_id)] = RuleToken(
            token_id=str(token_id),
            value=token_payload.get("value"),
            kind=str(token_payload.get("kind") or "literal"),
        )

    rules: List[Rule] = []
    for raw in rules_raw:
        if not isinstance(raw, dict):
            continue
        rules.append(
            Rule(
                group=str(raw.get("group") or "inv_1"),
                resource_type=str(raw.get("resource_type") or ""),
                field=str(raw.get("field") or ""),
                op=str(raw.get("op") or "exact_match"),
                token=str(raw.get("token") or ""),
            )
        )

    return RuleSet(version=version, tokens=tokens, rules=rules, source=source)

