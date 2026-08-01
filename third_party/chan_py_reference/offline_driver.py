"""Offline driver for the chan.py reference oracle.

Feeds a synthetic K-line sequence directly into ``CChan`` via
``trigger_load()``, bypassing every network data source. Test-only:
see README.md for provenance, scope, and the local patch list. This
module must never be imported from ``skills/`` or ``scripts/`` — the
guard test in ``tests/test_chan_reference_guard.py`` enforces that.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
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
    callers never touch chan.py's mutable OOP graph directly.
    """
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
        )
        for bsp in chan.get_bsp()
    ]
    return bi_records, bsp_records
