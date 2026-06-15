"""Urllib-based adapters for independent A-share social attention sources."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlencode

from data_access_config import provider_settings, social_attention_settings
from http_client import DataSourceError, ErrorType, HttpClient, build_request


EASTMONEY_URL = "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"
EASTMONEY_RISING_URL = "https://emappdata.eastmoney.com/stockrank/getAllHisRcList"
XUEQIU_URL = "https://xueqiu.com/service/v5/stock/screener/screen"
BAIDU_URL = "https://finance.pae.baidu.com/selfselect/listsugrecomm"
USER_AGENT = "Mozilla/5.0 (A-Stock-Agent; social-attention-v1)"


def _invalid(source: str, message: str) -> DataSourceError:
    return DataSourceError(
        source,
        message,
        error_type=ErrorType.INVALID_RESPONSE,
    )


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _client(source: str, settings_name: str) -> HttpClient:
    settings = provider_settings(settings_name)
    return HttpClient(
        source,
        timeout=float(settings.get("timeout_seconds", 10)),
        max_attempts=int(settings.get("max_attempts", 2)),
    )


def parse_eastmoney_rank_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        raise _invalid("eastmoney_attention", "response root must be an object")
    if payload.get("status") not in (0, "0") or payload.get("code") not in (0, "0"):
        raise _invalid(
            "eastmoney_attention",
            str(payload.get("message") or "business status failure"),
        )
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise _invalid("eastmoney_attention", "data must be a list")
    result = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise _invalid("eastmoney_attention", "data contains a non-object row")
        code = str(row.get("sc") or "").upper()
        rank = int(_number(row.get("rk")))
        if (
            not code.startswith(("SH", "SZ"))
            or code[2:3] not in {"0", "3", "6"}
            or rank <= 0
        ):
            continue
        result.append({
            "code": code,
            "name": None,
            "rank": rank,
            "rank_change": _number(row.get("hisRc", row.get("hrc"))),
        })
    if not result:
        raise _invalid("eastmoney_attention", "no valid ranking rows")
    return result


def parse_eastmoney_rising_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        raise _invalid("eastmoney_attention", "response root must be an object")
    if payload.get("status") not in (0, "0") or payload.get("code") not in (0, "0"):
        raise _invalid(
            "eastmoney_attention",
            str(payload.get("message") or "business status failure"),
        )
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise _invalid("eastmoney_attention", "data must be a list")
    result = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise _invalid("eastmoney_attention", "data contains a non-object row")
        code = str(row.get("sc") or "").upper()
        rising_rank = int(_number(row.get("hrcrk")))
        if (
            not code.startswith(("SH", "SZ"))
            or code[2:3] not in {"0", "3", "6"}
            or rising_rank <= 0
        ):
            continue
        result.append({
            "code": code,
            "name": None,
            "rank": rising_rank,
            "rank_change": _number(row.get("hrc")),
            "current_rank": int(_number(row.get("rk"))),
        })
    if not result:
        raise _invalid("eastmoney_attention", "no valid rising ranking rows")
    return result


def fetch_eastmoney_rankings(
    *,
    limit: int | None = None,
    client: HttpClient | None = None,
) -> dict[str, list[dict[str, Any]]]:
    size = min(100, int(limit or social_attention_settings()["top_limit"]))
    payload = {
        "appId": "appId01",
        "globalId": "786e4c21-70dc-435a-93bb-38",
        "marketType": "",
        "pageNo": 1,
        "pageSize": size,
    }
    active_client = client or _client("eastmoney_attention", "eastmoney")

    def _post(url: str) -> Any:
        request = build_request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        return active_client.request_json(request).data

    return {
        "eastmoney": parse_eastmoney_rank_payload(_post(EASTMONEY_URL))[:size],
        "eastmoney_rising": parse_eastmoney_rising_payload(
            _post(EASTMONEY_RISING_URL)
        )[:size],
    }


def parse_xueqiu_rank_payload(
    payload: Any,
    *,
    metric: str,
) -> list[dict[str, Any]]:
    if metric not in {"tweet7d", "follow7d"}:
        raise ValueError(f"unsupported Xueqiu metric: {metric}")
    if not isinstance(payload, Mapping) or payload.get("error_code") not in (0, "0"):
        raise _invalid(
            "xueqiu_attention",
            str(
                payload.get("error_description")
                if isinstance(payload, Mapping)
                else "response root must be an object"
            ),
        )
    data = payload.get("data")
    rows = data.get("list") if isinstance(data, Mapping) else None
    if not isinstance(rows, list):
        raise _invalid("xueqiu_attention", "data.list must be a list")
    result = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, Mapping):
            raise _invalid("xueqiu_attention", "data.list contains a non-object row")
        code = str(row.get("symbol") or "").upper()
        if (
            not code.startswith(("SH", "SZ"))
            or code[2:3] not in {"0", "3", "6"}
        ):
            continue
        result.append({
            "code": code,
            "name": row.get("name"),
            "rank": index,
            "metric_value": _number(row.get(metric)),
            "price_change_pct": (
                _number(row.get("pct"))
                if row.get("pct") is not None
                else None
            ),
        })
    if not result:
        raise _invalid("xueqiu_attention", f"no valid {metric} ranking rows")
    return result


def _fetch_xueqiu_metric(
    metric: str,
    *,
    limit: int,
    client: HttpClient,
) -> list[dict[str, Any]]:
    params = urlencode({
        "category": "CN",
        "size": min(200, limit),
        "order": "desc",
        "order_by": metric,
        "only_count": "0",
        "page": "1",
    })
    request = build_request(
        f"{XUEQIU_URL}?{params}",
        headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://xueqiu.com/hq",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    response = client.request_json(request)
    return parse_xueqiu_rank_payload(response.data, metric=metric)[:limit]


def fetch_xueqiu_rankings(
    *,
    limit: int | None = None,
    client: HttpClient | None = None,
) -> dict[str, list[dict[str, Any]]]:
    size = min(200, int(limit or social_attention_settings()["top_limit"]))
    active_client = client or _client("xueqiu_attention", "xueqiu")
    return {
        "xueqiu_discussion": _fetch_xueqiu_metric(
            "tweet7d",
            limit=size,
            client=active_client,
        ),
        "xueqiu_follow": _fetch_xueqiu_metric(
            "follow7d",
            limit=size,
            client=active_client,
        ),
    }


def parse_baidu_hot_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        raise _invalid("baidu_attention", "response root must be an object")
    if str(payload.get("ResultCode") or "0") != "0":
        raise _invalid(
            "baidu_attention",
            f"ResultCode={payload.get('ResultCode')}",
        )
    result = payload.get("Result")
    listing = result.get("list") if isinstance(result, Mapping) else None
    rows = listing.get("body") if isinstance(listing, Mapping) else None
    if not isinstance(rows, list):
        raise _invalid("baidu_attention", "Result.list.body must be a list")
    normalized = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, Mapping):
            continue
        label = str(row.get("name") or "")
        code_match = re.search(r"(?<!\d)(\d{6})(?!\d)", label)
        if not code_match:
            continue
        code = code_match.group(1)
        if code[:1] not in {"0", "3", "6"}:
            continue
        normalized.append({
            "code": ("SH" if code.startswith("6") else "SZ") + code,
            "name": label.splitlines()[0].strip() or None,
            "rank": index,
            "metric_value": _number(row.get("heat")),
            "price_change_pct": (
                _number(row.get("pxChangeRate"))
                if row.get("pxChangeRate") is not None
                else None
            ),
        })
    if not normalized:
        raise _invalid("baidu_attention", "no valid A-share hot-search rows")
    return normalized


def fetch_baidu_hot_rankings(
    *,
    asof: str | None = None,
    client: HttpClient | None = None,
) -> list[dict[str, Any]]:
    day = (asof or date.today().isoformat()).replace("-", "")
    params = urlencode({
        "bizType": "wisexmlnew",
        "dsp": "iphone",
        "product": "search",
        "style": "tablelist",
        "market": "ab",
        "type": "今日",
        "day": day,
        "hour": datetime.now().hour,
        "pn": "0",
        "rn": "12",
        "finClientType": "pc",
    })
    response = (
        client or _client("baidu_attention", "baidu_attention")
    ).request_json(build_request(f"{BAIDU_URL}?{params}", headers={"User-Agent": USER_AGENT}))
    return parse_baidu_hot_payload(response.data)


def _health_ok(count: int) -> dict[str, Any]:
    return {
        "status": "ok",
        "record_count": count,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _health_failed(error: Exception) -> dict[str, Any]:
    detail = (
        error.to_dict()
        if isinstance(error, DataSourceError)
        else {"error": str(error), "error_type": "unknown"}
    )
    return {
        "status": "failed",
        **detail,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def collect_social_rankings(
    *,
    eastmoney_fetcher: Callable[
        [],
        Sequence[Mapping[str, Any]]
        | Mapping[str, Sequence[Mapping[str, Any]]],
    ] = fetch_eastmoney_rankings,
    xueqiu_fetcher: Callable[
        [], Mapping[str, Sequence[Mapping[str, Any]]]
    ] = fetch_xueqiu_rankings,
    baidu_fetcher: Callable[[], Sequence[Mapping[str, Any]]] = fetch_baidu_hot_rankings,
    baidu_enabled: bool | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Collect every source independently so one schema drift cannot erase others."""
    rankings: dict[str, list[dict[str, Any]]] = {}
    health: dict[str, dict[str, Any]] = {}
    try:
        eastmoney = eastmoney_fetcher()
        if isinstance(eastmoney, Mapping):
            count = 0
            for key in ("eastmoney", "eastmoney_rising"):
                rows = [dict(row) for row in eastmoney.get(key, [])]
                rankings[key] = rows
                count += len(rows)
        else:
            rows = [dict(row) for row in eastmoney]
            rankings["eastmoney"] = rows
            count = len(rows)
        health["eastmoney"] = _health_ok(count)
    except Exception as exc:  # noqa: BLE001
        health["eastmoney"] = _health_failed(exc)

    try:
        groups = xueqiu_fetcher()
        for key in ("xueqiu_discussion", "xueqiu_follow"):
            rankings[key] = [dict(row) for row in groups.get(key, [])]
        health["xueqiu"] = _health_ok(
            len(rankings["xueqiu_discussion"]) + len(rankings["xueqiu_follow"])
        )
    except Exception as exc:  # noqa: BLE001
        health["xueqiu"] = _health_failed(exc)

    enabled = (
        social_attention_settings()["baidu_enabled"]
        if baidu_enabled is None
        else baidu_enabled
    )
    if enabled:
        try:
            rows = [dict(row) for row in baidu_fetcher()]
            rankings["baidu"] = rows
            health["baidu"] = _health_ok(len(rows))
        except Exception as exc:  # noqa: BLE001
            health["baidu"] = _health_failed(exc)
    else:
        health["baidu"] = {"status": "disabled"}
    return rankings, health
