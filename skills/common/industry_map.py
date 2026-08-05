"""全市场行业归属映射 —— 构建 / 按日缓存 / 注入。

背景
====
候选筛选的板块聚类、主线识别、龙头识别都依赖每只股票的 ``industry`` 字段，
但交易所上市列表只有深市带（粗口径的证监会）行业，沪市主板/科创板全为空，
导致 ``hot_money_selection`` 的板块覆盖长期偏低、主线判断有系统性偏差。

本模块反向汇出全市场 ``code -> 行业``，按交易日缓存，并把结果注入 universe
记录的 ``industry``。

数据源（2026-08-04 更换）
========================
原路径「枚举东财行业板块 → adata 逐板块取成分股」依赖 ``push2.eastmoney.com``，
该域名在生产机上直连与走代理**双双不通**：391 个板块全部失败、单轮耗时
700s+，导致 ``industry-map-refresh`` 自 2026-06-24 上线起**从未成功过一次**，
缓存文件从未生成，下游行业归属长期静默退化。现改为两段式：

1. **主源 = 新浪行业板块**（新浪行业 49 个 + 证监会行业 84 个，共 133 次请求、
   约 20s）。直连可达、无风控，实测覆盖全市场约 68%。
2. **补口 = 百度股市通逐只直查**（``getrelatedblock``，20 只/批），只补主源
   覆盖不到的代码（次新股、北交所等），**按批次预算封顶**并礼貌限速。该接口
   在高频调用下会返回 ``ResultCode=403`` 且封禁窗口较长，因此只能当补口用；
   补口失败不影响主源结果落盘，缺口靠逐日与旧缓存合并累积收敛。

设计要点
========
- **消费端零触网**：``candidate_discovery`` 只调用 :func:`load_cached` 读缓存；
  缓存缺失即注入为 no-op，绝不在主链上发起网络请求（无回归、无阻塞）。
- **构建端容错**：:func:`refresh` 由独立 CLI / cron 调用，限速 + 重试 +
  与昨日缓存合并；整体失败时保留旧缓存而非清空。
- **失败必须可见**：整体失败不写缓存，CLI 以非零退出码收场；消费端可用
  :func:`load_cached_status` 拿到「为什么是空的」，不允许静默退化为「其他」。
- **纯逻辑可测**：数据源以参数注入，核心 build / enrich / cache 全部可单测。
"""

from __future__ import annotations

import json
import time
import urllib.parse
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Mapping, Sequence, Tuple

from http_client import request_bytes
from paths import data_file
from state_store import atomic_write_json, read_json

SCHEMA = "industry_map_v1"
SOURCE = "resilient_industry_board"
# 新浪行业板块（主源）：两张分类表 + 逐板块成分股，均为直连 JSON。
_SINA_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_SINA_REFERER = "https://finance.sina.com.cn/"
_SINA_BOARD_LIST_URLS = (
    "http://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php",          # 新浪行业
    "http://money.finance.sina.com.cn/q/view/newFLJK.php?param=industry",  # 证监会行业
)
_SINA_CONSTITUENT_URL = (
    "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData?page=1&num=500&sort=symbol&asc=1&node={node}&symbol=&_s_r_a=page"
)
_SINA_TIMEOUT_SECONDS = 12.0
_SINA_PACE_SECONDS = 0.05

# 百度股市通个股关联板块接口（补口）：单次请求最多 20 只，超出部分被静默丢弃。
_BAIDU_RELATED_BLOCK_URL = "https://finance.pae.baidu.com/api/getrelatedblock"
_BAIDU_BATCH_SIZE = 20
_BAIDU_REFERER = "https://gushitong.baidu.com/"
_BAIDU_TIMEOUT_SECONDS = 12.0
# 该接口高频调用会触发 403 且封禁窗口以小时计，故限速远比主源保守，并对单轮
# 的批次数设上限——补不完的留给下一个交易日（缓存合并会累积）。
_BAIDU_PACE_SECONDS = 1.5
_BAIDU_MAX_BATCHES_PER_RUN = 120

# 覆盖率低于此值即认为构建失败（宁可保留旧缓存，也不写一份残缺映射覆盖它）。
MIN_COVERAGE_RATE = 0.50

# 默认允许缓存 asof 晚于查询 asof 的天数（回补历史日期时用得到）。
DEFAULT_MAX_FUTURE_DAYS = 30

# 连续失败到这个数量就判定为系统性故障（域名不通 / 被封），立刻中止整轮构建，
# 而不是把 200+ 个批次的重试全部跑完再撞作业超时——超时会让失败以 "timeout"
# 的形态出现，看不出真实原因。
_MAX_CONSECUTIVE_FAILURES = 10

class IndustrySourceDown(RuntimeError):
    """数据源被判定为系统性故障而提前中止；``partial`` 保留已经取到的部分结果。"""

    def __init__(self, message: str, partial: Mapping[str, str]):
        super().__init__(message)
        self.partial = dict(partial)


BoardsFetcher = Callable[[], Sequence[Tuple[str, str]]]
ConstituentsFetcher = Callable[[str], Sequence[str]]
UniverseFetcher = Callable[[], Sequence[Any]]
BatchIndustryFetcher = Callable[[Sequence[str]], Mapping[str, str]]


# ── 代码规整 ──────────────────────────────────────────────────────

def _norm_code(code: Any) -> str:
    text = str(code or "").strip().lower()
    if text.startswith(("sh", "sz", "bj")):
        text = text[2:]
    return text.zfill(6) if text else ""


# ── 默认数据源（真实，惰性导入，尊重环境代理）────────────────────

def _fetch_text(
    url: str,
    *,
    source: str,
    referer: str,
    encoding: str,
    timeout: float,
    min_interval_seconds: float,
) -> str:
    """走共享 ``http_client`` 的 GET。

    这里显式传 ``min_interval_seconds``：默认 2.5s 的进程级节流是给东财 WAF 用的，
    本作业单轮要扫一两百个板块端点，按默认节流会直接吃掉整个作业预算。新浪目录
    型端点无风控，百度补口则反过来要比默认更慢（见 ``_BAIDU_PACE_SECONDS``）。
    """
    raw = request_bytes(
        url,
        source=source,
        timeout=timeout,
        max_attempts=1,  # 重试由各构建器的 _fetch_with_retry 统一负责，避免相乘
        headers={"User-Agent": _SINA_UA, "Referer": referer},
        min_interval_seconds=min_interval_seconds,
    ).data
    # 新浪的 GBK 页面偶有非法字节，按 ignore 解码；宁可丢字符也不要整批失败。
    return raw.decode(encoding, "ignore")


def parse_sina_board_list(text: str) -> List[Tuple[str, str]]:
    """新浪板块分类表（``var X = {...}``）→ [(节点码, 行业名)]（纯函数，可单测）。"""
    start = text.find("{")
    if start < 0:
        return []
    payload = json.loads(text[start:].strip().rstrip(";"))
    boards: List[Tuple[str, str]] = []
    for node, row in payload.items():
        fields = str(row).split(",")
        name = fields[1].strip() if len(fields) > 1 else ""
        if node and name:
            boards.append((str(node), name))
    return boards


def _default_boards_fetcher() -> List[Tuple[str, str]]:
    """行业板块清单 → [(节点码, 行业名)]，取新浪行业 + 证监会行业两张表。"""
    boards: List[Tuple[str, str]] = []
    errors: List[str] = []
    for url in _SINA_BOARD_LIST_URLS:
        try:
            boards.extend(
                parse_sina_board_list(
                    _fetch_text(
                        url,
                        source="sina_industry_boards",
                        referer=_SINA_REFERER,
                        encoding="gbk",
                        timeout=_SINA_TIMEOUT_SECONDS,
                        min_interval_seconds=_SINA_PACE_SECONDS,
                    )
                )
            )
        except Exception as exc:  # noqa: BLE001 - 单张表失败不致整体中断
            errors.append(f"{url}: {exc}")
    if not boards:
        raise RuntimeError(f"行业板块清单为空（{'; '.join(errors) or '无错误信息'}）")
    return boards


def _default_constituents_fetcher(board_code: str) -> List[str]:
    """新浪板块成分股 → [code]。空结果视为可重试失败。"""
    rows = json.loads(
        _fetch_text(
            _SINA_CONSTITUENT_URL.format(node=urllib.parse.quote(board_code)),
            source="sina_industry_constituents",
            referer=_SINA_REFERER,
            encoding="gbk",
            timeout=_SINA_TIMEOUT_SECONDS,
            min_interval_seconds=_SINA_PACE_SECONDS,
        )
        or "[]"
    )
    codes = [str(row.get("code") or "") for row in rows or [] if isinstance(row, Mapping)]
    codes = [code for code in codes if code]
    if not codes:
        raise RuntimeError(f"板块 {board_code} 成分股为空")
    return codes


def _default_universe_fetcher() -> List[str]:
    """全市场股票代码清单（adata 上市列表）。"""
    import adata  # 惰性导入

    frame = adata.stock.info.all_code()
    codes = (
        []
        if frame is None or len(frame) == 0
        else [str(value) for value in frame["stock_code"].tolist()]
    )
    if not codes:
        raise RuntimeError("全市场代码清单为空")
    return codes


def _pick_sw_industry(blocks: Any) -> str:
    """从百度关联板块结构里取申万一级行业名。"""
    for block in blocks or []:
        if not isinstance(block, Mapping) or block.get("name") != "行业":
            continue
        items = [item for item in (block.get("list") or []) if isinstance(item, Mapping)]
        for item in items:
            if str(item.get("describe") or "").startswith("申万一级"):
                return str(item.get("name") or "").strip()
        if items:
            return str(items[0].get("name") or "").strip()
    return ""


def parse_baidu_related_blocks(payload: Mapping[str, Any]) -> Dict[str, str]:
    """百度 ``getrelatedblock`` 响应 → ``code -> 申万一级行业``（纯函数，可单测）。"""
    result = payload.get("Result")
    if not isinstance(result, Mapping):
        return {}
    mapping: Dict[str, str] = {}
    for raw_code, blocks in result.items():
        code = _norm_code(raw_code)
        industry = _pick_sw_industry(blocks)
        if code and industry:
            mapping[code] = industry
    return mapping


def _default_batch_industry_fetcher(codes: Sequence[str]) -> Dict[str, str]:
    """一批（≤20 只）股票 → ``code -> 申万一级行业``；空结果视为可重试失败。

    被限流时接口返回 HTTP 200 但 ``ResultCode=403`` + 空 ``Result``，因此空结果
    必须当失败处理（由 :func:`build_industry_map_batched` 的连续失败熔断兜底），
    不能当成"这批股票没有行业"。
    """
    stock = json.dumps(
        [{"code": code, "market": "ab", "type": "stock"} for code in codes],
        separators=(",", ":"),
    )
    query = urllib.parse.urlencode({"stock": stock, "finClientType": "pc"})
    payload = json.loads(
        _fetch_text(
            f"{_BAIDU_RELATED_BLOCK_URL}?{query}",
            source="baidu_related_block",
            referer=_BAIDU_REFERER,
            encoding="utf-8",
            timeout=_BAIDU_TIMEOUT_SECONDS,
            min_interval_seconds=_BAIDU_PACE_SECONDS,
        )
        or "{}"
    )
    mapping = parse_baidu_related_blocks(payload)
    if not mapping:
        raise RuntimeError(f"百度行业接口无有效结果: {list(codes)[:3]}…")
    return mapping


# ── 构建 ─────────────────────────────────────────────────────────

def _fetch_with_retry(
    fetcher: Callable[[Any], Any],
    argument: Any,
    retry: int,
    pace_seconds: float,
) -> Any | None:
    last_error: Exception | None = None
    for attempt in range(retry + 1):
        try:
            return fetcher(argument)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < retry and pace_seconds:
                time.sleep(pace_seconds)
    _ = last_error
    return None


def build_industry_map(
    *,
    boards_fetcher: BoardsFetcher,
    constituents_fetcher: ConstituentsFetcher,
    pace_seconds: float = 0.3,
    retry: int = 2,
) -> Dict[str, Any]:
    """枚举行业板块并取成分股，反向汇成 ``code -> 行业``（板块路径）。

    同一只股票若分属多个板块（行业 + 细分行业），以**首个**枚举到的板块为主行业。
    单个板块失败不致整体中断，仅记入 ``failed_units``。

    .. warning::
       默认的东财 fetcher 在生产机上不可达，本函数只在调用方注入数据源时使用；
       cron 刷新走 :func:`build_industry_map_batched`。
    """
    boards = list(boards_fetcher())
    industry_by_code: Dict[str, str] = {}
    failed_units: List[str] = []
    for board_code, industry_name in boards:
        members = _fetch_with_retry(constituents_fetcher, board_code, retry, pace_seconds)
        if members is None:
            failed_units.append(board_code)
            continue
        for raw_code in members:
            code = _norm_code(raw_code)
            if code and code not in industry_by_code:
                industry_by_code[code] = industry_name
        if pace_seconds:
            time.sleep(pace_seconds)
    return {
        "schema": SCHEMA,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "source": SOURCE,
        "mode": "boards",
        "industry_by_code": industry_by_code,
        "unit_count": len(boards),
        "stock_count": len(industry_by_code),
        "failed_units": failed_units,
    }


def build_industry_map_batched(
    *,
    universe_fetcher: UniverseFetcher,
    batch_fetcher: BatchIndustryFetcher,
    batch_size: int = _BAIDU_BATCH_SIZE,
    pace_seconds: float = 0.15,
    retry: int = 2,
    max_consecutive_failures: int = _MAX_CONSECUTIVE_FAILURES,
) -> Dict[str, Any]:
    """按批直查全市场每只股票的行业，汇成 ``code -> 行业``。

    单批失败不致整体中断，仅记入 ``failed_units``；但连续
    ``max_consecutive_failures`` 批失败即判定为系统性故障并抛出 —— 让"数据源不通"
    以错误而非作业超时的形态暴露出来。
    """
    codes = [code for code in (_norm_code(item) for item in universe_fetcher()) if code]
    if not codes:
        raise RuntimeError("全市场代码清单为空")

    industry_by_code: Dict[str, str] = {}
    failed_units: List[str] = []
    consecutive_failures = 0
    batches = [codes[start:start + batch_size] for start in range(0, len(codes), batch_size)]
    for index, batch in enumerate(batches):
        mapped = _fetch_with_retry(batch_fetcher, batch, retry, pace_seconds)
        if mapped is None:
            failed_units.append(f"batch#{index}")
            consecutive_failures += 1
            if consecutive_failures >= max_consecutive_failures:
                raise IndustrySourceDown(
                    f"行业数据源连续 {consecutive_failures} 批失败，判定为系统性故障，"
                    f"已中止（完成 {index + 1}/{len(batches)} 批）",
                    industry_by_code,
                )
            continue
        consecutive_failures = 0
        for raw_code, industry_name in mapped.items():
            code = _norm_code(raw_code)
            if code and industry_name and code not in industry_by_code:
                industry_by_code[code] = str(industry_name)
        if pace_seconds:
            time.sleep(pace_seconds)
    return {
        "schema": SCHEMA,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "source": SOURCE,
        "mode": "batched",
        "industry_by_code": industry_by_code,
        "unit_count": len(batches),
        "universe_count": len(codes),
        "stock_count": len(industry_by_code),
        "failed_units": failed_units,
    }


# ── 注入 ─────────────────────────────────────────────────────────

def enrich_records(
    records: Sequence[Mapping[str, Any]],
    industry_by_code: Mapping[str, str],
) -> List[Dict[str, Any]]:
    """把行业映射注入 universe 记录（不可变，返回新列表）。

    命中映射 → 统一采用映射口径（覆盖旧的粗行业）；未命中 → 保留原有行业。
    """
    enriched: List[Dict[str, Any]] = []
    for raw in records:
        item = dict(raw)
        mapped = industry_by_code.get(_norm_code(item.get("code")))
        if mapped:
            item["industry"] = mapped
            item["industry_source"] = SOURCE
        enriched.append(item)
    return enriched


# ── 缓存 ─────────────────────────────────────────────────────────

def _default_cache_file() -> str:
    return data_file("stock-triage", "industry_map.json")


def _resolve_pace(pace_seconds: float | None, default: float) -> float:
    if pace_seconds is not None:
        return float(pace_seconds)
    try:
        from config_registry import load_registered

        cfg = load_registered("candidate_selection").get("industry_map") or {}
        return float(cfg.get("pace_seconds", default))
    except Exception:  # noqa: BLE001
        return default


def _stale_cache_result(prior: Any, prior_map: Mapping[str, str], error: str) -> Dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": "stale_cache",
        "error": error,
        "asof": prior.get("asof") if isinstance(prior, Mapping) else None,
        "industry_by_code": dict(prior_map),
        "stock_count": len(prior_map),
    }


def fill_industry_gaps(
    mapping: Mapping[str, str],
    *,
    universe_fetcher: UniverseFetcher,
    batch_fetcher: BatchIndustryFetcher,
    pace_seconds: float = _BAIDU_PACE_SECONDS,
    retry: int = 1,
    max_batches: int = _BAIDU_MAX_BATCHES_PER_RUN,
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """给主源覆盖不到的代码逐只补行业。返回 ``(补充映射, 补口报告)``。

    补口是尽力而为：失败只记录，不影响主源结果落盘；单轮批次数封顶，补不完的
    留给下一个交易日（:func:`refresh` 与旧缓存合并，覆盖率逐日累积）。
    """
    try:
        universe = [code for code in (_norm_code(item) for item in universe_fetcher()) if code]
    except Exception as exc:  # noqa: BLE001 - 补口不可用不应拖垮主源
        return {}, {"status": "skipped", "error": str(exc), "filled": 0}

    missing = [code for code in universe if code not in mapping]
    if not missing:
        return {}, {"status": "complete", "universe_count": len(universe), "missing": 0, "filled": 0}

    budget = missing[: max_batches * _BAIDU_BATCH_SIZE]
    report: Dict[str, Any] = {
        "universe_count": len(universe),
        "missing": len(missing),
        "attempted": len(budget),
    }
    try:
        built = build_industry_map_batched(
            universe_fetcher=lambda: budget,
            batch_fetcher=batch_fetcher,
            pace_seconds=pace_seconds,
            retry=retry,
        )
    except IndustrySourceDown as exc:
        # 熔断前已经取到的部分照单收下：补口本就是尽力而为，丢掉已付出的请求
        # 只会让覆盖率恢复得更慢。
        return dict(exc.partial), {
            **report,
            "status": "aborted",
            "error": str(exc),
            "filled": len(exc.partial),
        }
    except Exception as exc:  # noqa: BLE001
        return {}, {**report, "status": "failed", "error": str(exc), "filled": 0}

    filled = dict(built["industry_by_code"])
    return filled, {
        **report,
        "status": "ok" if not built["failed_units"] else "partial",
        "failed_units": len(built["failed_units"]),
        "filled": len(filled),
    }


def _read_prior_cache(cache_file: str) -> Tuple[Any, Dict[str, str]]:
    prior = read_json(cache_file, {})
    prior_map = dict(prior.get("industry_by_code") or {}) if isinstance(prior, Mapping) else {}
    return prior, prior_map


def _coverage(gap_report: Mapping[str, Any]) -> Tuple[int | None, int | None, float | None]:
    """从补口报告推出 ``(全市场只数, 已覆盖只数, 覆盖率)``。

    覆盖率只按"全市场代码里被映射到的比例"算：主源产出里混有已退市 / 非 A 股
    代码，用映射条数当分子会把覆盖率算高。
    """
    universe_count = gap_report.get("universe_count")
    if not isinstance(universe_count, int) or not universe_count:
        return (universe_count if isinstance(universe_count, int) else None), None, None
    covered = (
        universe_count - int(gap_report.get("missing") or 0) + int(gap_report.get("filled") or 0)
    )
    return universe_count, covered, covered / universe_count


def refresh(
    asof: str,
    *,
    cache_file: str | None = None,
    boards_fetcher: BoardsFetcher | None = None,
    constituents_fetcher: ConstituentsFetcher | None = None,
    universe_fetcher: UniverseFetcher | None = None,
    batch_fetcher: BatchIndustryFetcher | None = None,
    gap_fill: bool = True,
    pace_seconds: float | None = None,
    gap_pace_seconds: float | None = None,
    retry: int = 2,
) -> Dict[str, Any]:
    """构建并落盘行业映射，与昨日缓存合并；整体失败则保留旧缓存。

    两段式：板块主源（新浪）→ 逐只补口（百度，尽力而为、有预算上限）。
    """
    cache_file = cache_file or _default_cache_file()
    prior, prior_map = _read_prior_cache(cache_file)
    try:
        built = build_industry_map(
            boards_fetcher=boards_fetcher or _default_boards_fetcher,
            constituents_fetcher=constituents_fetcher or _default_constituents_fetcher,
            pace_seconds=_resolve_pace(pace_seconds, 0.05),
            retry=retry,
        )
    except Exception as exc:  # noqa: BLE001
        return _stale_cache_result(prior, prior_map, str(exc))

    new_map = dict(built["industry_by_code"])
    if not new_map:
        return _stale_cache_result(prior, prior_map, "构建结果为空")

    gap_report: Dict[str, Any] = {"status": "disabled", "filled": 0}
    if gap_fill:
        filled, gap_report = fill_industry_gaps(
            new_map,
            universe_fetcher=universe_fetcher or _default_universe_fetcher,
            batch_fetcher=batch_fetcher or _default_batch_industry_fetcher,
            pace_seconds=(
                _BAIDU_PACE_SECONDS if gap_pace_seconds is None else float(gap_pace_seconds)
            ),
        )
        new_map = {**filled, **new_map}

    universe_count, covered, coverage_rate = _coverage(gap_report)
    if coverage_rate is not None and coverage_rate < MIN_COVERAGE_RATE:
        return _stale_cache_result(
            prior,
            prior_map,
            f"覆盖率 {coverage_rate:.1%} 低于下限 {MIN_COVERAGE_RATE:.0%}"
            f"（{covered}/{universe_count}）",
        )

    merged = {**prior_map, **new_map}
    payload = {
        "schema": SCHEMA,
        "asof": asof,
        "built_at": built["built_at"],
        "source": SOURCE,
        "status": (
            "ok"
            if not built["failed_units"] and gap_report.get("status") in ("ok", "complete")
            else "partial"
        ),
        "industry_by_code": merged,
        "unit_count": built["unit_count"],
        "universe_count": universe_count,
        "covered_count": covered,
        "coverage_rate": round(coverage_rate, 4) if coverage_rate is not None else None,
        "stock_count": len(merged),
        "fresh_stock_count": len(new_map),
        "failed_units": built["failed_units"],
        "gap_fill": gap_report,
    }
    atomic_write_json(cache_file, payload)
    return payload


def load_cached_status(
    asof: str,
    *,
    cache_file: str | None = None,
    max_age_days: int = 5,
    max_future_days: int = DEFAULT_MAX_FUTURE_DAYS,
) -> Dict[str, Any]:
    """缓存可用性诊断：``status`` ∈ ok / missing / empty / malformed / stale / future。

    消费端拿到空映射时必须能说出**为什么**空 —— 缓存文件从未生成（missing）
    和缓存过期（stale）是完全不同的故障，静默退化为「其他」会把两者一起吞掉。
    """
    cache_file = cache_file or _default_cache_file()
    diagnosis: Dict[str, Any] = {
        "cache_file": cache_file,
        "cache_asof": None,
        "age_days": None,
        "stock_count": 0,
    }
    data = read_json(cache_file, {})
    if not isinstance(data, Mapping) or not data:
        return {**diagnosis, "status": "missing", "reason": "行业映射缓存文件不存在或不可读"}
    mapping = data.get("industry_by_code")
    if not isinstance(mapping, Mapping) or not mapping:
        return {**diagnosis, "status": "empty", "reason": "行业映射缓存存在但内容为空"}

    cache_asof = str(data.get("asof"))
    diagnosis.update({"cache_asof": cache_asof, "stock_count": len(mapping)})
    try:
        age = (date.fromisoformat(asof) - date.fromisoformat(cache_asof)).days
    except (TypeError, ValueError):
        return {**diagnosis, "status": "malformed", "reason": f"缓存 asof 不可解析: {cache_asof!r}"}

    diagnosis["age_days"] = age
    if age > max_age_days:
        return {**diagnosis, "status": "stale", "reason": f"缓存已过期 {age} 天（上限 {max_age_days}）"}
    # asof 早于缓存 asof（回补历史日期）：行业归属是慢变量，用一份稍晚构建的映射
    # 远好过零覆盖，因此在 max_future_days 内放行，仅在诊断里留痕。
    if age < -max_future_days:
        return {
            **diagnosis,
            "status": "future",
            "reason": f"缓存比查询日期晚 {-age} 天（上限 {max_future_days}）",
        }
    return {**diagnosis, "status": "ok", "reason": ""}


def load_cached(
    asof: str,
    *,
    cache_file: str | None = None,
    max_age_days: int = 5,
    max_future_days: int = DEFAULT_MAX_FUTURE_DAYS,
) -> Dict[str, str]:
    """读取按日缓存的 ``code -> 行业``；不可用返回空（注入退化为 no-op）。

    需要知道"为什么为空"时用 :func:`load_cached_status`。
    """
    cache_file = cache_file or _default_cache_file()
    status = load_cached_status(
        asof,
        cache_file=cache_file,
        max_age_days=max_age_days,
        max_future_days=max_future_days,
    )
    if status["status"] != "ok":
        return {}
    data = read_json(cache_file, {})
    mapping = data.get("industry_by_code") if isinstance(data, Mapping) else {}
    return {str(code): str(name) for code, name in (mapping or {}).items()}


# ── CLI ──────────────────────────────────────────────────────────

_OK_STATUSES = frozenset({"ok", "partial", "cached"})


def main() -> int:
    """返回进程退出码：0 = 缓存可用，1 = 刷新失败 / 缓存不可用。

    刷新失败必须以非零退出码收场，否则 cron 会把 ``stale_cache``（没写缓存、
    下游全线退化）记成一次成功的运行。
    """
    import argparse
    import contextlib
    import sys

    parser = argparse.ArgumentParser(description="全市场行业映射刷新")
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--refresh", action="store_true", help="构建并刷新缓存")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.refresh:
        # 数据源库（adata 等）会往 stdout 打诊断行，而作业契约要求 stdout 是纯
        # JSON，因此把构建期的 stdout 整体改道到 stderr。
        with contextlib.redirect_stdout(sys.stderr):
            result = refresh(args.asof)
    else:
        status = load_cached_status(args.asof)
        result = {
            "schema": SCHEMA,
            "asof": args.asof,
            "status": "cached" if status["status"] == "ok" else status["status"],
            "reason": status["reason"],
            "cache_asof": status["cache_asof"],
            "stock_count": status["stock_count"],
        }

    summary = {key: value for key, value in result.items() if key != "industry_by_code"}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(
            f"行业映射 {args.asof}: status={summary.get('status')} "
            f"覆盖={summary.get('stock_count')} "
            f"失败单元={len(summary.get('failed_units') or [])}"
        )
    return 0 if summary.get("status") in _OK_STATUSES else 1


if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    raise SystemExit(main())
