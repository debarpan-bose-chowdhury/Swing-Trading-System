"""The terminal progress line of a running study (one line, updated per finished trial)."""

import sys
import time


def clock(seconds: float | None) -> str:
    if seconds is None:
        return "--"
    s = int(seconds)
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def line(st: dict) -> str:
    best = f"{st['bestCagr'] * 100:.1f}%/{-st['bestDepth'] * 100:.1f}%" if st.get("bestCagr") is not None else "--"
    return (f"[{st['name']}] {st['done']}/{st['planned']}  feasible {st['feasible']}  fail {st['fail']}  best {best}  HV {st['hypervolume']:.4f}  "
            f"{st['trialsPerHour']:.0f}/h  {clock(st['elapsedS'])} (eta {clock(st['etaS'])})  N_eff {st['effectiveN']:.0f}/{st['cap']}")


class Progress:
    def __init__(self, stream=None, every: float = 0.5):
        self.stream, self.every, self.last = stream or sys.stderr, every, 0.0
        self.tty = hasattr(self.stream, "isatty") and self.stream.isatty()

    def update(self, st: dict, final: bool = False) -> None:
        now = time.time()
        if not final and now - self.last < self.every:
            return
        self.last = now
        text = line(st)
        self.stream.write(("\r" + text + "\x1b[K" + ("\n" if final else "")) if self.tty else text + "\n")
        self.stream.flush()
