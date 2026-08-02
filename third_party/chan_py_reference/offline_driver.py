"""Offline driver for the chan.py reference oracle.

Feeds a synthetic K-line sequence directly into ``CChan`` via
``trigger_load()``, bypassing every network data source. Test-only:
see README.md for provenance, scope, and the local patch list. This
module must never be imported from ``skills/`` or ``scripts/`` — the
guard test in ``tests/test_chan_reference_guard.py`` enforces that.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

_REFERENCE_ROOT = Path(__file__).resolve().parent


def _ensure_on_syspath() -> None:
    """chan.py uses absolute-style imports (``from Bi.Bi import ...``),
    so its package root must be on sys.path before any of its modules
    are imported."""
    root = str(_REFERENCE_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


_ensure_on_syspath()

from Chan import CChan  # noqa: E402
from ChanConfig import CChanConfig  # noqa: E402
from Common.CEnum import DATA_FIELD, KL_TYPE  # noqa: E402
from Common.CTime import CTime  # noqa: E402
from KLine.KLine_Unit import CKLine_Unit  # noqa: E402


@dataclass(frozen=True)
class SyntheticBar:
    """One synthetic daily OHLC bar."""

    date: str  # "YYYY-MM-DD"
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class BiRecord:
    """Snapshot of one chan.py 笔 (Bi)."""

    dir: str
    begin: str
    end: str
    is_sure: bool


@dataclass(frozen=True)
class BspRecord:
    """Snapshot of one chan.py 买卖点 (buy/sell point)."""

    bi_idx: int
    is_buy: bool
    types: tuple[str, ...]
    time: str
    is_sure: bool = True  # 锚定笔的确定态（CBi.is_sure）
    features: dict[str, float | None] = field(default_factory=dict)


@dataclass(frozen=True)
class SegRecord:
    """Snapshot of one chan.py 线段 (Seg)."""

    dir: str
    begin: str
    end: str
    is_sure: bool
    start_bi_idx: int
    end_bi_idx: int
    reason: str


@dataclass(frozen=True)
class ZsRecord:
    """Snapshot of one chan.py 中枢 (ZS)."""

    zg: float  # CZS.high — min of the member 笔 highs
    zd: float  # CZS.low  — max of the member 笔 lows
    begin: str
    end: str
    is_sure: bool
    start_bi_idx: int
    end_bi_idx: int


@dataclass(frozen=True)
class OfflineResult:
    """All structure snapshots from one offline run."""

    bi_records: list[BiRecord]
    bsp_records: list[BspRecord]
    seg_records: list[SegRecord]
    zs_records: list[ZsRecord]


def _to_klu(bar: SyntheticBar) -> CKLine_Unit:
    year, month, day = (int(part) for part in bar.date.split("-"))
    kl_dict = {
        DATA_FIELD.FIELD_TIME: CTime(year, month, day, 0, 0),
        DATA_FIELD.FIELD_OPEN: bar.open,
        DATA_FIELD.FIELD_HIGH: bar.high,
        DATA_FIELD.FIELD_LOW: bar.low,
        DATA_FIELD.FIELD_CLOSE: bar.close,
    }
    return CKLine_Unit(kl_dict)


def run_offline(
    bars: Sequence[SyntheticBar],
    overrides: dict | None = None,
) -> tuple[list[BiRecord], list[BspRecord]]:
    """Run chan.py structure analysis on synthetic daily bars, fully offline.

    ``overrides`` is merged into the CChanConfig dict so differential tests
    can exercise non-default 笔 settings (bi_fx_check, bi_strict, gap_as_kl,
    bi_allow_sub_peak). ``trigger_step`` stays forced on: it is what keeps
    CChan.__init__ off the network data-source path.

    Returns (bi_records, bsp_records) as plain, immutable dataclasses so
    callers never touch chan.py's mutable OOP graph directly. Use
    ``run_offline_structure`` when 线段 records are needed too; this
    two-tuple signature is kept for existing callers.
    """
    result = run_offline_structure(bars, overrides)
    return result.bi_records, result.bsp_records


def run_offline_structure(
    bars: Sequence[SyntheticBar],
    overrides: dict | None = None,
) -> OfflineResult:
    """Same offline run as ``run_offline``, also exporting 线段 (seg) and 中枢 (zs) records."""
    if len(bars) < 2:
        raise ValueError("run_offline requires at least 2 bars")

    config = CChanConfig({**(overrides or {}), "trigger_step": True})
    chan = CChan(
        code="SYNTH",
        lv_list=[KL_TYPE.K_DAY],
        config=config,
    )
    klu_list = [_to_klu(bar) for bar in bars]
    chan.trigger_load({KL_TYPE.K_DAY: klu_list})

    kl_list = chan[0]
    bi_records = [
        BiRecord(
            dir=bi.dir.name,
            begin=str(bi.get_begin_klu().time),
            end=str(bi.get_end_klu().time),
            is_sure=bi.is_sure,
        )
        for bi in kl_list.bi_list
    ]
    bsp_records = [
        BspRecord(
            bi_idx=bsp.bi.idx,
            is_buy=bsp.is_buy,
            types=tuple(t.value for t in bsp.type),
            time=str(bsp.klu.time),
            is_sure=bsp.bi.is_sure,
            features=dict(bsp.features.items()),
        )
        for bsp in chan.get_bsp()
    ]
    seg_records = [
        SegRecord(
            dir=seg.dir.name,
            begin=str(seg.get_begin_klu().time),
            end=str(seg.get_end_klu().time),
            is_sure=seg.is_sure,
            start_bi_idx=seg.start_bi.idx,
            end_bi_idx=seg.end_bi.idx,
            reason=seg.reason,
        )
        for seg in kl_list.seg_list
    ]
    zs_records = [
        ZsRecord(
            zg=zs.high,
            zd=zs.low,
            begin=str(zs.begin.time),
            end=str(zs.end.time),
            is_sure=zs.is_sure,
            start_bi_idx=zs.begin_bi.idx,
            end_bi_idx=zs.end_bi.idx,
        )
        for zs in kl_list.zs_list
    ]
    return OfflineResult(bi_records, bsp_records, seg_records, zs_records)
