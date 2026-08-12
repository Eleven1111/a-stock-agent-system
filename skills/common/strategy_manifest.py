"""Declarative strategy manifests and the origin trust tiers bound to them.

A manifest is what a strategy *declares* about itself before any evidence
exists: who authored it, which sealed datasets it may read, and — the part the
registry enforces — which trust tier it belongs to.

The tier is not advice. ``MAXIMUM_PROMOTION_BY_ORIGIN`` is the ceiling
``strategy_registry.promote_strategy`` refuses to cross, so an externally
authored strategy cannot reach a promotion state that carries live weight even
if every other gate is satisfied. Admission evidence still lives in
``research_artifact``/``strategy_registry``; this module only fixes identity
and ceiling.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence


MANIFEST_SCHEMA = "strategy_manifest_v1"
ORIGINS = ("first_party", "trusted_contributor", "external_user")
DEFAULT_ORIGIN = "first_party"
STRATEGY_KINDS = {"cross_sectional_score", "event"}

# 与 strategy_registry.PROMOTION_STATES 同序。外部作者的策略封顶 shadow：
# 它可以被观察、被统计证伪，但 shadow 及以下的 live_weight 恒为 0。
PROMOTION_ORDER = (
    "research_only",
    "shadow",
    "eligible_for_manual_pilot",
    "manual_pilot",
    "live",
)
MAXIMUM_PROMOTION_BY_ORIGIN = {
    "first_party": "live",
    "trusted_contributor": "live",
    "external_user": "shadow",
}


class StrategyManifestError(ValueError):
    """A strategy manifest does not satisfy the declared contract."""

    def __init__(self, *errors: str) -> None:
        self.errors = tuple(dict.fromkeys(str(error) for error in errors if error))
        super().__init__("; ".join(self.errors) or "strategy_manifest_invalid")


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _required(mapping: Mapping[str, Any], names: Sequence[str]) -> list[str]:
    return [f"required:{name}" for name in names if not mapping.get(name)]


def normalize_origin(value: Any) -> str:
    """Unknown or absent origins read as first party — every legacy caller is.

    Callers that accept third-party input must validate the manifest instead of
    relying on this helper, which exists so records written before manifests
    keep a meaningful tier.
    """
    origin = str(value or "").strip()
    return origin if origin in ORIGINS else DEFAULT_ORIGIN


def maximum_promotion_state(origin: Any) -> str:
    return MAXIMUM_PROMOTION_BY_ORIGIN[normalize_origin(origin)]


def promotion_within_origin_ceiling(origin: Any, target_state: str) -> bool:
    """Whether ``target_state`` stays at or below the tier ceiling."""
    if target_state not in PROMOTION_ORDER:
        return False
    ceiling = maximum_promotion_state(origin)
    return PROMOTION_ORDER.index(target_state) <= PROMOTION_ORDER.index(ceiling)


def _input_errors(inputs: Any) -> list[str]:
    if not isinstance(inputs, Sequence) or isinstance(inputs, (str, bytes)) or not inputs:
        return ["inputs_missing"]
    errors: list[str] = []
    for index, item in enumerate(inputs):
        if not isinstance(item, Mapping):
            errors.append(f"input_invalid:{index}")
            continue
        errors.extend(
            f"{error}:inputs[{index}]"
            for error in _required(item, ("dataset_id", "contract_hash", "catalog_hash"))
        )
    return errors


def _timestamp_errors(manifest: Mapping[str, Any]) -> list[str]:
    raw = str(manifest.get("created_at") or "")
    if not raw:
        return []
    try:
        datetime.fromisoformat(raw)
    except ValueError:
        try:
            date.fromisoformat(raw)
        except ValueError:
            return ["created_at_invalid"]
    return []


def validate_manifest(manifest: Mapping[str, Any]) -> list[str]:
    """Return every contract violation; an empty list means the manifest holds."""
    errors = _required(
        manifest,
        (
            "strategy_id", "origin", "strategy_kind", "display_name",
            "description", "author", "inputs", "created_at",
        ),
    )
    if manifest.get("schema") != MANIFEST_SCHEMA:
        errors.append(f"schema_mismatch:{MANIFEST_SCHEMA}")
    origin = manifest.get("origin")
    if origin is not None and origin not in ORIGINS:
        errors.append("origin_invalid")
    if manifest.get("strategy_kind") not in STRATEGY_KINDS:
        errors.append("strategy_kind_invalid")
    author = manifest.get("author")
    if author is not None and not isinstance(author, Mapping):
        errors.append("author_invalid")
    elif isinstance(author, Mapping):
        errors.extend(_required(author, ("name",)))
    errors.extend(_input_errors(manifest.get("inputs")))
    errors.extend(_timestamp_errors(manifest))
    # 外部作者的策略在契约层就必须自认研究专用；registry 的档位天花板是第二道,
    # 两道都在才叫「即使有人改坏一处，权重仍是 0」。
    if origin == "external_user" and manifest.get("research_only") is not True:
        errors.append("external_user_requires_research_only")
    return list(dict.fromkeys(errors))


def seal_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Validate then bind a content hash; a declared mismatching hash is fatal."""
    body = dict(manifest)
    declared_hash = body.pop("manifest_hash", None)
    errors = validate_manifest(body)
    if errors:
        raise StrategyManifestError(*errors)
    actual_hash = _content_hash(body)
    if declared_hash is not None and declared_hash != actual_hash:
        raise StrategyManifestError("manifest_hash_mismatch")
    return {**body, "manifest_hash": actual_hash}


def load_manifest(path: str | Path) -> dict[str, Any]:
    target = Path(path).expanduser()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StrategyManifestError(f"manifest_unreadable:{target}") from exc
    if not isinstance(raw, Mapping):
        raise StrategyManifestError("manifest_not_an_object")
    return seal_manifest(raw)
