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
