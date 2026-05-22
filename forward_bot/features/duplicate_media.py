from __future__ import annotations

import math
from io import BytesIO
from typing import Iterable

from PIL import Image


def compute_phash(image_bytes: bytes) -> int:
    with Image.open(BytesIO(image_bytes)) as image:
        image = image.convert("L").resize((32, 32), Image.LANCZOS)
        pixels = list(image.getdata())
    matrix = [pixels[i * 32:(i + 1) * 32] for i in range(32)]
    dct_vals = _dct_2d(matrix, 8)
    flat = [dct_vals[r][c] for r in range(8) for c in range(8)]
    median = _median([v for i, v in enumerate(flat) if i != 0])
    bits = 0
    for value in flat:
        bits = (bits << 1) | (1 if value > median else 0)
    return bits


def similarity(hash_a: int, hash_b: int) -> float:
    distance = (hash_a ^ hash_b).bit_count()
    return 1.0 - (distance / 64.0)


def has_similar_hash(target: int, existing: Iterable[int], threshold: float) -> bool:
    for item in existing:
        if similarity(target, item) >= threshold:
            return True
    return False


def _dct_2d(matrix: list[list[int]], size: int) -> list[list[float]]:
    n = 32
    result = [[0.0 for _ in range(size)] for _ in range(size)]
    for u in range(size):
        for v in range(size):
            cu = 1.0 / math.sqrt(2.0) if u == 0 else 1.0
            cv = 1.0 / math.sqrt(2.0) if v == 0 else 1.0
            total = 0.0
            for x in range(n):
                for y in range(n):
                    total += (
                        matrix[x][y]
                        * math.cos(((2 * x + 1) * u * math.pi) / (2 * n))
                        * math.cos(((2 * y + 1) * v * math.pi) / (2 * n))
                    )
            result[u][v] = 0.25 * cu * cv * total
    return result


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    mid = len(sorted_vals) // 2
    if len(sorted_vals) % 2 == 1:
        return float(sorted_vals[mid])
    return float(sorted_vals[mid - 1] + sorted_vals[mid]) / 2.0
