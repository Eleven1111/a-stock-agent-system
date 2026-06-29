"""Sector/theme resolution helpers for A-share candidate selection.

`industry` is a stable classification field. `sector` is a tradable theme or
narrow board used for mainline selection. Coarse exchange industry labels must
not be promoted into `sector`, because they make broad buckets look like
actionable market themes.
"""

from __future__ import annotations

from typing import Any, Mapping


BROAD_SECTOR_LABELS = {
    "A 农、林、牧、渔业",
    "B 采矿业",
    "C 制造业",
    "D 电力、热力、燃气及水生产和供应业",
    "E 建筑业",
    "F 批发和零售业",
    "G 交通运输、仓储和邮政业",
    "H 住宿和餐饮业",
    "I 信息传输、软件和信息技术服务业",
    "I 信息技术",
    "J 金融业",
    "K 房地产业",
    "L 租赁和商务服务业",
    "M 科学研究和技术服务业",
    "N 水利、环境和公共设施管理业",
    "O 居民服务、修理和其他服务业",
    "P 教育",
    "Q 卫生和社会工作",
    "R 文化、体育和娱乐业",
    "S 综合",
    "农、林、牧、渔业",
    "采矿业",
    "制造业",
    "电力、热力、燃气及水生产和供应业",
    "建筑业",
    "批发和零售业",
    "交通运输、仓储和邮政业",
    "住宿和餐饮业",
    "信息传输、软件和信息技术服务业",
    "金融业",
    "房地产业",
    "租赁和商务服务业",
    "科学研究和技术服务业",
    "水利、环境和公共设施管理业",
    "居民服务、修理和其他服务业",
    "教育",
    "卫生和社会工作",
    "文化、体育和娱乐业",
    "综合",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def is_broad_sector_label(value: Any) -> bool:
    text = _text(value)
    if not text:
        return False
    if text in BROAD_SECTOR_LABELS:
        return True
    return len(text) >= 3 and text[1:2] == " " and text[:1].isalpha()


def resolve_sector(
    record: Mapping[str, Any],
    *,
    ladder: Mapping[str, Any] | None = None,
    social: Mapping[str, Any] | None = None,
) -> tuple[str, str | None]:
    """Resolve the tradable sector/theme and its source.

    Explicit theme-like fields win. Industry can be used only when it is narrow
    enough; broad exchange labels remain `industry` and return no sector.
    """
    for key, source in (
        ("sector", "explicit_sector"),
        ("theme", "explicit_theme"),
        ("concept", "explicit_concept"),
        ("topic", "explicit_topic"),
    ):
        value = _text(record.get(key))
        if value and not is_broad_sector_label(value):
            return value, source

    ladder_sector = _text((ladder or {}).get("sector"))
    if ladder_sector and not is_broad_sector_label(ladder_sector):
        return ladder_sector, "lianban_ladder"

    social_record = social or {}
    for key, source in (
        ("sector", "social_sector"),
        ("theme", "social_theme"),
        ("concept", "social_concept"),
        ("industry", "social_industry"),
    ):
        value = _text(social_record.get(key))
        if value and not is_broad_sector_label(value):
            return value, source

    industry = _text(record.get("industry"))
    if industry and not is_broad_sector_label(industry):
        return industry, str(record.get("industry_source") or "industry")

    return "", None
