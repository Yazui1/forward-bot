from __future__ import annotations

from bisect import bisect_left


def linear_interpolate(points: list[tuple[float, float]], x: float) -> float:
    if not points:
        raise ValueError("points must not be empty")
    points = sorted(points, key=lambda p: p[0])
    xs = [p[0] for p in points]
    idx = bisect_left(xs, x)
    if idx <= 0:
        return points[0][1]
    if idx >= len(points):
        return points[-1][1]
    x0, y0 = points[idx - 1]
    x1, y1 = points[idx]
    if x1 == x0:
        return y1
    ratio = (x - x0) / (x1 - x0)
    return y0 + (y1 - y0) * ratio
