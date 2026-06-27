from __future__ import annotations


def interpolate(schedule: list[dict], x_key: str, y_key: str, x: float, default: float = 0.0) -> float:
    points = sorted(
        [(float(item[x_key]), float(item[y_key])) for item in schedule if item.get(x_key) is not None and item.get(y_key) is not None],
        key=lambda p: p[0],
    )
    if not points:
        return default
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        if x1 <= x <= x2:
            if x2 == x1:
                return y2
            ratio = (x - x1) / (x2 - x1)
            return y1 + (y2 - y1) * ratio
    return default
