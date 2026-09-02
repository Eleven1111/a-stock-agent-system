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
   约 20s）。直连可达、无风控，实测单独覆盖全市场约 63%。
2. **补口 = adata ``get_industry_sw`` 逐只直查**，只补主源覆盖不到的代码，
   **按批次预算封顶**。直接返回申万一级（与 ``sw_to_group`` 同源），
   2026-08-05 实测：200 只抽样零失败；全量回补 2165 只同样 **0 失败**，
   总耗时 847s，覆盖率 **62.6% → 94.3%**。残余约 190 只确实没有申万一级数据
   （新上市/北交所/退市），换任何源都补不到，属不可约残差。

   补口原为百度股市通 ``getrelatedblock``，但它在高频下返回 ``ResultCode=403``
   且封禁窗口以小时计——实跑 107 批只跑通 10 批即熔断、``filled=0``。
   函数保留为 :func:`_baidu_batch_industry_fetcher`，不再是默认。

主源覆盖是**结构性偏斜**的，不是随机缺失：2026-08-05 实测缺口 1779 只中
创业板占 944（该板缺 67%）、科创板 423（缺 69%），而沪深主板只缺 6.6%/20%。
缺的正是科技成长股集中处——抽样 200 只里 58% 属申万「电子/医药生物/电力设备/
计算机」。因此补口不是锦上添花：不补，「科技」组会被系统性低估。

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
import os
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

# 申万补口（现默认）：adata get_industry_sw 逐只直查，直接给申万一级 —— 与
# sw_to_group 的报告口径同源，不需要任何名称映射。2026-08-05 实测 200 只
# 零失败、中位 0.20s、总耗时 58s；约 12% 的代码没有申万一级数据（新上市/
# 退市/北交所），属正常空值，不是限流。
# 批大小取 1：``get_industry_sw`` 本就是逐只接口，让每只股票各成一批，
# 单只失败/重试/连续失败熔断全部复用 build_industry_map_batched 既有的批级
# 容错，fetcher 自身不需要再吞异常（也就不引入新的宽泛 except）。
_SW_BATCH_SIZE = 1
_SW_PACE_SECONDS = 0.05
# 单轮补口预算按**只**计（不按批），批大小一变语义就漂。
# 1779 只缺口 × 实测 0.29s ≈ 8.6 分钟，加主源 3 分钟仍在作业 1200s 预算内。
_SW_MAX_CODES_PER_RUN = 2000

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


# ── 新浪板块脏数据校正表 ──────────────────────────────────────────

# 新浪行业板块的成分股列表存在历史脏点：000034 神州数码（IT 分销/云服务）
# 被错误收进「煤炭行业」(new_mthy) 板块 —— 2026-08-17 实测同板块还混有
# 安通控股/百花医药/退市游久等非煤股；而新浪自身的电子信息/电子器件板块
# 都没有收录 000034，管线「首个枚举到的板块为主行业」于是把它标成煤炭行业。
# 真实归属：东财行业板块 = IT服务Ⅱ、东财 EM2016 = 信息技术-计算机软件、
# 证监会行业 = 批发业（IT 分销本质是批发贸易）。
# 该表优先级高于任何数据源结果；新增条目必须附验证依据，禁止随手加。
INDUSTRY_OVERRIDES: Dict[str, str] = {
    "000034": "电子信息",
}


def apply_industry_overrides(mapping: Mapping[str, str]) -> Dict[str, str]:
    """把校正表应用到 ``code -> 行业`` 映射（不修改入参）。

    只修正**已存在**的归属（替换错值），不新增键 —— 校正表是纠错不是补全，
    避免把不在映射里的代码塞进去虚增覆盖率与条数。
    """
    result: Dict[str, str] = {}
    for code, name in mapping.items():
        norm = _norm_code(code)
        if norm:
            result[norm] = str(name)
    for raw_code, industry in INDUSTRY_OVERRIDES.items():
        norm = _norm_code(raw_code)
        if norm in result:
            result[norm] = industry
    return result


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
    """一批股票 → ``code -> 申万一级行业``（adata ``get_industry_sw``，逐只直查）。

    相比百度补口的两点优势：直接返回**申万一级**（与 ``sw_to_group`` 同源，
    省掉一层名称映射），且实测无限流——百度那条在高频下返回 ``ResultCode=403``，
    实跑 107 批里只跑通 10 批就熔断。

    单只取不到申万一级（新上市/退市/北交所，实测约 12%）是**正常空值**：
    返回空 mapping 而非抛错，不会被误判成限流。异常一律向上抛，由
    :func:`build_industry_map_batched` 的批级重试与连续失败熔断处理——
    ``_SW_BATCH_SIZE = 1`` 使「批」即「只」，于是熔断阈值就是「连续 N 只失败」。
    """
    import adata  # 惰性导入

    mapping: Dict[str, str] = {}
    for code in codes:
        frame = adata.stock.info.get_industry_sw(stock_code=code)
        if frame is None or len(frame) == 0:
            continue
        rows = frame[frame["industry_name"].notna()]
        level1 = rows[rows["industry_type"] == "申万一级"]
        if len(level1):
            mapping[_norm_code(code)] = str(level1["industry_name"].iloc[0]).strip()
    return mapping


def _baidu_batch_industry_fetcher(codes: Sequence[str]) -> Dict[str, str]:
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
    pace_seconds: float = _SW_PACE_SECONDS,
    retry: int = 1,
    max_codes: int = _SW_MAX_CODES_PER_RUN,
    batch_size: int = _SW_BATCH_SIZE,
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """给主源覆盖不到的代码逐只补行业。返回 ``(补充映射, 补口报告)``。

    补口是尽力而为：失败只记录，不影响主源结果落盘；单轮批次数封顶，补不完的
    留给下一个交易日（:func:`refresh` 与旧缓存合并）。

    注意：「逐日累积必然收敛」只在补口数据源可用时成立。2026-08-05 用百度源
    实跑时该假设不成立——接口 403 熔断，``filled=0``，覆盖率连续两日不升反降。
    现默认改用申万源（无限流），但报告里的 ``status``/``filled`` 仍必须被消费方
    如实看待，不能默认「明天就好了」。
    """
    try:
        universe = [code for code in (_norm_code(item) for item in universe_fetcher()) if code]
    except Exception as exc:  # noqa: BLE001 - 补口不可用不应拖垮主源
        return {}, {"status": "skipped", "error": str(exc), "filled": 0}

    missing = [code for code in universe if code not in mapping]
    if not missing:
        return {}, {"status": "complete", "universe_count": len(universe), "missing": 0, "filled": 0}

    budget = missing[:max_codes]
    report: Dict[str, Any] = {
        "universe_count": len(universe),
        "missing": len(missing),
        "attempted": len(budget),
    }
    try:
        built = build_industry_map_batched(
            universe_fetcher=lambda: budget,
            batch_fetcher=batch_fetcher,
            batch_size=batch_size,
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


# ── 按交易日的归属变更日志（PIT 前提） ───────────────────────────
#
# ``industry_map.json`` 是「最新归属向前滚」的单文件快照：每次 refresh 把
# ``{**prior_map, **new_map}`` 覆盖写回去，因此**没有任何历史**。用它回放历史
# 决策，等于把今天的行业归属倒灌进过去 —— 这是行业轮动研究里最大的一处幸存者
# 偏差（换股、行业调整、新上市全部被抹平）。
#
# 这里加一份 append-only 的变更日志，形状是 SCD Type-2 的最小版本：
#
# - 只有**当日真实观测到**的代码才会产生事件。``refresh`` 会把昨日缓存合并进来
#   补主源没覆盖到的部分，那些是**沿用**不是观测；把沿用记成事件，会让一次数据源
#   抖动看起来像一次行业重分类。
# - 历史从落盘那天开始。查询早于 ``history_start`` 的日期一律返回
#   ``before_history``，**绝不**拿首日快照往前铺 —— 那正好是本模块要消灭的偏差。
HISTORY_SCHEMA = "industry_map_history_v1"


def _default_history_file(cache_file: str) -> str:
    directory = os.path.dirname(cache_file) or "."
    return os.path.join(directory, "industry_map_history.jsonl")


def _read_history_events(history_file: str) -> List[Dict[str, Any]]:
    if not os.path.isfile(history_file):
        return []
    events: List[Dict[str, Any]] = []
    with open(history_file, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # 半行（写入时被打断）不该让整份历史不可读，但也绝不猜它的内容。
                continue
            if isinstance(record, dict) and record.get("code") and record.get("asof"):
                events.append(record)
    return events


def _latest_recorded(events: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    """事件流 → ``code -> 最近一次记录的行业``（按出现顺序，后写覆盖先写）。"""
    latest: Dict[str, str] = {}
    for record in events:
        latest[str(record["code"])] = str(record.get("industry") or "")
    return latest


def record_history(
    asof: str,
    observed: Mapping[str, str],
    *,
    history_file: str,
) -> Dict[str, Any]:
    """把当日**观测到**的归属追加成变更事件；无变化则不写。

    ``observed`` 只能是当日真实取到的映射，不能包含从昨日缓存沿用的条目。
    """
    events = _read_history_events(history_file)
    latest = _latest_recorded(events)
    bootstrap = not events

    appended: List[Dict[str, Any]] = []
    for code, industry in sorted(observed.items()):
        code, industry = str(code), str(industry)
        if not code or not industry:
            continue
        previous = latest.get(code)
        if previous == industry:
            continue
        appended.append(
            {
                "schema": HISTORY_SCHEMA,
                "asof": asof,
                "code": code,
                "industry": industry,
                "previous": previous,
            }
        )

    if appended:
        os.makedirs(os.path.dirname(history_file) or ".", exist_ok=True)
        with open(history_file, "a", encoding="utf-8") as handle:
            for record in appended:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    return {
        "schema": HISTORY_SCHEMA,
        "asof": asof,
        "history_file": history_file,
        "bootstrap": bootstrap,
        "appended": len(appended),
        "observed": len(observed),
        "reclassified": sum(1 for record in appended if record["previous"]),
    }


def history_asof(asof: str, *, history_file: str) -> Dict[str, Any]:
    """还原 ``asof`` 当日的 ``code -> 行业``（PIT 查询）。

    ``status``：``ok`` / ``before_history``（早于历史起点）/ ``missing``（无历史）。
    早于起点时返回空映射 —— 用今天的归属回答那天的问题是错的，宁可不可用。
    """
    events = _read_history_events(history_file)
    if not events:
        return {
            "schema": HISTORY_SCHEMA,
            "status": "missing",
            "asof": asof,
            "history_start": None,
            "industry_by_code": {},
            "reason": "归属变更日志尚未生成",
        }

    history_start = min(str(record["asof"]) for record in events)
    if asof < history_start:
        return {
            "schema": HISTORY_SCHEMA,
            "status": "before_history",
            "asof": asof,
            "history_start": history_start,
            "industry_by_code": {},
            "reason": f"历史自 {history_start} 起，更早的归属没有被观测过",
        }

    mapping: Dict[str, str] = {}
    for record in events:
        if str(record["asof"]) <= asof:
            mapping[str(record["code"])] = str(record.get("industry") or "")
    return {
        "schema": HISTORY_SCHEMA,
        "status": "ok",
        "asof": asof,
        "history_start": history_start,
        "industry_by_code": apply_industry_overrides(mapping),
        "reason": "",
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
    history_file: str | None = None,
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

    两段式：板块主源（新浪）→ 逐只补口（申万，尽力而为、有预算上限）。
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
                _SW_PACE_SECONDS if gap_pace_seconds is None else float(gap_pace_seconds)
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

    payload = _build_refresh_payload(
        asof,
        built=built,
        prior_map=prior_map,
        new_map=new_map,
        gap_report=gap_report,
        coverage=(universe_count, covered, coverage_rate),
        history_file=history_file or _default_history_file(cache_file),
    )
    atomic_write_json(cache_file, payload)
    return payload


def _build_refresh_payload(
    asof: str,
    *,
    built: Mapping[str, Any],
    prior_map: Mapping[str, str],
    new_map: Mapping[str, str],
    gap_report: Mapping[str, Any],
    coverage: Tuple[int | None, int | None, float | None],
    history_file: str,
) -> Dict[str, Any]:
    """合并当日观测与昨日缓存，落一条历史事件，产出缓存载荷。"""
    universe_count, covered, coverage_rate = coverage
    merged = apply_industry_overrides({**prior_map, **new_map})
    # 只把当日**观测到**的部分写进历史；prior_map 是沿用，不是观测。
    history = record_history(
        asof,
        apply_industry_overrides(dict(new_map)),
        history_file=history_file,
    )
    return {
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
        "history": history,
    }


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
    # 读取时再兜底一遍校正表：即使缓存文件里已是脏值（旧版构建落盘的），
    # 消费端拿到的也一定是修正后的归属，不必等下一个交易日的刷新。
    return apply_industry_overrides(
        {str(code): str(name) for code, name in (mapping or {}).items()}
    )


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
    parser.add_argument(
        "--history",
        action="store_true",
        help="按 --asof 做 PIT 查询：还原当日归属，而不是读最新快照",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.history:
        result = history_asof(
            args.asof, history_file=_default_history_file(_default_cache_file())
        )
        result = {
            **{key: value for key, value in result.items() if key != "industry_by_code"},
            "stock_count": len(result["industry_by_code"]),
        }
    elif args.refresh:
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
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    raise SystemExit(main())
