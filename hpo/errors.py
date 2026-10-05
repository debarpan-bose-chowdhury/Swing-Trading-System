"""Exit-code exceptions: the CLI maps them to 1 (failed), 2 (busy) and 3 (refused: gate, cap, holdout, changed inputs)."""


class Failed(Exception):
    """The run cannot continue (exit 1)."""


class Busy(Exception):
    """Another run holds the lock (exit 2)."""


class Refusal(Exception):
    """A rule refuses the run: effective-N cap, changed inputs on resume, holdout already scored (exit 3)."""
