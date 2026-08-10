"""Deterministic, point-in-time hybrid retrieval for research documents."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from agent_evidence import untrusted_external_text
from paths import data_file
from state_store import atomic_write_json


DOCUMENT_SCHEMA = "research_document_v1"
BUNDLE_SCHEMA = "retrieval_bundle_v1"
DOCUMENT_FIELDS = {
    "schema",
    "document_id",
    "title",
    "content",
    "source",
    "published_at",
    "available_at",
    "access_scopes",
    "license",
    "claims",
    "document_hash",
}
SOURCE_FIELDS = {"name", "url", "source_rank", "authority_scope"}
CLAIM_FIELDS = {"key", "stance"}
SOURCE_RANKS = {
    "primary_official": 1.0,
    "primary_market": 0.9,
    "derived_internal": 0.7,
    "external_reference": 0.4,
}
CLAIM_STANCES = {"support", "oppose", "neutral"}
_TOKEN = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


class RetrievalError(ValueError):
    """A document or query violates the bounded retrieval contract."""

    def __init__(self, *errors: str) -> None:
        self.errors = tuple(dict.fromkeys(str(error) for error in errors if error))
        super().__init__("; ".join(self.errors) or "retrieval_invalid")


def default_document_store_dir() -> str:
    return data_file("research-committee", "retrieval", "documents")


def default_bundle_store_dir() -> str:
    return data_file("research-committee", "retrieval", "bundles")


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise RetrievalError("payload_not_canonical_json") from exc


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _aware(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise RetrievalError(f"{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RetrievalError(f"{field}_timezone_required")
    return parsed


def _strict(value: Any, allowed: set[str], prefix: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RetrievalError(f"{prefix}_invalid")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise RetrievalError(*(f"{prefix}_field_not_allowed:{item}" for item in unknown))
    return dict(value)


def _claims(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise RetrievalError("claims_invalid")
    result = []
    for raw in value:
        claim = _strict(raw, CLAIM_FIELDS, "claim")
        key = str(claim.get("key") or "").strip()
        stance = str(claim.get("stance") or "").strip()
        if not key:
            raise RetrievalError("claim_key_missing")
        if stance not in CLAIM_STANCES:
            raise RetrievalError("claim_stance_invalid")
        result.append({"key": key, "stance": stance})
    return result


def seal_document(value: Mapping[str, Any]) -> dict[str, Any]:
    document = _strict(value, DOCUMENT_FIELDS, "document")
    declared_hash = document.pop("document_hash", None)
    if document.get("schema") != DOCUMENT_SCHEMA:
        raise RetrievalError("document_schema_invalid")
    for field in ("document_id", "title", "content", "license"):
        if not str(document.get(field) or "").strip():
            raise RetrievalError(f"document_{field}_missing")
    if len(str(document["content"])) > 100_000:
        raise RetrievalError("document_content_too_large")
    source = _strict(document.get("source"), SOURCE_FIELDS, "source")
    for field in ("name", "url", "authority_scope"):
        if not str(source.get(field) or "").strip():
            raise RetrievalError(f"source_{field}_missing")
    parsed_url = urlparse(str(source["url"]))
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise RetrievalError("source_url_invalid")
    if source.get("source_rank") not in SOURCE_RANKS:
        raise RetrievalError("source_rank_invalid")
    published = _aware(document.get("published_at"), "published_at")
    available = _aware(document.get("available_at"), "available_at")
    if available < published:
        raise RetrievalError("available_before_published")
    scopes = document.get("access_scopes")
    if not isinstance(scopes, list) or not scopes or any(
        not str(scope).strip() for scope in scopes
    ):
        raise RetrievalError("access_scopes_invalid")
    body = {
        **document,
        "source": source,
        "published_at": published.isoformat(),
        "available_at": available.isoformat(),
        "access_scopes": sorted(set(str(scope) for scope in scopes)),
        "claims": _claims(document.get("claims")),
    }
    actual_hash = _hash(body)
    if declared_hash is not None and declared_hash != actual_hash:
        raise RetrievalError("document_hash_mismatch")
    return {**body, "document_hash": actual_hash}


def store_document(
    value: Mapping[str, Any], *, store_dir: str | None = None
) -> dict[str, Any]:
    document = seal_document(value)
    path = Path(store_dir or default_document_store_dir()) / (
        f"{document['document_hash'].removeprefix('sha256:')}.json"
    )
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RetrievalError("document_unreadable") from exc
        sealed = seal_document(existing)
        if sealed["document_hash"] != document["document_hash"]:
            raise RetrievalError("document_hash_mismatch")
        return {"created": False, "document": sealed, "artifact_path": str(path)}
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(path), document)
    return {"created": True, "document": document, "artifact_path": str(path)}


def load_documents(store_dir: str | None = None) -> list[dict[str, Any]]:
    documents = []
    for path in sorted(Path(store_dir or default_document_store_dir()).glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RetrievalError("document_unreadable") from exc
        document = seal_document(raw)
        expected_name = document["document_hash"].removeprefix("sha256:") + ".json"
        if path.name != expected_name:
            raise RetrievalError("document_hash_mismatch")
        documents.append(document)
    ids = [document["document_id"] for document in documents]
    if len(ids) != len(set(ids)):
        raise RetrievalError("duplicate_document_id")
    return documents


def _verify_bundle(value: Any, expected_hash: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != BUNDLE_SCHEMA:
        raise RetrievalError("bundle_schema_invalid")
    declared_hash = str(value.get("bundle_hash") or "")
    body = {key: item for key, item in value.items() if key != "bundle_hash"}
    actual_hash = _hash(body)
    if declared_hash != actual_hash or (
        expected_hash is not None and declared_hash != expected_hash
    ):
        raise RetrievalError("bundle_hash_mismatch")
    if value.get("research_only") is not True or value.get("trading_action") != "none":
        raise RetrievalError("bundle_boundary_invalid")
    return value


def store_bundle(
    value: Mapping[str, Any], *, store_dir: str | None = None
) -> dict[str, Any]:
    bundle = _verify_bundle(dict(value))
    path = Path(store_dir or default_bundle_store_dir()) / (
        f"{bundle['bundle_hash'].removeprefix('sha256:')}.json"
    )
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RetrievalError("bundle_unreadable") from exc
        _verify_bundle(existing, bundle["bundle_hash"])
        return {"created": False, "bundle": existing, "artifact_path": str(path)}
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(path), bundle)
    return {"created": True, "bundle": bundle, "artifact_path": str(path)}


def load_bundle(bundle_hash: str, *, store_dir: str | None = None) -> dict[str, Any]:
    normalized = str(bundle_hash or "")
    if not normalized.startswith("sha256:") or len(normalized) != 71:
        raise RetrievalError("bundle_hash_invalid")
    path = Path(store_dir or default_bundle_store_dir()) / (
        f"{normalized.removeprefix('sha256:')}.json"
    )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RetrievalError("bundle_unreadable") from exc
    return _verify_bundle(value, normalized)


def _tokens(text: str) -> list[str]:
    base = _TOKEN.findall(str(text).casefold())
    chinese_bigrams = [
        first + second
        for first, second in zip(base, base[1:])
        if len(first) == len(second) == 1 and "\u4e00" <= first <= "\u9fff" and "\u4e00" <= second <= "\u9fff"
    ]
    return base + chinese_bigrams


def _lexical_scores(documents: list[dict[str, Any]], query_tokens: list[str]) -> dict[str, float]:
    token_rows = {
        document["document_id"]: _tokens(document["title"]) * 2 + _tokens(document["content"])
        for document in documents
    }
    lengths = [len(tokens) for tokens in token_rows.values()]
    average_length = sum(lengths) / len(lengths) if lengths else 1.0
    document_frequency = {
        token: sum(1 for tokens in token_rows.values() if token in set(tokens))
        for token in set(query_tokens)
    }
    scores: dict[str, float] = {}
    count = len(documents)
    for document_id, tokens in token_rows.items():
        frequencies = Counter(tokens)
        score = 0.0
        for token in set(query_tokens):
            frequency = frequencies[token]
            if not frequency:
                continue
            df = document_frequency[token]
            inverse = math.log(1 + (count - df + 0.5) / (df + 0.5))
            denominator = frequency + 1.2 * (0.25 + 0.75 * len(tokens) / average_length)
            score += inverse * frequency * 2.2 / denominator
        scores[document_id] = score
    maximum = max(scores.values(), default=0.0)
    return {
        document_id: (score / maximum if maximum else 0.0)
        for document_id, score in scores.items()
    }


def _semantic_scores(
    value: Mapping[str, Any] | None, document_ids: set[str]
) -> dict[str, float]:
    if value is None:
        return {}
    unknown = sorted(set(value) - document_ids)
    if unknown:
        raise RetrievalError(*(f"semantic_document_unknown:{item}" for item in unknown))
    result = {}
    for document_id, score in value.items():
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= float(score) <= 1:
            raise RetrievalError(f"semantic_score_invalid:{document_id}")
        result[str(document_id)] = float(score)
    return result


def _excerpt(content: str, query_tokens: list[str], limit: int = 280) -> str:
    lowered = content.casefold()
    offsets = [lowered.find(token) for token in query_tokens if lowered.find(token) >= 0]
    start = max(0, (min(offsets) if offsets else 0) - 60)
    return content[start:start + limit]


def _conflicts(results: list[dict[str, Any]], documents: Mapping[str, dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, set[str]]] = {}
    for result in results:
        document = documents[result["document_id"]]
        for claim in document["claims"]:
            entry = by_key.setdefault(claim["key"], {"stances": set(), "document_ids": set()})
            entry["stances"].add(claim["stance"])
            entry["document_ids"].add(document["document_id"])
    return [
        {
            "claim_key": key,
            "stances": sorted(value["stances"]),
            "document_ids": sorted(value["document_ids"]),
        }
        for key, value in sorted(by_key.items())
        if {"support", "oppose"} <= value["stances"]
    ]


def _eligible_documents(
    documents: list[dict[str, Any]],
    *,
    cutoff: datetime,
    scopes: set[str],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    eligible = []
    excluded = {"future_or_unavailable": 0, "access_denied": 0}
    for document in documents:
        if _aware(document["published_at"], "published_at") > cutoff or _aware(
            document["available_at"], "available_at"
        ) > cutoff:
            excluded["future_or_unavailable"] += 1
        elif not set(document["access_scopes"]) <= scopes:
            excluded["access_denied"] += 1
        else:
            eligible.append(document)
    return eligible, excluded


def _rank_documents(
    documents: list[dict[str, Any]],
    query_tokens: list[str],
    semantic: Mapping[str, float],
    *,
    hybrid: bool,
) -> list[tuple[float, dict[str, Any], float, float, float]]:
    lexical = _lexical_scores(documents, query_tokens)
    weights = (0.55, 0.35, 0.10) if hybrid else (0.90, 0.0, 0.10)
    ranked = []
    for document in documents:
        lexical_score = lexical.get(document["document_id"], 0.0)
        semantic_score = semantic.get(document["document_id"], 0.0)
        if lexical_score == 0 and semantic_score == 0:
            continue
        authority = SOURCE_RANKS[document["source"]["source_rank"]]
        total = (
            weights[0] * lexical_score
            + weights[1] * semantic_score
            + weights[2] * authority
        )
        ranked.append((total, document, lexical_score, semantic_score, authority))
    ranked.sort(key=lambda item: (-item[0], item[1]["document_id"]))
    return ranked


def _result_row(
    ranked: tuple[float, dict[str, Any], float, float, float],
    query_tokens: list[str],
) -> dict[str, Any]:
    total, document, lexical_score, semantic_score, authority = ranked
    return {
        "document_id": document["document_id"],
        "document_hash": document["document_hash"],
        "excerpt": untrusted_external_text(
            _excerpt(document["content"], query_tokens),
            source=document["source"]["url"],
        ),
        "citation": {
            "title": document["title"],
            "url": document["source"]["url"],
            "source_name": document["source"]["name"],
            "source_rank": document["source"]["source_rank"],
            "published_at": document["published_at"],
            "available_at": document["available_at"],
        },
        "score": round(total, 8),
        "score_components": {
            "lexical": round(lexical_score, 8),
            "semantic": round(semantic_score, 8),
            "authority": authority,
        },
    }


def search(
    documents: Sequence[Mapping[str, Any]],
    query: str,
    *,
    asof: str,
    allowed_scopes: Sequence[str],
    semantic_scores: Mapping[str, float] | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    query_value = str(query or "").strip()
    if not query_value:
        raise RetrievalError("query_missing")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise RetrievalError("limit_invalid")
    cutoff = _aware(asof, "asof")
    scopes = {str(scope) for scope in allowed_scopes if str(scope).strip()}
    if not scopes:
        raise RetrievalError("allowed_scopes_missing")
    sealed = [seal_document(document) for document in documents]
    ids = [document["document_id"] for document in sealed]
    if len(ids) != len(set(ids)):
        raise RetrievalError("duplicate_document_id")
    semantic = _semantic_scores(semantic_scores, set(ids))
    eligible, excluded = _eligible_documents(sealed, cutoff=cutoff, scopes=scopes)
    query_tokens = _tokens(query_value)
    hybrid = semantic_scores is not None
    ranked = _rank_documents(eligible, query_tokens, semantic, hybrid=hybrid)
    results = [
        _result_row(item, query_tokens) for item in ranked[:limit]
    ]
    document_map = {document["document_id"]: document for document in eligible}
    body = {
        "schema": BUNDLE_SCHEMA,
        "query": query_value,
        "asof": cutoff.isoformat(),
        "allowed_scopes": sorted(scopes),
        "retrieval_mode": "hybrid" if hybrid else "lexical_authority",
        "results": results,
        "conflicts": _conflicts(results, document_map),
        "excluded": excluded,
        "absence_means_no_evidence": False,
        "research_only": True,
        "trading_action": "none",
    }
    return {**body, "bundle_hash": _hash(body)}


__all__ = [
    "BUNDLE_SCHEMA",
    "DOCUMENT_SCHEMA",
    "RetrievalError",
    "default_bundle_store_dir",
    "default_document_store_dir",
    "load_bundle",
    "load_documents",
    "seal_document",
    "search",
    "store_bundle",
    "store_document",
]
