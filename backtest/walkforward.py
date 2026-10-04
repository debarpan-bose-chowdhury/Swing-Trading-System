"""Evaluation windows: the holdout carved out first, then purged rolling (decision) and anchored (cross-check) folds.

Everything is in index trading days. The purge is in trading days because the selector's look-backs are: the effective purge is
the larger of backtest.json walkforward.purgeDays and the longest look-back any allowed parameter can use (params.required_purge).
Layout: [ start ... tuning_end ] purge [ holdout_start ... last ]. No tuning run may read past tuning_end; the holdout is scored
once, for one parameter set, and the marker file refuses a second, different set.
"""

import json
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


class HoldoutRead(Exception):
    """A tuning run asked for data in the holdout, or the holdout was already scored for another parameter set."""


@dataclass(frozen=True)
class Fold:
    kind: str  # "rolling" or "anchored"
    index: int
    train: tuple[str, str]
    test: tuple[str, str]


class Windows:
    def __init__(self, index_dates: list[str], start: str, wf: dict, holdout_years: int, required_purge: int):
        self.dates, self.wf = list(index_dates), wf
        self.purge = max(wf["purgeDays"], required_purge)
        self.start, self.last = start, self.dates[-1]
        h = self._first_on_or_after(str(pd.Timestamp(self.last) - pd.DateOffset(years=holdout_years))[:10])
        self.holdout_start = self.dates[h]
        self.tuning_end = self.dates[h - self.purge - 1]
        if self.tuning_end <= start:
            raise ValueError(f"no tuning data: start {start}, holdout from {self.holdout_start}, purge {self.purge} sessions")

    def _first_on_or_after(self, day: str) -> int:
        i = bisect_left(self.dates, day)
        if i >= len(self.dates):
            raise ValueError(f"{day} is after the last index date {self.last}")
        return i

    def _span(self, first: int, years: int) -> tuple[int, int]:
        """Positions (first, last) of a window of `years` calendar years starting at position `first`."""
        end = bisect_left(self.dates, str(pd.Timestamp(self.dates[first]) + pd.DateOffset(years=years))[:10]) - 1
        return first, end

    def _build(self, kind: str) -> list[Fold]:
        wf, folds, limit = self.wf, [], bisect_left(self.dates, self.tuning_end)
        k = 0
        while True:
            shift = k * wf["stepYears"]
            t0 = self._first_on_or_after(self.start if kind == "anchored" else str(pd.Timestamp(self.start) + pd.DateOffset(years=shift))[:10])
            _, t1 = self._span(t0, wf["trainYears"] + (shift if kind == "anchored" else 0))
            first_test = t1 + self.purge + 1
            if folds and kind == "rolling":  # session counts differ a little between years: never let two tests share a day
                first_test = max(first_test, bisect_left(self.dates, folds[-1].test[1]) + 1)
            if first_test >= len(self.dates):
                return folds
            a, b = self._span(first_test, wf["testYears"])
            if b > limit:
                return folds
            folds.append(Fold(kind, k, (self.dates[t0], self.dates[t1]), (self.dates[a], self.dates[b])))
            k += 1

    def rolling(self) -> list[Fold]:
        return self._build("rolling")

    def anchored(self) -> list[Fold]:
        return self._build("anchored")

    def check_tuning(self, start: str, end: str) -> None:
        """Raise HoldoutRead unless [start, end] stays inside the tuning region."""
        if end > self.tuning_end or start > end:
            raise HoldoutRead(f"{start}..{end} reaches past the tuning region (ends {self.tuning_end}); the holdout starts {self.holdout_start}")

    def holdout(self, marker: Path, params_key: str) -> tuple[str, str]:
        """The holdout window, once: a second call for a different parameter set raises."""
        if marker.exists():
            done = json.loads(marker.read_text(encoding="utf-8"))
            if done["params"] != params_key:
                raise HoldoutRead(f"the holdout was already scored for {done['params'][:60]}...; it is scored once, for one parameter set")
        else:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"params": params_key, "window": [self.holdout_start, self.last]}), encoding="utf-8")
        return self.holdout_start, self.last
