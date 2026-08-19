"""Local SQLite cache for historical A-share daily bars."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence


_COLUMNS = (
    "code",
    "trading_date",
    "adjust_flag",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "amount",
    "turn",
    "pct_chg",
    "source",
    "source_version",
    "updated_at",
)
_VALUE_COLUMNS = _COLUMNS[3:]


def _state_home() -> Path:
    return Path(os.environ.get("A_STOCK_STATE_HOME", "~/.hermes")).expanduser()


def _database_path() -> Path:
    return _state_home() / "market" / "history.sqlite3"


def _connect() -> sqlite3.Connection:
    path = _database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def ensure_schema() -> None:
    """Create the history table if it does not exist."""
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_bars (
                code TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                adjust_flag TEXT NOT NULL DEFAULT 'qfq',
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                preclose REAL,
                volume REAL,
                amount REAL,
                turn REAL,
                pct_chg REAL,
                source TEXT,
                source_version TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (code, trading_date, adjust_flag)
            )
            """
        )


def upsert_daily_bars(rows: Sequence[Mapping[str, Any]]) -> int:
    """Atomically insert or replace bars, returning the number of input rows."""
    rows = list(rows)
    if not rows:
        return 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    values = []
    for row in rows:
        code = row.get("code")
        trading_date = row.get("trading_date", row.get("date"))
        if not code or not trading_date:
            raise ValueError("each daily bar requires code and trading_date")
        values.append(
            (
                str(code),
                str(trading_date),
                str(row.get("adjust_flag") or "qfq"),
                *(row.get(column) for column in _VALUE_COLUMNS[:-3]),
                row.get("source"),
                row.get("source_version"),
                row.get("updated_at") or now,
            )
        )

    ensure_schema()
    placeholders = ", ".join("?" for _ in _COLUMNS)
    updates = ", ".join(
        f"{column}=excluded.{column}" for column in _COLUMNS[3:]
    )
    with _connect() as connection:
        connection.executemany(
            f"""
            INSERT INTO daily_bars ({', '.join(_COLUMNS)})
            VALUES ({placeholders})
            ON CONFLICT(code, trading_date, adjust_flag) DO UPDATE SET {updates}
            """,
            values,
        )
    return len(values)


def _normalise_codes(codes: Sequence[str]) -> list[str]:
    result = [str(code) for code in codes if code is not None]
    if not result:
        raise ValueError("codes must not be empty")
    return list(dict.fromkeys(result))


def _query_rows(sql: str, parameters: Sequence[Any]) -> list[dict[str, Any]]:
    ensure_schema()
    with _connect() as connection:
        return [dict(row) for row in connection.execute(sql, parameters)]


def get_daily_bars(
    codes: Sequence[str],
    end_date: str,
    lookback: int,
    adjust_flag: str = "qfq",
) -> list[dict[str, Any]]:
    """Return up to ``lookback`` bars per code, sorted by code and date."""
    if lookback < 0:
        raise ValueError("lookback must be non-negative")
    code_list = _normalise_codes(codes)
    if lookback == 0:
        return []
    marks = ", ".join("?" for _ in code_list)
    sql = f"""
        SELECT {', '.join(_COLUMNS)} FROM (
            SELECT daily_bars.*, ROW_NUMBER() OVER (
                PARTITION BY code ORDER BY trading_date DESC
            ) AS row_number
            FROM daily_bars
            WHERE code IN ({marks}) AND trading_date <= ? AND adjust_flag = ?
        ) WHERE row_number <= ?
        ORDER BY code, trading_date
    """
    return _query_rows(sql, [*code_list, str(end_date), adjust_flag, lookback])


def get_latest_daily_bars(
    codes: Sequence[str], trading_date: str | None = None
) -> list[dict[str, Any]]:
    """Return the latest available bar at or before ``trading_date`` per code."""
    code_list = _normalise_codes(codes)
    marks = ", ".join("?" for _ in code_list)
    date_clause = "" if trading_date is None else " AND trading_date <= ?"
    sql = f"""
        SELECT {', '.join(_COLUMNS)} FROM (
            SELECT daily_bars.*, ROW_NUMBER() OVER (
                PARTITION BY code ORDER BY trading_date DESC, adjust_flag
            ) AS row_number
            FROM daily_bars
            WHERE code IN ({marks}){date_clause}
        ) WHERE row_number = 1
        ORDER BY code, trading_date
    """
    parameters: list[Any] = [*code_list]
    if trading_date is not None:
        parameters.append(str(trading_date))
    return _query_rows(sql, parameters)


def cache_stats() -> dict[str, Any]:
    """Return basic cache size and date-range statistics."""
    ensure_schema()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS row_count, COUNT(DISTINCT code) AS code_count,
                   MIN(trading_date) AS min_date, MAX(trading_date) AS max_date
            FROM daily_bars
            """
        ).fetchone()
    return {
        "path": str(_database_path()),
        "row_count": row["row_count"],
        "code_count": row["code_count"],
        "min_date": row["min_date"],
        "max_date": row["max_date"],
    }
