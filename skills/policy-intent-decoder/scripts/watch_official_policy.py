#!/usr/bin/env python3
"""Watch official policy sources and emit new A-share-relevant policy signals.

This script is intentionally lightweight and stdlib-first. It does not infer
policy intent from market media; it watches the source catalog bundled with the
skill, extracts candidate official links, scores titles with transparent
keywords, de-duplicates by fingerprint, and writes bounded state under
`$A_STOCK_STATE_HOME/skills/policy-intent-decoder/data/`.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[3]
COMMON = ROOT / "skills" / "common"
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

from http_client import DataSourceError, request_text  # noqa: E402
from paths import skill_data_dir  # noqa: E402
from state_store import atomic_write_json, read_json  # noqa: E402
import novelty_gate  # noqa: E402


BJ = timezone(timedelta(hours=8))
SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = SKILL_DIR / "references" / "official-policy-sources.json"
DATA_SKILL = "policy-intent-decoder"

POLICY_KEYWORDS = (
    "政治局",
    "中央经济工作会议",
    "中央金融",
    "国务院",
    "国常会",
    "政府工作报告",
    "资本市场",
    "股市",
    "稳股市",
    "活跃资本市场",
    "新国九条",
    "金融风险",
    "防风险",
    "强监管",
    "高质量发展",
    "新质生产力",
    "中长期资金",
    "回购",
    "分红",
    "并购重组",
    "注册制",
    "退市",
    "减持",
    "量化交易",
    "恶意做空",
    "财务造假",
    "依法查处",
    "专项整治",
    "反内卷",
    "全国统一大市场",
    "扩大内需",
    "促消费",
    "设备更新",
    "以旧换新",
    "专项债",
    "特别国债",
    "降准",
    "降息",
    "再贷款",
    "结构性货币政策",
    "房地产",
    "化债",
    "地方债",
    "人工智能",
    "半导体",
    "芯片",
    "算力",
    "新能源",
    "储能",
    "光伏",
    "电力",
    "低空经济",
    "机器人",
    "数据要素",
    "国产替代",
    "自主可控",
    "供应链安全",
)

HARD_TOOL_KEYWORDS = (
    "印发",
    "实施",
    "方案",
    "办法",
    "通知",
    "意见",
    "细则",
    "试点",
    "专项",
    "额度",
    "资金",
    "补贴",
    "税收",
    "监管",
    "处罚",
    "查处",
    "问责",
    "移送",
    "公安",
    "法院",
    "检察",
    "联合",
    "会同",
)

DEPARTMENT_KEYWORDS = (
    "证监会",
    "人民银行",
    "财政部",
    "发改委",
    "工业和信息化部",
    "金融监管总局",
    "交易所",
    "公安部",
    "最高法",
    "最高检",
    "商务部",
    "市场监管总局",
    "网信办",
)

ANCHOR_RE = re.compile(
    r"<a\b[^>]*?href=[\"'](?P<href>[^\"']+)[\"'][^>]*>(?P<label>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
DATE_PATTERNS = (
    re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})"),
    re.compile(r"(20\d{2})(\d{2})(\d{2})"),
    re.compile(r"(20\d{2})(\d{2})"),
)
GENERIC_NAV_TITLES = {
    "意见征集",
    "公告通知",
    "总局监管动态",
    "政策解读",
    "新闻发布",
    "工作动态",
    "更多",
    "加载更多",
}


def now_bj() -> str:
    return datetime.now(BJ).isoformat(timespec="seconds")


def normalize_text(value: str) -> str:
    value = html.unescape(value or "")
    value = TAG_RE.sub(" ", value)
    return SPACE_RE.sub(" ", value).strip()


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    path = re.sub(r"/+", "/", parts.path)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def fingerprint(source_id: str, title: str, url: str) -> str:
    basis = f"{source_id}|{canonical_url(url) or normalize_text(title)}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def parse_date_hint(*values: str) -> str | None:
    text = " ".join(value or "" for value in values)
    for pattern in DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        year = int(match.group(1))
        month = int(match.group(2))
        day = int(match.group(3)) if len(match.groups()) >= 3 else 1
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            continue
    return None


def freshness(published_hint: str | None, *, checked_at: str, lookback_days: int) -> str:
    if not published_hint:
        return "undated"
    try:
        published = date.fromisoformat(published_hint)
        checked = datetime.fromisoformat(checked_at).date()
    except ValueError:
        return "undated"
    if published > checked + timedelta(days=1):
        return "future_dated"
    if published < checked - timedelta(days=max(1, lookback_days)):
        return "stale"
    return "recent"


def load_catalog(path: str | os.PathLike[str]) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("policy source catalog has no sources")
    return payload


def extract_links(document: str, source: dict[str, Any]) -> list[dict[str, Any]]:
    base_url = str(source["url"])
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in ANCHOR_RE.finditer(document):
        title = normalize_text(match.group("label"))
        href = html.unescape(match.group("href") or "").strip()
        if (
            not title
            or len(title) < 4
            or title in GENERIC_NAV_TITLES
            or href.startswith(("javascript:", "#"))
            or "{" in href
            or "}" in href
        ):
            continue
        url = canonical_url(urljoin(base_url, href))
        if url in seen:
            continue
        seen.add(url)
        nearby = document[max(0, match.start() - 80): match.end() + 120]
        items.append({
            "title": title,
            "url": url,
            "published_hint": parse_date_hint(title, url, nearby),
            "source_id": source["id"],
            "source_name": source["name"],
            "source_rank": source["source_rank"],
            "source_type": source.get("source_type"),
        })
    return items


def score_item(item: dict[str, Any]) -> dict[str, Any] | None:
    title = item["title"]
    matched = [word for word in POLICY_KEYWORDS if word in title]
    hard_tools = [word for word in HARD_TOOL_KEYWORDS if word in title]
    departments = [word for word in DEPARTMENT_KEYWORDS if word in title]
    if not matched and not hard_tools:
        return None
    rank_score = int(str(item["source_rank"]).replace("S", "") or "0")
    hardness = min(5, len(hard_tools))
    coordination = "L2" if len(set(departments)) >= 2 or "联合" in title or "会同" in title else "L1"
    if "国务院" in title or item["source_rank"] == "S5":
        coordination = "L3" if coordination == "L1" else coordination
    if "政治局" in title or "中央经济工作会议" in title:
        coordination = "L4"
    signal_score = rank_score * 2 + min(5, len(matched)) + hardness
    enriched = dict(item)
    enriched.update({
        "fingerprint": fingerprint(item["source_id"], title, item["url"]),
        "matched_keywords": matched[:10],
        "tool_keywords": hard_tools[:10],
        "department_keywords": sorted(set(departments)),
        "coordination_level": coordination,
        "signal_score": signal_score,
        "should_decode": signal_score >= 7,
    })
    return enriched


def fetch_source(source: dict[str, Any], *, timeout: float, max_links: int) -> dict[str, Any]:
    fetched_at = now_bj()
    try:
        result = request_text(
            source["url"],
            source=source["id"],
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        candidates = []
        for item in extract_links(result.data, source):
            scored = score_item(item)
            if scored:
                candidates.append(scored)
        candidates.sort(key=lambda item: item["signal_score"], reverse=True)
        return {
            "source_id": source["id"],
            "status": "ok",
            "fetched_at": result.fetched_at,
            "items": candidates[:max_links],
        }
    except DataSourceError as exc:
        return {
            "source_id": source["id"],
            "status": "error",
            "fetched_at": fetched_at,
            "error": exc.to_dict(),
            "items": [],
        }
    except Exception as exc:
        return {
            "source_id": source["id"],
            "status": "error",
            "fetched_at": fetched_at,
            "error": {"error_type": type(exc).__name__, "error": str(exc)},
            "items": [],
        }


def state_paths() -> dict[str, str]:
    data_dir = skill_data_dir(DATA_SKILL)
    return {
        "data_dir": data_dir,
        "seen": os.path.join(data_dir, "seen_policy_items.json"),
        "latest": os.path.join(data_dir, "policy_watch_latest.json"),
        "runs_dir": os.path.join(data_dir, "policy_watch_runs"),
    }


def mark_new_items(items: list[dict[str, Any]], *, no_state: bool, max_seen: int) -> tuple[list[dict[str, Any]], list[str]]:
    if no_state:
        return items, []
    paths = state_paths()
    seen_payload = read_json(paths["seen"], {"fingerprints": []})
    seen = set(seen_payload.get("fingerprints") or [])
    new_items = [item for item in items if item["fingerprint"] not in seen]
    updated = list(dict.fromkeys([*(seen_payload.get("fingerprints") or []), *(item["fingerprint"] for item in items)]))
    if len(updated) > max_seen:
        updated = updated[-max_seen:]
    atomic_write_json(paths["seen"], {
        "schema": "policy_seen_fingerprints_v1",
        "updated_at": now_bj(),
        "fingerprints": updated,
    })
    return new_items, updated


def build_watch_result(
    catalog: dict[str, Any],
    *,
    timeout: float,
    max_per_source: int,
    no_state: bool,
    max_seen: int,
    lookback_days: int,
) -> dict[str, Any]:
    checked_at = now_bj()
    source_results = [
        fetch_source(source, timeout=timeout, max_links=max_per_source)
        for source in catalog["sources"]
    ]
    all_items: list[dict[str, Any]] = []
    for source_result in source_results:
        all_items.extend(source_result.get("items") or [])
    deduped = {item["fingerprint"]: item for item in all_items}
    ranked_items = sorted(
        deduped.values(),
        key=lambda item: (item["signal_score"], item["source_rank"], item["title"]),
        reverse=True,
    )
    for item in ranked_items:
        item["freshness"] = freshness(
            item.get("published_hint"),
            checked_at=checked_at,
            lookback_days=lookback_days,
        )
    decode_ready_items = [
        item
        for item in ranked_items
        if item["should_decode"] and item["freshness"] == "recent"
    ]
    new_items, _seen = mark_new_items(decode_ready_items, no_state=no_state, max_seen=max_seen)
    novelty = novelty_gate.NoveltyResult(new_items, [])
    if not no_state and new_items:
        novelty = novelty_gate.filter_items(
            new_items,
            namespace="market-news",
            job_id="official-policy-watch",
            now=datetime.now(timezone.utc),
        )
        new_items = novelty.items
    ok_sources = [item for item in source_results if item["status"] == "ok"]
    status = "no_signal"
    if not ok_sources:
        status = "insufficient_source"
    elif new_items:
        status = "ready"
    elif ranked_items:
        status = "no_new_signal"

    result = {
        "schema": "policy_intent_watch_v1",
        "checked_at": checked_at,
        "status": status,
        "research_only": True,
        "trading_action": "none",
        "source_catalog": {
            "path": str(DEFAULT_CATALOG),
            "source_count": len(catalog["sources"]),
            "contract": catalog.get("source_contract"),
        },
        "summary": {
            "ok_sources": len(ok_sources),
            "failed_sources": len(source_results) - len(ok_sources),
            "candidate_count": len(ranked_items),
            "new_count": len(new_items),
            "duplicate_event_count": novelty.duplicate_count,
            "lookback_days": lookback_days,
        },
        "archive_note": novelty_gate.duplicate_archive_note(novelty),
        "novelty_gate": {
            "fail_open": novelty.fail_open,
            "shadow": novelty.shadow,
            "would_suppress": novelty.would_suppress,
        },
        "signals": new_items,
        "candidate_preview": ranked_items[:25],
        "source_results": source_results,
    }
    if status == "insufficient_source":
        result["blocked_reason"] = "all official policy sources failed"
    return result


def persist_result(result: dict[str, Any]) -> None:
    paths = state_paths()
    atomic_write_json(paths["latest"], result)
    checked = result["checked_at"].replace(":", "").replace("-", "")
    run_dir = os.path.join(paths["runs_dir"], result["checked_at"][:10])
    atomic_write_json(os.path.join(run_dir, f"{checked}.json"), result)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--no-state", action="store_true", help="do not read/write seen fingerprints")
    parser.add_argument("--max-per-source", type=int, default=12)
    parser.add_argument("--max-seen", type=int, default=5000)
    parser.add_argument("--lookback-days", type=int, default=45)
    parser.add_argument("--timeout", type=float, default=8.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    catalog = load_catalog(args.catalog)
    result = build_watch_result(
        catalog,
        timeout=args.timeout,
        max_per_source=max(1, args.max_per_source),
        no_state=args.no_state,
        max_seen=max(100, args.max_seen),
        lookback_days=max(1, args.lookback_days),
    )
    if not args.no_state:
        persist_result(result)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for item in result["signals"]:
            print(f"[{item['source_rank']}] {item['title']} {item['url']}")
    return 1 if result["status"] == "insufficient_source" else 0


if __name__ == "__main__":
    raise SystemExit(main())
