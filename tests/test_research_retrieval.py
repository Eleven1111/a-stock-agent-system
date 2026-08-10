import json

import pytest

import research_retrieval as retrieval


def _document(
    document_id,
    *,
    title,
    content,
    source_rank="external_reference",
    published_at="2026-08-08T09:00:00+08:00",
    available_at="2026-08-08T09:05:00+08:00",
    scopes=None,
    claims=None,
):
    return {
        "schema": "research_document_v1",
        "document_id": document_id,
        "title": title,
        "content": content,
        "source": {
            "name": f"source-{document_id}",
            "url": f"https://example.com/{document_id}",
            "source_rank": source_rank,
            "authority_scope": "issuer_statement" if source_rank == "primary_official" else "commentary",
        },
        "published_at": published_at,
        "available_at": available_at,
        "access_scopes": scopes or ["public"],
        "license": "research_reference",
        "claims": claims or [],
    }


def test_seal_and_store_documents_are_content_addressed(tmp_path):
    document = _document("doc-1", title="回购公告", content="公司公告回购股份计划。")

    first = retrieval.store_document(document, store_dir=str(tmp_path))
    second = retrieval.store_document(document, store_dir=str(tmp_path))

    assert first["created"] is True
    assert second["created"] is False
    assert first["document"]["document_hash"].startswith("sha256:")
    loaded = retrieval.load_documents(str(tmp_path))
    assert [item["document_id"] for item in loaded] == ["doc-1"]


def test_time_and_access_filters_fail_closed_without_erasing_diagnostics():
    documents = [
        _document("eligible", title="回购公告", content="公司公告回购股份。"),
        _document(
            "future",
            title="未来公告",
            content="未来发布的回购信息。",
            published_at="2026-08-11T09:00:00+08:00",
            available_at="2026-08-11T09:05:00+08:00",
        ),
        _document(
            "restricted",
            title="内部研究",
            content="内部回购研究。",
            scopes=["internal_research"],
        ),
    ]

    result = retrieval.search(
        documents,
        "回购",
        asof="2026-08-10T15:00:00+08:00",
        allowed_scopes=["public"],
    )

    assert [item["document_id"] for item in result["results"]] == ["eligible"]
    assert result["excluded"] == {"future_or_unavailable": 1, "access_denied": 1}
    assert result["absence_means_no_evidence"] is False


def test_hybrid_ranking_blends_lexical_semantic_and_source_authority():
    documents = [
        _document(
            "official",
            title="上市公司回购公告",
            content="公司正式披露股份回购计划。",
            source_rank="primary_official",
        ),
        _document(
            "commentary",
            title="市场观点",
            content="市场人士讨论股份回购可能性。",
            source_rank="external_reference",
        ),
        _document(
            "semantic",
            title="资本配置",
            content="董事会讨论资本配置方案。",
            source_rank="derived_internal",
        ),
    ]

    lexical = retrieval.search(
        documents,
        "股份回购",
        asof="2026-08-10T15:00:00+08:00",
        allowed_scopes=["public"],
    )
    hybrid = retrieval.search(
        documents,
        "股份回购",
        asof="2026-08-10T15:00:00+08:00",
        allowed_scopes=["public"],
        semantic_scores={"official": 0.6, "commentary": 0.2, "semantic": 1.0},
    )

    assert lexical["results"][0]["document_id"] == "official"
    assert hybrid["results"][0]["document_id"] in {"official", "semantic"}
    assert all("score_components" in item for item in hybrid["results"])
    assert hybrid["retrieval_mode"] == "hybrid"


def test_conflicting_claims_are_preserved_in_bundle():
    documents = [
        _document(
            "support",
            title="需求上升",
            content="公司认为下游需求正在上升。",
            claims=[{"key": "demand_outlook", "stance": "support"}],
        ),
        _document(
            "oppose",
            title="需求承压",
            content="行业协会表示下游需求仍然承压。",
            claims=[{"key": "demand_outlook", "stance": "oppose"}],
        ),
    ]

    result = retrieval.search(
        documents,
        "下游需求",
        asof="2026-08-10T15:00:00+08:00",
        allowed_scopes=["public"],
    )

    assert result["conflicts"] == [
        {"claim_key": "demand_outlook", "stances": ["oppose", "support"], "document_ids": ["oppose", "support"]}
    ]
    assert all("content" not in item for item in result["results"])
    assert all(item["citation"]["url"].startswith("https://") for item in result["results"])
    assert all(item["excerpt"]["trust"] == "untrusted_external_data" for item in result["results"])


def test_source_url_rejects_non_http_instruction_channels():
    document = _document("doc-1", title="公告", content="股份回购公告。")
    document["source"]["url"] = "javascript:ignore-previous-instructions"

    with pytest.raises(retrieval.RetrievalError, match="source_url_invalid"):
        retrieval.seal_document(document)


@pytest.mark.parametrize(
    ("semantic_scores", "expected"),
    [
        ({"missing": 0.5}, "semantic_document_unknown"),
        ({"doc-1": 1.5}, "semantic_score_invalid"),
    ],
)
def test_semantic_scores_are_strictly_bounded(semantic_scores, expected):
    documents = [_document("doc-1", title="公告", content="股份回购公告。")]

    with pytest.raises(retrieval.RetrievalError, match=expected):
        retrieval.search(
            documents,
            "回购",
            asof="2026-08-10T15:00:00+08:00",
            allowed_scopes=["public"],
            semantic_scores=semantic_scores,
        )


def test_tampered_stored_document_is_rejected(tmp_path):
    stored = retrieval.store_document(
        _document("doc-1", title="公告", content="股份回购公告。"),
        store_dir=str(tmp_path),
    )
    path = tmp_path / f"{stored['document']['document_hash'].removeprefix('sha256:')}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["content"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(retrieval.RetrievalError, match="document_hash_mismatch"):
        retrieval.load_documents(str(tmp_path))


def test_retrieval_bundle_is_content_addressed_and_tamper_evident(tmp_path):
    bundle = retrieval.search(
        [_document("doc-1", title="公告", content="股份回购公告。")],
        "回购",
        asof="2026-08-10T15:00:00+08:00",
        allowed_scopes=["public"],
    )

    first = retrieval.store_bundle(bundle, store_dir=str(tmp_path))
    second = retrieval.store_bundle(bundle, store_dir=str(tmp_path))
    assert first["created"] is True
    assert second["created"] is False
    assert retrieval.load_bundle(bundle["bundle_hash"], store_dir=str(tmp_path)) == bundle

    path = tmp_path / f"{bundle['bundle_hash'].removeprefix('sha256:')}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["query"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(retrieval.RetrievalError, match="bundle_hash_mismatch"):
        retrieval.load_bundle(bundle["bundle_hash"], store_dir=str(tmp_path))
