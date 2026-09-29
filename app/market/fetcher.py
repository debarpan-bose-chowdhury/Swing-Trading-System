"""yfinance access shared by Migrator and Updator: batching, throttle, retries, block pauses."""

import logging
import random
import re
import time

import pandas as pd
import yfinance as yf

from app.market.common import COLS

PRICE = COLS[2:]
EXTRA = ["Dividends", "Splits"]
BLOCK = re.compile(r"\b429\b|\b999\b|too many requests|rate ?limit", re.I)


class Blocked(Exception):
    """Yahoo rate-limited the request."""


class _Capture(logging.Handler):
    """yfinance swallows per-ticker errors (incl. rate limits) into its log; listen for blocks there."""

    blocked = False

    def emit(self, record: logging.LogRecord) -> None:
        self.blocked = self.blocked or bool(BLOCK.search(record.getMessage()))


def tidy(raw: pd.DataFrame | None, symbols: list[str]) -> pd.DataFrame:
    """yfinance (Ticker, Field) frame -> long frame of COLS + Dividends + Splits; all-empty rows dropped."""
    out = [pd.DataFrame(columns=COLS + EXTRA)]
    if raw is not None and not raw.empty:
        if not isinstance(raw.columns, pd.MultiIndex):
            raw.columns = pd.MultiIndex.from_product([[symbols[0].upper()], raw.columns])
        present = set(raw.columns.get_level_values(0))
        for sym in symbols:
            if sym.upper() not in present:
                continue
            d = raw[sym.upper()].rename(columns={"Adj Close": "AdjClose", "Stock Splits": "Splits"})
            d = d.reindex(columns=PRICE + EXTRA).dropna(how="all", subset=PRICE)
            d = d.fillna({"Dividends": 0, "Splits": 0})
            d["Date"], d["Ticker"] = pd.to_datetime(d.index).strftime("%Y-%m-%d"), sym
            out.append(d[COLS + EXTRA])
    return pd.concat(out, ignore_index=True)


class Fetcher:
    def __init__(self, cfg: dict, log: logging.Logger):
        self.c, self.log = cfg["fetch"], log
        self.last_end: float | None = None
        self.window_start, self.used = time.monotonic(), 0
        self.pauses, self.exhausted = 0, False

    def _yf(self, symbols: list[str], **kw) -> pd.DataFrame:
        capture, lg = _Capture(), logging.getLogger("yfinance")
        lg.addHandler(capture)
        try:
            raw = yf.download(
                symbols, interval="1d", auto_adjust=False, actions=True, group_by="ticker",
                threads=False, progress=False, **kw,
            )
        except Exception as e:
            if BLOCK.search(f"{type(e).__name__} {e}"):
                raise Blocked from e
            raise
        finally:
            lg.removeHandler(capture)
        if capture.blocked:
            raise Blocked
        return tidy(raw, symbols)

    def _wait(self, n: int) -> None:
        """Honour the batch gap (from the end of the previous call) and the hourly request budget."""
        if self.last_end is not None:
            time.sleep(max(0.0, self.c["batchGapSeconds"] - (time.monotonic() - self.last_end)))
        elapsed = time.monotonic() - self.window_start
        if elapsed >= 3600:
            self.window_start, self.used = time.monotonic(), 0
        elif self.used + n > self.c["hourlyRequestBudget"]:
            self.log.info("Hourly request budget spent; waiting %.0fs", 3600 - elapsed)
            time.sleep(3600 - elapsed)
            self.window_start, self.used = time.monotonic(), 0
        self.used += n

    def fetch(self, symbols: list[str], **kw) -> pd.DataFrame | None:
        """One throttled request. None if it failed after retries or the block-pause budget is spent."""
        if self.exhausted:
            return None
        attempt = 0
        while True:
            self._wait(len(symbols))
            try:
                return self._yf(symbols, **kw)
            except Blocked:
                if self.pauses >= self.c["maxBlockPausesPerRun"]:
                    self.exhausted = True
                    self.log.error("Still rate-limited after %d pauses; deferring the rest to the next run", self.pauses)
                    return None
                self.pauses += 1
                self.log.warning("Rate-limited; pause %d/%d for %d min", self.pauses, self.c["maxBlockPausesPerRun"], self.c["blockPauseMinutes"])
                time.sleep(self.c["blockPauseMinutes"] * 60)
            except Exception as e:
                if attempt >= self.c["maxRetries"]:
                    self.log.error("Fetch of %s failed after retries: %r", symbols[:3], e)
                    return None
                backoff = self.c["backoffSeconds"]
                time.sleep(backoff[min(attempt, len(backoff) - 1)] + random.SystemRandom().random())
                attempt += 1
            finally:
                self.last_end = time.monotonic()

    def batches(self, symbols: list[str], size: int, **kw):
        """Yield (chunk, frame); frame is None for a chunk that failed or was deferred."""
        for i in range(0, len(symbols), size):
            chunk = symbols[i : i + size]
            yield chunk, self.fetch(chunk, **kw)
