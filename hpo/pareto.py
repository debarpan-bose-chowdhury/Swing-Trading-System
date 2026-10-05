"""Feasible front and hypervolume of the two objectives: f1 = fold-CVaR post-tax CAGR (maximise), f2 = drawdown depth (minimise)."""

Point = tuple[float, float]


def dominates(a: Point, b: Point) -> bool:
    return a[0] >= b[0] and a[1] <= b[1] and a != b


def front(points: list[Point]) -> list[int]:
    """Indices of the non-dominated points, in input order (equal points are all kept)."""
    out, lowest_f2 = [], float("inf")
    for i in sorted(range(len(points)), key=lambda i: (-points[i][0], points[i][1])):
        if points[i][1] < lowest_f2 or (out and points[out[-1]] == points[i]):
            out.append(i)
            lowest_f2 = min(lowest_f2, points[i][1])
    return sorted(out)


def hypervolume(points: list[Point], ref: Point) -> float:
    """Area dominated by the points inside the reference corner (f1 above ref[0], f2 below ref[1])."""
    pts = sorted({p for p in points if p[0] > ref[0] and p[1] < ref[1]}, key=lambda p: -p[0])
    area, lowest_f2 = 0.0, ref[1]
    for k, (f1, f2) in enumerate(pts):
        lowest_f2 = min(lowest_f2, f2)
        next_f1 = pts[k + 1][0] if k + 1 < len(pts) else ref[0]
        area += (f1 - next_f1) * (ref[1] - lowest_f2)
    return area


def select(rows: list[dict], rule: str) -> dict:
    """One configuration of the feasible front. rows: {"trial", "f1", "f2", "calmar"}. calmar = highest Calmar ratio; knee = the point farthest
    from the straight line between the two extremes of the front, after scaling both objectives to 0..1 (the best trade-off by shape)."""
    if not rows:
        raise ValueError("no feasible trial on the front")
    if rule == "calmar":
        return max(rows, key=lambda r: (r["calmar"] if r["calmar"] is not None else float("-inf"), r["f1"]))
    if rule != "knee":
        raise ValueError("select rule must be calmar or knee")
    if len(rows) < 3:
        return max(rows, key=lambda r: r["f1"] - r["f2"])
    lo1, hi1 = min(r["f1"] for r in rows), max(r["f1"] for r in rows)
    lo2, hi2 = min(r["f2"] for r in rows), max(r["f2"] for r in rows)
    pts = [(((r["f1"] - lo1) / (hi1 - lo1)) if hi1 > lo1 else 0.0, ((r["f2"] - lo2) / (hi2 - lo2)) if hi2 > lo2 else 0.0) for r in rows]
    a = min(pts, key=lambda p: p[0])
    b = max(pts, key=lambda p: p[0])
    dx, dy = b[0] - a[0], b[1] - a[1]
    norm = (dx * dx + dy * dy) ** 0.5 or 1.0
    # the point farthest below the chord (the same CAGR for less depth), from the low-CAGR end to the high-CAGR end
    return rows[max(range(len(rows)), key=lambda i: ((pts[i][0] - a[0]) * dy - (pts[i][1] - a[1]) * dx) / norm)]
