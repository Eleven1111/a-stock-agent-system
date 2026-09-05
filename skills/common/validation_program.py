#!/usr/bin/env python3
"""Auditable controls for point-in-time strategy validation.

The module deliberately separates *control-plane readiness* from empirical
success.  It can register immutable evidence and compute validation metrics,
but it never upgrades a strategy to live use.
"""

from __future__ import annotations

import fcntl
import hashlib
import itertools
import json
import math
import os
import random
import statistics
import subprocess
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any, Iterable, Iterator, Mapping, Sequence

from a_share_rules import CalendarCoverageError, is_trading_day
from market_snapshot import PointInTimeViolation, validate_point_in_time


_PROCESS_INSTANCE_ID = uuid.uuid4().hex

# Bumped together with the deflated-Sharpe / CSCV method change of 2026-09-05.
# Artifacts stamped with the v1 pair were produced by the superseded estimators
# and deliberately stop satisfying the gate: they have to be recomputed, not
# grandfathered.  See docs/statistical-method-migration.md.
STATISTICS_SCHEMA_VERSION = "statistical-validation-suite-v2"
STATISTICS_COMPUTED_BY = "validation_program-v2"


class ValidationError(ValueError):
    """A fail-closed validation error with a stable reason code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_hash(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _persist_content_addressed(
    source: Path, store: Path, digest: str
) -> Path:
    store.mkdir(parents=True, exist_ok=True)
    target = store / f"{digest}.json"
    if target.exists():
        if _file_hash(target) != digest:
            raise ValidationError("daily_evidence_store_corrupt")
        return target
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        payload = source.read_bytes()
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    if _file_hash(target) != digest:
        raise ValidationError("daily_evidence_store_corrupt")
    return target


@contextmanager
def _locked_jsonl(path: Path) -> Iterator[Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            yield handle
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_locked_records(handle: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    previous_hash: str | None = None
    for line_number, raw in enumerate(handle, start=1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ValidationError("registry_corrupt", f"invalid record at line {line_number}") from exc
        if not isinstance(value, dict):
            raise ValidationError("registry_corrupt", f"non-object record at line {line_number}")
        recorded_hash = value.get("record_sha256")
        hash_payload = {key: item for key, item in value.items() if key != "record_sha256"}
        if (
            value.get("previous_record_sha256") != previous_hash
            or recorded_hash != _canonical_hash(hash_payload)
        ):
            raise ValidationError("registry_corrupt", f"hash-chain mismatch at line {line_number}")
        records.append(value)
        previous_hash = str(recorded_hash)
    return records


def _append_locked(handle: Any, record: Mapping[str, Any]) -> None:
    handle.seek(0)
    prior = _read_locked_records(handle)
    chained = dict(record)
    chained["previous_record_sha256"] = prior[-1]["record_sha256"] if prior else None
    chained["record_sha256"] = _canonical_hash(chained)
    if isinstance(record, dict):
        record.update(chained)
    payload = _canonical_bytes(chained) + b"\n"
    handle.seek(0, os.SEEK_END)
    handle.buffer.write(payload)
    handle.flush()
    os.fsync(handle.fileno())


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args], cwd=repo, check=check, capture_output=True, text=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValidationError("git_state_unavailable") from exc


class OOSRegistry:
    """Append-only two-invocation registry for OOS commitments and reveals."""

    def __init__(
        self,
        registry_path: str | os.PathLike[str],
        repo_root: str | os.PathLike[str],
        *,
        invocation_id: str | None = None,
    ) -> None:
        self.path = Path(registry_path)
        self.repo_root = Path(repo_root).resolve()
        self.invocation_id = invocation_id or str(uuid.uuid4())

    def _head(self) -> str:
        return _git(self.repo_root, "rev-parse", "HEAD").stdout.strip()

    def _assert_clean(self) -> None:
        if _git(self.repo_root, "status", "--porcelain", "--untracked-files=all").stdout.strip():
            raise ValidationError("dirty_tree")

    def create_precommit(
        self,
        rules_path: str | os.PathLike[str],
        dataset_path: str | os.PathLike[str],
        *,
        split: Mapping[str, Any],
        variants: Sequence[str],
        fold_ids: Sequence[str],
        thresholds_path: str | os.PathLike[str],
    ) -> dict[str, Any]:
        self._assert_clean()
        variant_set = _unique_nonempty(variants, "variant_invalid")
        fold_set = _unique_nonempty(fold_ids, "fold_invalid")
        if not isinstance(split, Mapping) or not split.get("method"):
            raise ValidationError("split_invalid")
        core = {
            "schema_version": "oos-precommit-v1",
            "ancestor_commit": self._head(),
            "clean_tree": True,
            "rules_sha256": _file_hash(rules_path),
            "dataset_sha256": _file_hash(dataset_path),
            "thresholds_sha256": _file_hash(thresholds_path),
            "split": dict(split),
            "variants": sorted(variant_set),
            "fold_ids": sorted(fold_set),
        }
        precommit_id = _canonical_hash(core)
        with _locked_jsonl(self.path) as handle:
            records = _read_locked_records(handle)
            existing = next(
                (record for record in records if record.get("precommit_id") == precommit_id), None
            )
            if existing is not None:
                return existing
            record = {
                "record_type": "precommit",
                "precommit_id": precommit_id,
                "invocation_id": self.invocation_id,
                "process_instance_id": _PROCESS_INSTANCE_ID,
                "created_at": _now(),
                **core,
            }
            _append_locked(handle, record)
            return record

    def register_result(
        self,
        precommit_id: str,
        rules_path: str | os.PathLike[str],
        dataset_path: str | os.PathLike[str],
        *,
        variant_results: Mapping[str, Mapping[str, Any]],
        fold_results: Mapping[str, Mapping[str, Any]],
        thresholds_path: str | os.PathLike[str],
    ) -> dict[str, Any]:
        with _locked_jsonl(self.path) as handle:
            records = _read_locked_records(handle)
            precommit = next(
                (
                    record
                    for record in records
                    if record.get("record_type") == "precommit"
                    and record.get("precommit_id") == precommit_id
                ),
                None,
            )
            if precommit is None:
                raise ValidationError("precommit_missing")
            if (
                precommit.get("invocation_id") == self.invocation_id
                or precommit.get("process_instance_id") == _PROCESS_INSTANCE_ID
            ):
                raise ValidationError("same_run_reveal")
            if any(
                record.get("record_type") == "result"
                and record.get("precommit_id") == precommit_id
                for record in records
            ):
                raise ValidationError("duplicate_reveal")
            if _file_hash(rules_path) != precommit.get("rules_sha256"):
                raise ValidationError("artifact_tampered", "rules hash changed")
            if _file_hash(dataset_path) != precommit.get("dataset_sha256"):
                raise ValidationError("artifact_tampered", "dataset hash changed")
            if _file_hash(thresholds_path) != precommit.get("thresholds_sha256"):
                raise ValidationError("artifact_tampered", "threshold hash changed")
            self._assert_ancestor(str(precommit.get("ancestor_commit") or ""))
            self._assert_clean()
            _assert_exact_keys(variant_results, precommit.get("variants") or [], "variant")
            _assert_exact_keys(fold_results, precommit.get("fold_ids") or [], "fold")
            variants = _normalise_results(variant_results, "variant_id")
            folds = _normalise_results(fold_results, "fold_id")
            result_core = {
                "schema_version": "oos-result-v1",
                "precommit_id": precommit_id,
                "revealed_from_commit": self._head(),
                "variants": variants,
                "folds": folds,
            }
            record = {
                "record_type": "result",
                "invocation_id": self.invocation_id,
                "created_at": _now(),
                "status": "registered",
                **result_core,
                "artifact_sha256": _canonical_hash(result_core),
            }
            _append_locked(handle, record)
            return record

    def _assert_ancestor(self, ancestor: str) -> None:
        if not ancestor:
            raise ValidationError("ancestry_invalid")
        result = _git(
            self.repo_root, "merge-base", "--is-ancestor", ancestor, "HEAD", check=False
        )
        if result.returncode != 0:
            raise ValidationError("ancestry_invalid")


def _unique_nonempty(values: Sequence[str], code: str) -> set[str]:
    normalised = [str(value).strip() for value in values]
    if not normalised or any(not value for value in normalised) or len(set(normalised)) != len(normalised):
        raise ValidationError(code)
    return set(normalised)


def _assert_exact_keys(results: Mapping[str, Any], expected: Iterable[str], prefix: str) -> None:
    actual = set(results)
    required = set(expected)
    if required - actual:
        raise ValidationError(f"{prefix}_missing")
    if actual - required:
        raise ValidationError(f"late_{prefix}_addition")


def _normalise_results(results: Mapping[str, Mapping[str, Any]], id_field: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for identifier in sorted(results):
        result = results[identifier]
        if not isinstance(result, Mapping) or result.get("status") not in {
            "passed", "failed", "not_evaluated"
        }:
            raise ValidationError("result_invalid", identifier)
        output.append({id_field: identifier, **dict(result)})
    return output


def build_walk_forward_folds(
    total_observations: int,
    *,
    train_size: int,
    calibration_size: int,
    test_size: int,
    step: int,
    purge: int,
    embargo: int,
    mode: str = "expanding",
) -> list[dict[str, Any]]:
    """Generate index-exclusive walk-forward folds with explicit time gaps."""

    values = (total_observations, train_size, test_size, step)
    if any(not isinstance(value, int) or value <= 0 for value in values):
        raise ValidationError("walk_forward_invalid")
    if (
        purge < 0
        or embargo < 0
        or calibration_size <= 0
        or mode not in {"expanding", "rolling"}
    ):
        raise ValidationError("walk_forward_invalid")
    if train_size - purge <= 0:
        raise ValidationError("walk_forward_invalid")
    folds: list[dict[str, int | str]] = []
    test_start = train_size + calibration_size + 2 * purge
    increment = max(step, test_size + embargo)
    while test_start + test_size <= total_observations:
        calibration_end = test_start - purge
        calibration_start = calibration_end - calibration_size
        train_end = calibration_start - purge
        train_start = 0 if mode == "expanding" else max(0, train_end - train_size)
        if train_end <= train_start:
            raise ValidationError("walk_forward_invalid")
        fold: dict[str, Any] = {
                "fold_id": f"fold-{len(folds)}",
                "train_start": train_start,
                "train_end": train_end,
                "test_start": test_start,
                "test_end": test_start + test_size,
                "purge": purge,
                "embargo": embargo,
            }
        fold.update({
            "calibration_start": calibration_start,
            "calibration_end": calibration_end,
            "roles": ["train", "calibration", "test"],
        })
        folds.append(fold)
        test_start += increment
    if not folds:
        raise ValidationError("walk_forward_invalid")
    return folds


def compute_effective_samples(trades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Kish effective *breadth* across trade/stock/regime/session clusters.

    These are dispersion counts, not autocorrelation-adjusted effective sample
    sizes: thirty trades opened on one session score ``trade = 30`` because they
    occupy thirty distinct (session, stock) slots, while ``session = 1`` records
    that they share a single day of market information.  ``basis`` is emitted so
    downstream reports cannot quietly present breadth as independence, and
    ``sector`` stays ``None`` unless the trades actually carry a sector, so an
    absent dimension is never mistaken for a satisfied one.
    """

    input_hash = _canonical_hash(list(trades))
    required = ("trade_id", "stock", "session", "regime")
    if not trades or any(any(not trade.get(field) for field in required) for trade in trades):
        return {"status": "not_evaluated", "reason": "cluster_data_missing", "input_sha256": input_hash}
    trade_ids = [str(trade["trade_id"]) for trade in trades]
    if len(set(trade_ids)) != len(trade_ids):
        return {"status": "not_evaluated", "reason": "duplicate_trade_id", "input_sha256": input_hash}
    sectors = [str(trade.get("sector") or "") for trade in trades]
    return {
        "trade": _kish_cluster_count(
            Counter(
                f"{trade['session']}|{trade['stock']}"
                for trade in trades
            )
        ),
        "stock": _kish_cluster_count(Counter(str(trade["stock"]) for trade in trades)),
        "regime": _kish_cluster_count(Counter(str(trade["regime"]) for trade in trades)),
        "session": _kish_cluster_count(Counter(str(trade["session"]) for trade in trades)),
        "sector": _kish_cluster_count(Counter(sectors)) if all(sectors) else None,
        "sector_status": "evaluated" if all(sectors) else "unavailable",
        "basis": "kish_breadth",
        "autocorrelation_adjusted": False,
        "distinct_sessions": len({str(trade["session"]) for trade in trades}),
        "status": "evaluated",
        "input_sha256": input_hash,
    }


def _kish_cluster_count(counts: Counter[str]) -> float:
    total = sum(counts.values())
    denominator = sum(count * count for count in counts.values())
    return total * total / denominator if denominator else 0.0


def _numeric_series(values: Sequence[float], *, minimum: int = 2) -> tuple[list[float], str] | None:
    try:
        series = [float(value) for value in values]
    except (TypeError, ValueError):
        return None
    if len(series) < minimum or any(not math.isfinite(value) for value in series):
        return None
    return series, _canonical_hash(series)


def block_bootstrap_mean(
    values: Sequence[float], *, block_length: int, resamples: int = 1000, seed: int = 0
) -> dict[str, Any]:
    parsed = _numeric_series(values, minimum=max(4, block_length * 2))
    config = {"block_length": block_length, "resamples": resamples, "seed": seed}
    if parsed is None or block_length <= 0 or resamples < 20:
        return {"status": "not_evaluated", "reason": "sample_insufficient", "config_sha256": _canonical_hash(config)}
    series, input_hash = parsed
    rng = random.Random(seed)
    last_start = len(series) - block_length
    means: list[float] = []
    for _ in range(resamples):
        sample: list[float] = []
        while len(sample) < len(series):
            start = rng.randint(0, last_start)
            sample.extend(series[start:start + block_length])
        means.append(statistics.fmean(sample[:len(series)]))
    means.sort()
    return {
        "status": "evaluated",
        "method": "moving_block_bootstrap",
        "mean": statistics.fmean(series),
        "ci_low": _quantile(means, 0.025),
        "ci_high": _quantile(means, 0.975),
        "input_sha256": input_hash,
        "config_sha256": _canonical_hash(config),
    }


def hac_mean_uncertainty(values: Sequence[float], *, lags: int) -> dict[str, Any]:
    parsed = _numeric_series(values, minimum=max(4, lags + 2))
    config = {"lags": lags}
    if parsed is None or lags < 0:
        return {"status": "not_evaluated", "reason": "sample_insufficient", "config_sha256": _canonical_hash(config)}
    series, input_hash = parsed
    mean = statistics.fmean(series)
    centred = [value - mean for value in series]
    n = len(series)
    long_run_variance = sum(value * value for value in centred) / n
    for lag in range(1, min(lags, n - 1) + 1):
        covariance = sum(centred[index] * centred[index - lag] for index in range(lag, n)) / n
        long_run_variance += 2 * (1 - lag / (lags + 1)) * covariance
    if long_run_variance <= 0 or not math.isfinite(long_run_variance):
        return {
            "status": "not_evaluated", "reason": "degenerate_sample",
            "input_sha256": input_hash, "config_sha256": _canonical_hash(config),
        }
    standard_error = math.sqrt(long_run_variance / n)
    return {
        "status": "evaluated",
        "method": "newey_west_hac",
        "mean": mean,
        "standard_error": standard_error,
        "ci_low": mean - 1.96 * standard_error,
        "ci_high": mean + 1.96 * standard_error,
        "input_sha256": input_hash,
        "config_sha256": _canonical_hash(config),
    }


def fdr_benjamini_hochberg(p_values: Mapping[str, float], *, alpha: float) -> dict[str, Any]:
    config = {"alpha": alpha}
    try:
        ordered = sorted((str(key), float(value)) for key, value in p_values.items())
    except (TypeError, ValueError):
        ordered = []
    if not ordered or not 0 < alpha < 1 or any(not 0 <= value <= 1 for _, value in ordered):
        return {"status": "not_evaluated", "reason": "p_values_invalid", "config_sha256": _canonical_hash(config)}
    ranked = sorted(ordered, key=lambda item: (item[1], item[0]))
    count = len(ranked)
    cutoff = 0
    for rank, (_, value) in enumerate(ranked, start=1):
        if value <= rank * alpha / count:
            cutoff = rank
    adjusted: dict[str, float] = {}
    running = 1.0
    for rank in range(count, 0, -1):
        key, value = ranked[rank - 1]
        running = min(running, value * count / rank)
        adjusted[key] = min(1.0, running)
    return {
        "status": "evaluated",
        "method": "benjamini_hochberg",
        "discoveries": [key for key, _ in ranked[:cutoff]],
        "adjusted_p_values": dict(sorted(adjusted.items())),
        "input_sha256": _canonical_hash(dict(ordered)),
        "config_sha256": _canonical_hash(config),
    }


_PBO_TIE_TOLERANCE = 1e-12


def _parsed_variant_panel(
    variant_returns: Mapping[str, Sequence[float]], partitions: int
) -> tuple[dict[str, list[float]], dict[str, str], set[int]]:
    """Numeric variant panel of a single common length, plus what was dropped."""

    parsed: dict[str, list[float]] = {}
    excluded: dict[str, str] = {}
    lengths: set[int] = set()
    for key, values in variant_returns.items():
        numeric = _numeric_series(values, minimum=partitions)
        if numeric is None:
            excluded[str(key)] = "series_invalid"
            continue
        parsed[str(key)] = numeric[0]
        lengths.add(len(numeric[0]))
    if len(lengths) > 1:
        longest = max(lengths)
        for key, series in list(parsed.items()):
            if len(series) != longest:
                excluded[key] = "length_mismatch"
                parsed.pop(key)
        lengths = {longest}
    return parsed, excluded, lengths


def _collapse_indistinguishable(
    parsed: Mapping[str, list[float]],
) -> tuple[list[list[str]], dict[str, list[float]]]:
    """Group byte-identical series; ``k`` copies of one strategy are one trial."""

    groups_by_series: dict[str, list[str]] = {}
    for key, series in sorted(parsed.items()):
        groups_by_series.setdefault(_canonical_hash(series), []).append(key)
    duplicates = [members for members in groups_by_series.values() if len(members) > 1]
    distinct = {members[0]: parsed[members[0]] for members in groups_by_series.values()}
    return duplicates, distinct


def probability_of_backtest_overfitting(
    variant_returns: Mapping[str, Sequence[float]],
    *,
    partitions: int,
    embargo: int = 0,
) -> dict[str, Any]:
    """CSCV probability of backtest overfitting.

    Selection in-sample and ranking out-of-sample both use the mean return, and
    the result is invariant to variant naming: ties are resolved by averaging
    over the tied set rather than by lexical order.  Variants that are numerically
    indistinguishable are collapsed first, because ``k`` copies of one strategy
    are one trial, not ``k`` competing ones.
    """

    config = {"partitions": partitions, "embargo": embargo, "method_version": "cscv-v2"}
    hashed_config = _canonical_hash(config)
    parsed, excluded, lengths = _parsed_variant_panel(variant_returns, partitions)
    duplicate_groups, distinct = _collapse_indistinguishable(parsed)

    if (
        len(distinct) < 2 or partitions < 4 or partitions % 2 or embargo < 0
        or next(iter(lengths), 0) < partitions * 2
    ):
        reason = (
            "insufficient_distinct_variants"
            if len(parsed) >= 2 and len(distinct) < 2
            else "sample_insufficient"
        )
        return {
            "status": "not_evaluated", "reason": reason,
            "distinct_variants": sorted(distinct),
            "duplicate_groups": duplicate_groups,
            "excluded_variants": dict(sorted(excluded.items())),
            "config_sha256": hashed_config,
        }

    groups = _partition_indices(next(iter(lengths)), partitions)
    overfit_fractions = _cscv_overfit_fractions(distinct, groups, partitions, embargo)
    if overfit_fractions is None:
        return {
            "status": "not_evaluated", "reason": "embargo_exhausted_test_fold",
            "config_sha256": hashed_config,
        }
    return {
        "status": "evaluated",
        "method": "combinatorially_symmetric_cross_validation",
        "method_version": "cscv-v2",
        "pbo": statistics.fmean(overfit_fractions),
        "combinations": len(overfit_fractions),
        "resolution": 1 / len(overfit_fractions),
        "selection_metric": "in_sample_mean_return",
        "ranking_metric": "out_of_sample_mean_return",
        "distinct_variants": sorted(distinct),
        "duplicate_groups": duplicate_groups,
        "excluded_variants": dict(sorted(excluded.items())),
        "purge_embargo": {"observations": embargo, "applied": embargo > 0},
        "input_sha256": _canonical_hash(distinct),
        "config_sha256": hashed_config,
    }


def _cscv_overfit_fractions(
    distinct: Mapping[str, list[float]],
    groups: Sequence[Sequence[int]],
    partitions: int,
    embargo: int,
) -> list[float] | None:
    """Per-fold share of the in-sample winners that land in the bottom OOS half."""

    fractions: list[float] = []
    for train_groups in itertools.combinations(range(partitions), partitions // 2):
        train_indices = [index for group in train_groups for index in groups[group]]
        test_indices = _test_indices(groups, train_groups, partitions, embargo)
        if len(test_indices) < 2:
            return None
        train_means = {
            key: statistics.fmean(values[index] for index in train_indices)
            for key, values in distinct.items()
        }
        test_means = {
            key: statistics.fmean(values[index] for index in test_indices)
            for key, values in distinct.items()
        }
        best = max(train_means.values())
        selected = [
            key for key, value in train_means.items()
            if math.isclose(value, best, rel_tol=_PBO_TIE_TOLERANCE, abs_tol=_PBO_TIE_TOLERANCE)
        ]
        ranks = _mid_ranks(test_means)
        count = len(test_means)
        overfit = [
            math.log((ranks[key] / (count + 1)) / (1 - ranks[key] / (count + 1))) <= 0
            for key in selected
        ]
        fractions.append(sum(overfit) / len(overfit))
    return fractions


def _test_indices(
    groups: Sequence[Sequence[int]], train_groups: Sequence[int], partitions: int, embargo: int
) -> list[int]:
    """Test-fold indices with ``embargo`` observations purged after each train block."""

    indices: list[int] = []
    for group in range(partitions):
        if group in train_groups:
            continue
        block = list(groups[group])
        if embargo and group > 0 and (group - 1) in train_groups:
            block = block[embargo:]
        indices.extend(block)
    return indices


def _mid_ranks(values: Mapping[str, float]) -> dict[str, float]:
    """Ascending ranks starting at 1, ties sharing the average rank."""

    ordered = sorted(values.items(), key=lambda item: item[1])
    ranks: dict[str, float] = {}
    position = 0
    while position < len(ordered):
        end = position + 1
        while end < len(ordered) and math.isclose(
            ordered[end][1], ordered[position][1],
            rel_tol=_PBO_TIE_TOLERANCE, abs_tol=_PBO_TIE_TOLERANCE,
        ):
            end += 1
        shared = (position + 1 + end) / 2
        for key, _ in ordered[position:end]:
            ranks[key] = shared
        position = end
    return ranks


def _partition_indices(count: int, partitions: int) -> list[list[int]]:
    return [list(range(start * count // partitions, (start + 1) * count // partitions)) for start in range(partitions)]


_EULER_MASCHERONI = 0.5772156649015329


def expected_maximum_sharpe(*, trials: int, trial_sharpe_variance: float) -> float:
    """Bailey & Lopez de Prado (2014) expected maximum Sharpe across ``trials``.

    ``E[max SR] = sqrt(V) * [(1 - g) * Z^-1(1 - 1/N) + g * Z^-1(1 - 1/(N*e))]``
    with ``V`` the cross-trial variance of the observed Sharpe ratios and ``g``
    the Euler-Mascheroni constant.  The ``sqrt(V)`` scale is what puts the
    threshold on the same frequency as the candidate Sharpe ratio; dropping it
    silently compares a per-period Sharpe against a standard-normal quantile.
    """

    normal = NormalDist()
    gumbel = (
        (1 - _EULER_MASCHERONI) * normal.inv_cdf(1 - 1 / trials)
        + _EULER_MASCHERONI * normal.inv_cdf(1 - 1 / (trials * math.e))
    )
    return math.sqrt(trial_sharpe_variance) * gumbel


def observed_trial_sharpes(
    variant_returns: Mapping[str, Sequence[float]], *, minimum_observations: int = 20
) -> dict[str, Any]:
    """Per-variant Sharpe ratios, at the frequency of the supplied series.

    Returned separately from :func:`deflated_sharpe` so the caller stays the
    single owner of "which trials belong to this experiment"; the estimator
    never invents a trial set it was not given.
    """

    sharpes: dict[str, float] = {}
    excluded: dict[str, str] = {}
    lengths: set[int] = set()
    for key, values in variant_returns.items():
        parsed = _numeric_series(values, minimum=minimum_observations)
        if parsed is None:
            excluded[str(key)] = "sample_insufficient"
            continue
        series = parsed[0]
        std = statistics.stdev(series)
        if std <= 0 or not math.isfinite(std):
            excluded[str(key)] = "degenerate_sample"
            continue
        sharpes[str(key)] = statistics.fmean(series) / std
        lengths.add(len(series))
    return {
        "sharpes": dict(sorted(sharpes.items())),
        "excluded": dict(sorted(excluded.items())),
        "frequency_consistent": len(lengths) <= 1,
        "observation_count": next(iter(lengths)) if len(lengths) == 1 else None,
    }


def deflated_sharpe(
    values: Sequence[float],
    *,
    trials: int,
    trial_sharpes: Sequence[float] | None = None,
) -> dict[str, Any]:
    """Deflated Sharpe ratio, or the probabilistic Sharpe ratio when ``trials == 1``.

    ``trial_sharpes`` must carry the Sharpe ratios of the whole trial set at the
    *same* return frequency as ``values``.  Without it the cross-trial dispersion
    is unknowable, and the function refuses to evaluate rather than substituting
    a variance of one (which is what silently turned a per-period Sharpe of 0.12
    into a "threshold" of 1.07).
    """

    parsed = _numeric_series(values, minimum=20)
    config = {"trials": trials, "method_version": "deflated_sharpe-v2"}
    hashed_config = _canonical_hash(config)
    if parsed is None or trials < 1:
        return {"status": "not_evaluated", "reason": "sample_insufficient", "config_sha256": hashed_config}
    series, input_hash = parsed
    mean = statistics.fmean(series)
    std = statistics.stdev(series)
    if std <= 0:
        return {"status": "not_evaluated", "reason": "degenerate_sample", "input_sha256": input_hash, "config_sha256": hashed_config}
    sharpe = mean / std
    centred = [(value - mean) / std for value in series]
    skew = statistics.fmean(value ** 3 for value in centred)
    kurtosis = statistics.fmean(value ** 4 for value in centred)

    dispersion: float | None = None
    if trials == 1:
        method = "probabilistic_sharpe_ratio"
        expected_maximum = 0.0
        threshold_source = "single_trial_no_deflation"
    else:
        method = "deflated_sharpe"
        threshold_source = "observed_trial_sharpe_variance"
        observed = _numeric_series(list(trial_sharpes or ()), minimum=2)
        if observed is None:
            return {
                "status": "not_evaluated", "reason": "trial_dispersion_unavailable",
                "input_sha256": input_hash, "config_sha256": hashed_config,
            }
        dispersion = statistics.variance(observed[0])
        if dispersion <= 0 or not math.isfinite(dispersion):
            return {
                "status": "not_evaluated", "reason": "trial_dispersion_degenerate",
                "input_sha256": input_hash, "config_sha256": hashed_config,
            }
        expected_maximum = expected_maximum_sharpe(
            trials=len(observed[0]), trial_sharpe_variance=dispersion
        )
        trials = len(observed[0])

    variance = (1 - skew * sharpe + ((kurtosis - 1) / 4) * sharpe * sharpe) / (len(series) - 1)
    if variance <= 0 or not math.isfinite(variance):
        return {"status": "not_evaluated", "reason": "degenerate_sample", "input_sha256": input_hash, "config_sha256": hashed_config}
    probability = NormalDist().cdf((sharpe - expected_maximum) / math.sqrt(variance))
    return {
        "status": "evaluated",
        "method": method,
        "method_version": "deflated_sharpe-v2",
        "sharpe": sharpe,
        "expected_maximum_sharpe": expected_maximum,
        "probability": probability,
        "effective_trials": trials,
        "trial_sharpe_variance": dispersion,
        "threshold_source": threshold_source,
        "observation_count": len(series),
        "return_frequency": "per_observation_period",
        "input_sha256": input_hash,
        "config_sha256": hashed_config,
    }


def compute_statistical_validation(
    *,
    primary_variant: str,
    variant_returns: Mapping[str, Sequence[float]],
    p_values: Mapping[str, float],
    config: Mapping[str, Any],
    seed: int = 0,
) -> dict[str, Any]:
    """Compute the complete statistical suite from raw series, never pass flags."""

    try:
        primary = variant_returns[primary_variant]
        minimum = int(config["minimum_observations"])
        if len(primary) < minimum:
            raise ValueError
        bootstrap = block_bootstrap_mean(
            primary,
            block_length=int(config["block_length"]),
            resamples=int(config["bootstrap_resamples"]),
            seed=seed,
        )
        hac = hac_mean_uncertainty(primary, lags=int(config["hac_lags"]))
        fdr = fdr_benjamini_hochberg(p_values, alpha=float(config["fdr_alpha"]))
        pbo = probability_of_backtest_overfitting(
            variant_returns,
            partitions=int(config["pbo_partitions"]),
            embargo=int(config.get("pbo_embargo") or 0),
        )
        trials = observed_trial_sharpes(variant_returns, minimum_observations=minimum)
        if not trials["frequency_consistent"]:
            dsr = {
                "status": "not_evaluated", "reason": "trial_frequency_inconsistent",
                "config_sha256": _canonical_hash({"trials": len(trials["sharpes"])}),
            }
        else:
            dsr = deflated_sharpe(
                primary,
                trials=len(trials["sharpes"]) or 1,
                trial_sharpes=list(trials["sharpes"].values()),
            )
        maximum_pbo = float(config["maximum_pbo"])
        minimum_dsr = float(config["minimum_deflated_sharpe_probability"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("statistics_config_invalid") from exc
    calculations = {
        "block_bootstrap": bootstrap,
        "hac": hac,
        "fdr": fdr,
        "pbo": pbo,
        "deflated_sharpe": dsr,
    }
    if any(result.get("status") != "evaluated" for result in calculations.values()):
        status = "not_evaluated"
        reasons = ["statistics_not_evaluated"]
    else:
        checks = {
            "bootstrap_positive": float(bootstrap["ci_low"]) > 0,
            "hac_positive": float(hac["ci_low"]) > 0,
            "fdr_primary_discovery": primary_variant in fdr["discoveries"],
            "pbo_within_limit": float(pbo["pbo"]) <= maximum_pbo,
            "deflated_sharpe_within_limit": float(dsr["probability"]) >= minimum_dsr,
        }
        reasons = sorted(key for key, passed in checks.items() if not passed)
        status = "passed" if not reasons else "failed"
    core = {
        "schema_version": STATISTICS_SCHEMA_VERSION,
        "computed_by": STATISTICS_COMPUTED_BY,
        "primary_variant": primary_variant,
        "calculations": calculations,
        "trial_set": {
            "included": sorted(trials["sharpes"]),
            "excluded": trials["excluded"],
            "frequency_consistent": trials["frequency_consistent"],
            "observation_count": trials["observation_count"],
        },
        "decision_thresholds_sha256": _canonical_hash(dict(config)),
        "status": status,
        "reasons": reasons,
    }
    return {**core, "artifact_sha256": _canonical_hash(core)}


def _quantile(values: Sequence[float], probability: float) -> float:
    position = (len(values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(values[lower])
    fraction = position - lower
    return float(values[lower] * (1 - fraction) + values[upper] * fraction)


def load_validation_thresholds(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load and validate the versioned empirical/statistical/shadow policy."""

    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
        empirical = config["empirical"]
        statistical = config["statistics"]
        shadow = config["shadow"]
        if not config.get("schema_version") or not config.get("effective_date"):
            raise KeyError
        positive_fields = (
            empirical["minimum_real_trading_days"],
            empirical["minimum_trade_effective_samples"],
            empirical["minimum_stock_effective_samples"],
            empirical["minimum_regime_effective_samples"],
            statistical["minimum_observations"],
            statistical["block_length"],
            statistical["bootstrap_resamples"],
            statistical["hac_lags"],
            statistical["pbo_partitions"],
            statistical["maximum_pbo"],
            statistical["minimum_deflated_sharpe_probability"],
            shadow["minimum_trading_days"],
            shadow["maximum_simulation_error"],
            shadow["auto_demotion_error"],
            shadow["maximum_manual_pilot_weight"],
        )
        if any(not math.isfinite(float(value)) or float(value) <= 0 for value in positive_fields):
            raise ValueError
        if not 0 < float(statistical["fdr_alpha"]) < 1:
            raise ValueError
        if int(statistical["pbo_partitions"]) % 2:
            raise ValueError
        if not 0 < float(statistical["maximum_pbo"]) <= 1:
            raise ValueError
        if not 0 < float(statistical["minimum_deflated_sharpe_probability"]) <= 1:
            raise ValueError
        if float(shadow["auto_demotion_error"]) < float(shadow["maximum_simulation_error"]):
            raise ValueError
        if float(shadow["maximum_manual_pilot_weight"]) > 1:
            raise ValueError
    except (
        OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError
    ) as exc:
        raise ValidationError("threshold_config_invalid") from exc
    return {**config, "config_sha256": _canonical_hash(config)}


class DailyEvidenceRegistry:
    """Immutable registry of one PIT evidence artifact per real trading date."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def records(self) -> list[dict[str, Any]]:
        with _locked_jsonl(self.path) as handle:
            return _read_locked_records(handle)

    def append(
        self,
        trading_date: str,
        artifact_path: str | os.PathLike[str],
        *,
        event_asof: str,
    ) -> dict[str, Any]:
        try:
            parsed_date = datetime.strptime(trading_date, "%Y-%m-%d").date().isoformat()
            parsed_asof = datetime.fromisoformat(event_asof)
        except (TypeError, ValueError) as exc:
            raise ValidationError("daily_evidence_invalid") from exc
        if parsed_asof.tzinfo is None:
            raise ValidationError("daily_evidence_invalid", "event_asof requires timezone")
        if parsed_asof.date().isoformat() != parsed_date:
            raise ValidationError("daily_evidence_invalid", "event_asof date mismatch")
        try:
            if not is_trading_day(parsed_date):
                raise ValidationError("daily_evidence_not_trading_day")
        except CalendarCoverageError as exc:
            raise ValidationError("calendar_coverage_missing") from exc
        artifact = Path(artifact_path)
        try:
            snapshot = json.loads(artifact.read_text(encoding="utf-8"))
            if (
                not isinstance(snapshot, dict)
                or snapshot.get("schema") != "market_snapshot_v1"
                or not str(snapshot.get("snapshot_id") or "").startswith("snap-")
                or Path(str(snapshot.get("snapshot_path") or "")).resolve()
                != artifact.resolve()
                or snapshot.get("payload_hash") != _canonical_hash(snapshot.get("payload"))
            ):
                raise ValueError
            point_in_time = snapshot.get("point_in_time")
            if not isinstance(point_in_time, dict):
                raise ValueError
            validated_pit = validate_point_in_time(
                event_asof=str(point_in_time.get("event_asof") or ""),
                evidence_time=str(point_in_time.get("evidence_time") or ""),
                captured_at=str(point_in_time.get("captured_at") or ""),
                decision_mode=str(point_in_time.get("decision_mode") or ""),
                stage_policy=point_in_time.get("stage_policy") or {},
            )
            if validated_pit["event_asof"] != parsed_date:
                raise ValueError
        except (
            OSError, UnicodeError, json.JSONDecodeError, PointInTimeViolation,
            TypeError, ValueError,
        ) as exc:
            raise ValidationError("daily_evidence_invalid", "PIT snapshot required") from exc
        digest = _file_hash(artifact)
        content_path = _persist_content_addressed(
            artifact,
            self.path.parent / f"{self.path.stem}.artifacts",
            digest,
        )
        core = {
            "schema_version": "pit-daily-evidence-v1",
            "trading_date": parsed_date,
            "event_asof": event_asof,
            "artifact_sha256": digest,
            "snapshot_id": snapshot["snapshot_id"],
            "payload_sha256": snapshot["payload_hash"],
            "content_addressed_path": str(content_path.resolve()),
        }
        record = {**core, "record_id": _canonical_hash(core), "registered_at": _now()}
        with _locked_jsonl(self.path) as handle:
            records = _read_locked_records(handle)
            existing = next((item for item in records if item.get("trading_date") == parsed_date), None)
            if existing is not None:
                if existing.get("record_id") == record["record_id"]:
                    return existing
                raise ValidationError("daily_evidence_conflict")
            _append_locked(handle, record)
            return record

    def coverage_report(self) -> dict[str, Any]:
        records = self.records()
        valid = [record for record in records if _daily_record_valid(record)]
        dates = sorted({str(record.get("trading_date")) for record in valid})
        core = {"schema_version": "pit-coverage-v1", "trading_dates": dates}
        return {
            **core,
            "real_trading_days": len(dates),
            "invalid_or_missing_artifacts": len(records) - len(valid),
            "status": "evaluated" if dates else "not_evaluated",
            "artifact_sha256": _canonical_hash(core),
        }


def evaluate_empirical_gate(
    daily_records: Sequence[Mapping[str, Any]],
    *,
    trades: Sequence[Mapping[str, Any]],
    statistics: Mapping[str, Any],
    shadow: Mapping[str, Any],
    broker: Mapping[str, Any],
    thresholds: Mapping[str, float],
) -> dict[str, Any]:
    """Evaluate control evidence; never accepts caller-supplied sample counts."""

    days = len(
        {
            str(record.get("trading_date"))
            for record in daily_records
            if _daily_record_valid(record)
        }
    )
    samples = compute_effective_samples(trades)
    reasons: list[str] = []
    if days < int(thresholds.get("minimum_real_trading_days", 60)):
        reasons.append("<60_days")
    sample_limits = {
        "trade": float(thresholds.get("minimum_trade_effective_samples", math.inf)),
        "stock": float(thresholds.get("minimum_stock_effective_samples", math.inf)),
        "regime": float(thresholds.get("minimum_regime_effective_samples", math.inf)),
    }
    if samples.get("status") != "evaluated" or any(
        float(samples.get(key, 0)) < limit for key, limit in sample_limits.items()
    ):
        reasons.append("independent_clusters_insufficient")
    statistics_valid = _statistics_artifact_valid(statistics)
    if not statistics_valid or statistics.get("status") == "not_evaluated":
        reasons.append("statistics_not_evaluated")
    elif statistics.get("status") != "passed":
        reasons.append("statistics_failed")
    if not _shadow_artifact_valid(shadow) or shadow.get("status") not in {
        "passed", "eligible_for_manual_pilot"
    }:
        reasons.append("shadow_not_evaluated")
    if not _broker_artifact_valid(broker) or broker.get("status") != "reconciled":
        reasons.append("broker_reconciliation_missing")
    core = {
        "schema_version": "empirical-validation-gate-v1",
        "computed_by": "validation_program-v1",
        "real_trading_days": days,
        "effective_samples": samples,
        "statistics_status": statistics.get("status", "not_evaluated") if statistics_valid else "not_evaluated",
        "shadow_status": shadow.get("status", "not_evaluated"),
        "broker_status": broker.get("status", "not_evaluated"),
        "reasons": sorted(set(reasons)),
    }
    result = {
        **core,
        "status": "passed" if not reasons else "blocked",
        "production_release": "eligible_for_review" if not reasons else "blocked",
    }
    return {**result, "artifact_sha256": _canonical_hash(result)}


def _artifact_hash_valid(record: Mapping[str, Any]) -> bool:
    digest = record.get("artifact_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        return False
    core = {
        key: value
        for key, value in record.items()
        if key not in {"artifact_sha256", "previous_record_sha256", "record_sha256"}
    }
    try:
        return digest == _canonical_hash(core)
    except (TypeError, ValueError):
        return False


def verify_validation_artifact(record: Mapping[str, Any]) -> bool:
    """Verify the canonical content hash of a validation control artifact."""

    return _artifact_hash_valid(record)


def verify_oos_precommit_record(record: Mapping[str, Any]) -> bool:
    """Verify a hash-chained precommit record returned by ``OOSRegistry``."""

    digest = record.get("record_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        return False
    core = {key: value for key, value in record.items() if key != "record_sha256"}
    try:
        return (
            record.get("record_type") == "precommit"
            and record.get("schema_version") == "oos-precommit-v1"
            and digest == _canonical_hash(core)
        )
    except (TypeError, ValueError):
        return False


def _statistics_artifact_valid(record: Mapping[str, Any]) -> bool:
    calculations = record.get("calculations") or {}
    methods = {
        "block_bootstrap": {"moving_block_bootstrap"},
        "hac": {"newey_west_hac"},
        "fdr": {"benjamini_hochberg"},
        "pbo": {"combinatorially_symmetric_cross_validation"},
        "deflated_sharpe": {"deflated_sharpe", "probabilistic_sharpe_ratio"},
    }
    return bool(
        _artifact_hash_valid(record)
        and record.get("schema_version") == STATISTICS_SCHEMA_VERSION
        and record.get("computed_by") == STATISTICS_COMPUTED_BY
        and all(
            isinstance(calculations.get(name), Mapping)
            and calculations[name].get("status") == "evaluated"
            and calculations[name].get("method") in accepted
            for name, accepted in methods.items()
        )
    )


def _shadow_artifact_valid(record: Mapping[str, Any]) -> bool:
    return bool(
        _artifact_hash_valid(record)
        and record.get("schema_version") == "shadow-window-v1"
        and record.get("thresholds_sha256")
    )


def _broker_artifact_valid(record: Mapping[str, Any]) -> bool:
    return bool(
        _artifact_hash_valid(record)
        and record.get("schema_version") == "broker-reconciliation-v1"
        and record.get("broker_authoritative") is True
    )


def _daily_record_valid(record: Mapping[str, Any]) -> bool:
    core = {
        "schema_version": "pit-daily-evidence-v1",
        "trading_date": record.get("trading_date"),
        "event_asof": record.get("event_asof"),
        "artifact_sha256": record.get("artifact_sha256"),
        "snapshot_id": record.get("snapshot_id"),
        "payload_sha256": record.get("payload_sha256"),
        "content_addressed_path": record.get("content_addressed_path"),
    }
    path = Path(str(record.get("content_addressed_path") or ""))
    try:
        content_valid = (
            path.is_file()
            and _file_hash(path) == record.get("artifact_sha256")
        )
    except OSError:
        content_valid = False
    return (
        record.get("schema_version") == core["schema_version"]
        and isinstance(record.get("record_id"), str)
        and record.get("record_id") == _canonical_hash(core)
        and content_valid
    )


def build_shadow_run_artifact(
    *,
    strategy_id: str,
    trading_date: str,
    precommit_id: str,
    thresholds_sha256: str,
    simulated_orders: Sequence[Mapping[str, Any]],
    live_ranking_before: Sequence[str],
    live_ranking_after: Sequence[str],
) -> dict[str, Any]:
    """Build a shadow-run artifact and prove that it had zero live effect."""

    if not strategy_id or not precommit_id or len(thresholds_sha256) != 64:
        raise ValidationError("shadow_artifact_invalid")
    try:
        parsed_date = datetime.strptime(trading_date, "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError) as exc:
        raise ValidationError("shadow_artifact_invalid") from exc
    before = [str(value) for value in live_ranking_before]
    after = [str(value) for value in live_ranking_after]
    if before != after:
        raise ValidationError("shadow_live_effect_detected")
    core = {
        "schema_version": "shadow-run-v1",
        "strategy_id": strategy_id,
        "trading_date": parsed_date,
        "precommit_id": precommit_id,
        "thresholds_sha256": thresholds_sha256,
        "simulated_orders": [dict(order) for order in simulated_orders],
        "live_ranking_sha256": _canonical_hash(before),
        "live_effect": "none",
    }
    return {**core, "artifact_sha256": _canonical_hash(core)}


def reconcile_broker_statement(
    statement_path: str | os.PathLike[str],
    *,
    ledger_summary: Mapping[str, Any],
    cash_tolerance: float,
) -> dict[str, Any]:
    """Import a normalized broker statement and compute reconciliation status."""

    if not math.isfinite(float(cash_tolerance)) or cash_tolerance < 0:
        raise ValidationError("broker_reconciliation_invalid")
    path = Path(statement_path)
    try:
        statement = json.loads(path.read_text(encoding="utf-8"))
        if statement.get("schema_version") != "broker-statement-v1":
            raise KeyError
        asof = datetime.fromisoformat(str(statement["asof"]))
        if asof.tzinfo is None:
            raise ValueError
        broker_cash = float(statement["cash_balance"])
        ledger_cash = float(ledger_summary["cash_balance"])
        broker_positions = _normalise_positions(statement["positions"])
        ledger_positions = _normalise_positions(ledger_summary["positions"])
    except (
        OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError
    ) as exc:
        raise ValidationError("broker_reconciliation_invalid") from exc
    if any(not math.isfinite(value) for value in (broker_cash, ledger_cash)):
        raise ValidationError("broker_reconciliation_invalid")
    cash_difference = broker_cash - ledger_cash
    mismatches: dict[str, dict[str, float]] = {}
    for stock in sorted(set(broker_positions) | set(ledger_positions)):
        broker_quantity = broker_positions.get(stock, 0.0)
        ledger_quantity = ledger_positions.get(stock, 0.0)
        if broker_quantity != ledger_quantity:
            mismatches[stock] = {"broker": broker_quantity, "ledger": ledger_quantity}
    status = "reconciled" if abs(cash_difference) <= cash_tolerance and not mismatches else "mismatch"
    core = {
        "schema_version": "broker-reconciliation-v1",
        "computed_by": "validation_program-v1",
        "statement_sha256": _file_hash(path),
        "ledger_sha256": _canonical_hash(dict(ledger_summary)),
        "asof": statement["asof"],
        "cash_difference": cash_difference,
        "cash_tolerance": float(cash_tolerance),
        "position_mismatches": mismatches,
        "status": status,
        "broker_authoritative": True,
    }
    return {**core, "artifact_sha256": _canonical_hash(core)}


def _normalise_positions(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise ValueError("positions must be a mapping")
    positions: dict[str, float] = {}
    for stock, quantity in value.items():
        numeric = float(quantity)
        if not math.isfinite(numeric) or numeric < 0:
            raise ValueError("position quantity invalid")
        if numeric:
            positions[str(stock)] = numeric
    return positions


def build_validation_report(
    *,
    precommitted_variants: Sequence[str],
    precommitted_folds: Sequence[str],
    variant_results: Mapping[str, Mapping[str, Any]],
    fold_results: Mapping[str, Mapping[str, Any]],
    returns: Sequence[float],
    weights: Sequence[Mapping[str, float]],
    cost_stress_bps: Sequence[float],
    capacity_inputs: Sequence[Mapping[str, float]] | None,
    maximum_adv_participation: float = 0.1,
) -> dict[str, Any]:
    reasons: list[str] = []
    expected_variants = sorted(set(precommitted_variants))
    expected_folds = sorted(set(precommitted_folds))
    if set(variant_results) != set(expected_variants):
        reasons.append("variant_missing" if set(expected_variants) - set(variant_results) else "late_variant_addition")
    if set(fold_results) != set(expected_folds):
        reasons.append("fold_missing" if set(expected_folds) - set(fold_results) else "late_fold_addition")
    numeric = _numeric_series(returns, minimum=1)
    if numeric is None:
        reasons.append("returns_invalid")
        series: list[float] = []
    else:
        series = numeric[0]
    if not cost_stress_bps:
        reasons.append("cost_stress_missing")
    capacity_curve = _capacity_curve(capacity_inputs or [], maximum_adv_participation)
    if not capacity_inputs or len(capacity_curve) != len(capacity_inputs):
        reasons.append("capacity_unknown")
    variants = [{"variant_id": key, **dict(variant_results.get(key, {"status": "missing"}))} for key in expected_variants]
    folds = [{"fold_id": key, **dict(fold_results.get(key, {"status": "missing"}))} for key in expected_folds]
    core = {
        "schema_version": "complete-validation-report-v1",
        "variants": variants,
        "folds": folds,
        "turnover": _turnover(weights),
        "maximum_drawdown": _maximum_drawdown(series),
        "tail_loss": _empirical_lower_tail(series, 0.05),
        "cost_stress": {
            _number_key(bps): statistics.fmean(series) - float(bps) / 10_000
            for bps in cost_stress_bps
        } if series else {},
        "capacity_curve": capacity_curve,
        "reasons": sorted(set(reasons)),
    }
    return {
        **core,
        "status": "evaluated" if not reasons else "not_evaluated",
        "artifact_sha256": _canonical_hash(core),
    }


def _number_key(value: float) -> str:
    numeric = float(value)
    return str(int(numeric)) if numeric.is_integer() else str(numeric)


def _turnover(weights: Sequence[Mapping[str, float]]) -> float:
    total = 0.0
    for previous, current in zip(weights, weights[1:]):
        assets = set(previous) | set(current)
        total += sum(abs(float(current.get(asset, 0)) - float(previous.get(asset, 0))) for asset in assets) / 2
    return total


def _maximum_drawdown(returns: Sequence[float]) -> float | None:
    if not returns:
        return None
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for value in returns:
        equity *= 1 + value
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1)
    return worst


def _empirical_lower_tail(returns: Sequence[float], probability: float) -> float | None:
    """Nearest-rank lower-tail observation (no optimistic interpolation)."""

    if not returns:
        return None
    ordered = sorted(returns)
    index = max(0, math.ceil(probability * len(ordered)) - 1)
    return ordered[index]


def _capacity_curve(inputs: Sequence[Mapping[str, float]], maximum: float) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    if not 0 < maximum <= 1:
        return output
    for item in sorted(inputs, key=lambda value: float(value.get("capital", 0))):
        try:
            capital = float(item["capital"])
            required = float(item["required_notional"])
            adv = float(item["adv"])
        except (KeyError, TypeError, ValueError):
            continue
        if adv <= 0 or required < 0 or capital < 0:
            continue
        participation = required / adv
        output.append(
            {
                "capital": capital,
                "participation": participation,
                "status": "within_limit" if participation < maximum else "at_limit" if participation == maximum else "over_limit",
            }
        )
    return output


def _validated_shadow_date(
    real_trading_day: bool, trading_date: str | None
) -> str | None:
    if not real_trading_day:
        return None
    if not trading_date:
        raise ValidationError("shadow_trading_date_required")
    try:
        normalized = datetime.strptime(str(trading_date), "%Y-%m-%d").date().isoformat()
        if not is_trading_day(normalized):
            raise ValidationError("shadow_not_trading_day")
        return normalized
    except CalendarCoverageError as exc:
        raise ValidationError("shadow_calendar_uncovered") from exc
    except ValueError as exc:
        if isinstance(exc, ValidationError):
            raise
        raise ValidationError("shadow_trading_date_invalid") from exc


class ShadowWindowRegistry:
    """Append-only shadow window bound to a versioned threshold artifact."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    @staticmethod
    def _load_thresholds(path: str | os.PathLike[str]) -> tuple[dict[str, Any], str]:
        try:
            config = json.loads(Path(path).read_text(encoding="utf-8"))
            shadow = config["shadow"]
            required = {
                "minimum_trading_days", "maximum_simulation_error",
                "auto_demotion_error", "maximum_manual_pilot_weight",
            }
            if not config.get("schema_version") or not required <= set(shadow):
                raise KeyError
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValidationError("threshold_config_invalid") from exc
        return config, _file_hash(path)

    def start(
        self,
        strategy_id: str,
        thresholds_path: str | os.PathLike[str],
        *,
        precommit_registry_path: str | os.PathLike[str],
        precommit_id: str,
        invocation_id: str,
        repo_root: str | os.PathLike[str],
        rules_path: str | os.PathLike[str],
        dataset_path: str | os.PathLike[str],
        expected_split: Mapping[str, Any],
        expected_variants: Sequence[str],
        expected_fold_ids: Sequence[str],
    ) -> dict[str, Any]:
        config, threshold_hash = self._load_thresholds(thresholds_path)
        precommit = self._verify_persisted_precommit(
            precommit_registry_path=precommit_registry_path,
            precommit_id=precommit_id,
            invocation_id=invocation_id,
            repo_root=repo_root,
            rules_path=rules_path,
            dataset_path=dataset_path,
            thresholds_path=thresholds_path,
            expected_split=expected_split,
            expected_variants=expected_variants,
            expected_fold_ids=expected_fold_ids,
        )
        with _locked_jsonl(self.path) as handle:
            records = _read_locked_records(handle)
            current = _latest_shadow(records, strategy_id)
            if (
                current is not None
                and current.get("thresholds_sha256") == threshold_hash
                and current.get("precommit_id") == precommit_id
                and current.get("precommit_record_sha256") == precommit.get("record_sha256")
            ):
                return current
            record = {
                "schema_version": "shadow-window-v1",
                "computed_by": "validation_program-v1",
                "strategy_id": strategy_id,
                "precommit_id": precommit_id,
                "precommit_record_sha256": precommit["record_sha256"],
                "precommit_invocation_id": precommit["invocation_id"],
                "shadow_invocation_id": invocation_id,
                "ancestor_commit": precommit["ancestor_commit"],
                "split": precommit["split"],
                "variants": precommit["variants"],
                "fold_ids": precommit["fold_ids"],
                "thresholds_sha256": threshold_hash,
                "thresholds": config,
                "status": "shadow",
                "observed_trading_days": 0,
                "observed_trading_dates": [],
                "reason": "shadow_started" if current is None else "shadow_window_reset",
                "recorded_at": _now(),
            }
            record["artifact_sha256"] = _canonical_hash(record)
            _append_locked(handle, record)
            return record

    def _verify_persisted_precommit(
        self,
        *,
        precommit_registry_path: str | os.PathLike[str],
        precommit_id: str,
        invocation_id: str,
        repo_root: str | os.PathLike[str],
        rules_path: str | os.PathLike[str],
        dataset_path: str | os.PathLike[str],
        thresholds_path: str | os.PathLike[str],
        expected_split: Mapping[str, Any],
        expected_variants: Sequence[str],
        expected_fold_ids: Sequence[str],
    ) -> dict[str, Any]:
        registry_path = Path(precommit_registry_path).resolve()
        if registry_path == self.path.resolve() or not invocation_id:
            raise ValidationError("precommit_registry_invalid")
        with _locked_jsonl(registry_path) as handle:
            records = _read_locked_records(handle)
        precommit = next(
            (
                record
                for record in records
                if record.get("record_type") == "precommit"
                and record.get("precommit_id") == precommit_id
            ),
            None,
        )
        if precommit is None:
            raise ValidationError("precommit_missing")
        if (
            precommit.get("invocation_id") == invocation_id
            or not precommit.get("process_instance_id")
            or precommit.get("process_instance_id") == _PROCESS_INSTANCE_ID
        ):
            raise ValidationError("same_run_reveal")
        if precommit.get("clean_tree") is not True:
            raise ValidationError("precommit_invalid")
        try:
            rules_hash = _file_hash(rules_path)
            dataset_hash = _file_hash(dataset_path)
            thresholds_hash = _file_hash(thresholds_path)
        except OSError as exc:
            raise ValidationError("artifact_unavailable") from exc
        if rules_hash != precommit.get("rules_sha256"):
            raise ValidationError("artifact_tampered", "rules hash changed")
        if dataset_hash != precommit.get("dataset_sha256"):
            raise ValidationError("artifact_tampered", "dataset hash changed")
        if thresholds_hash != precommit.get("thresholds_sha256"):
            raise ValidationError("artifact_tampered", "threshold hash changed")
        if dict(expected_split) != precommit.get("split"):
            raise ValidationError("split_mismatch")
        self._verify_complete_set(
            expected_variants, precommit.get("variants") or [], "variant"
        )
        self._verify_complete_set(
            expected_fold_ids, precommit.get("fold_ids") or [], "fold"
        )
        repository = Path(repo_root).resolve()
        ancestor = str(precommit.get("ancestor_commit") or "")
        ancestry = _git(
            repository, "merge-base", "--is-ancestor", ancestor, "HEAD", check=False
        )
        if not ancestor or ancestry.returncode != 0:
            raise ValidationError("ancestry_invalid")
        if _git(
            repository, "status", "--porcelain", "--untracked-files=all"
        ).stdout.strip():
            raise ValidationError("dirty_tree")
        return precommit

    @staticmethod
    def _verify_complete_set(
        expected: Sequence[str], persisted: Sequence[str], prefix: str
    ) -> None:
        expected_set = _unique_nonempty(expected, f"{prefix}_invalid")
        persisted_set = set(str(value) for value in persisted)
        if persisted_set - expected_set:
            raise ValidationError(f"{prefix}_missing")
        if expected_set - persisted_set:
            raise ValidationError(f"late_{prefix}_addition")

    def observe(
        self,
        strategy_id: str,
        thresholds_path: str | os.PathLike[str],
        *,
        simulation_error: float,
        real_trading_day: bool,
        trading_date: str | None = None,
        threshold_overrides: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if threshold_overrides:
            raise ValidationError("threshold_override")
        config, threshold_hash = self._load_thresholds(thresholds_path)
        if not math.isfinite(float(simulation_error)) or simulation_error < 0:
            raise ValidationError("shadow_observation_invalid")
        with _locked_jsonl(self.path) as handle:
            records = _read_locked_records(handle)
            current = _latest_shadow(records, strategy_id)
            if current is None:
                raise ValidationError("shadow_window_missing")
            if current.get("thresholds_sha256") != threshold_hash:
                raise ValidationError("shadow_window_reset")
            shadow = config["shadow"]
            observed_dates = current.get("observed_trading_dates")
            if not isinstance(observed_dates, list):
                raise ValidationError("shadow_registry_corrupt")
            normalized_date = _validated_shadow_date(real_trading_day, trading_date)
            counted_new_day = bool(
                normalized_date and normalized_date not in observed_dates
            )
            if counted_new_day:
                observed_dates = sorted([*observed_dates, normalized_date])
            days = len(observed_dates)
            if simulation_error >= float(shadow["auto_demotion_error"]):
                status, reason = "research_only", "auto_demoted"
            elif (
                days >= int(shadow["minimum_trading_days"])
                and simulation_error <= float(shadow["maximum_simulation_error"])
            ):
                status, reason = "eligible_for_manual_pilot", "shadow_thresholds_met"
            else:
                status, reason = "shadow", "shadow_window_incomplete"
            record = {
                "schema_version": "shadow-window-v1",
                "computed_by": "validation_program-v1",
                "strategy_id": strategy_id,
                "precommit_id": current.get("precommit_id"),
                "precommit_record_sha256": current.get("precommit_record_sha256"),
                "precommit_invocation_id": current.get("precommit_invocation_id"),
                "shadow_invocation_id": current.get("shadow_invocation_id"),
                "ancestor_commit": current.get("ancestor_commit"),
                "split": current.get("split"),
                "variants": current.get("variants"),
                "fold_ids": current.get("fold_ids"),
                "thresholds_sha256": threshold_hash,
                "thresholds": config,
                "status": status,
                "observed_trading_days": days,
                "observed_trading_dates": observed_dates,
                "trading_date": normalized_date,
                "counted_new_trading_day": counted_new_day,
                "simulation_error": float(simulation_error),
                "reason": reason,
                "recorded_at": _now(),
            }
            record["artifact_sha256"] = _canonical_hash(record)
            _append_locked(handle, record)
            return record


def _latest_shadow(records: Sequence[Mapping[str, Any]], strategy_id: str) -> dict[str, Any] | None:
    for record in reversed(records):
        if record.get("strategy_id") == strategy_id:
            return dict(record)
    return None


__all__ = [
    "DailyEvidenceRegistry",
    "OOSRegistry",
    "ShadowWindowRegistry",
    "ValidationError",
    "block_bootstrap_mean",
    "build_shadow_run_artifact",
    "build_validation_report",
    "build_walk_forward_folds",
    "compute_effective_samples",
    "compute_statistical_validation",
    "deflated_sharpe",
    "evaluate_empirical_gate",
    "fdr_benjamini_hochberg",
    "hac_mean_uncertainty",
    "load_validation_thresholds",
    "probability_of_backtest_overfitting",
    "reconcile_broker_statement",
    "verify_validation_artifact",
    "verify_oos_precommit_record",
]
