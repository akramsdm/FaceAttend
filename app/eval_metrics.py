"""Pure FAR/FRR/threshold-sweep arithmetic for evaluation (no images/models)."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np


def _as_scores(values: Sequence[float]) -> np.ndarray:
    if not values:
        return np.array([], dtype=np.float64)
    return np.asarray(values, dtype=np.float64)


def far_frr_at_threshold(
    genuine: Sequence[float], impostor: Sequence[float], threshold: float
) -> dict[str, float | int]:
    g = _as_scores(genuine)
    i = _as_scores(impostor)
    t = float(threshold)

    genuine_accept = int(np.count_nonzero(g >= t)) if g.size else 0
    genuine_reject = int(np.count_nonzero(g < t)) if g.size else 0
    impostor_accept = int(np.count_nonzero(i >= t)) if i.size else 0
    impostor_reject = int(np.count_nonzero(i < t)) if i.size else 0

    far = (impostor_accept / i.size) if i.size else 0.0
    frr = (genuine_reject / g.size) if g.size else 0.0
    total = g.size + i.size
    accuracy = ((genuine_accept + impostor_reject) / total) if total else 0.0

    return {
        "threshold": t,
        "far": float(far),
        "frr": float(frr),
        "accuracy": float(accuracy),
        "genuine_accept": genuine_accept,
        "genuine_reject": genuine_reject,
        "impostor_accept": impostor_accept,
        "impostor_reject": impostor_reject,
        "genuine_count": int(g.size),
        "impostor_count": int(i.size),
    }


def threshold_grid(
    genuine: Sequence[float],
    impostor: Sequence[float],
    steps: int = 101,
    low: float | None = None,
    high: float | None = None,
) -> np.ndarray:
    g = _as_scores(genuine)
    i = _as_scores(impostor)
    values = np.concatenate([g, i]) if (g.size or i.size) else np.array([0.0, 1.0])
    lo = float(np.min(values)) if low is None else float(low)
    hi = float(np.max(values)) if high is None else float(high)
    if hi <= lo:
        hi = lo + 1e-6
    return np.linspace(lo, hi, max(2, int(steps)), dtype=np.float64)


def sweep_thresholds(
    genuine: Sequence[float],
    impostor: Sequence[float],
    steps: int = 101,
) -> list[dict[str, float | int]]:
    grid = threshold_grid(genuine, impostor, steps=steps)
    return [far_frr_at_threshold(genuine, impostor, float(t)) for t in grid]


def far_zero_operating_point(
    sweep: Sequence[dict[str, float | int]],
) -> dict[str, float | int] | None:
    """Lowest threshold (on the sweep grid) where FAR is 0 on this sample.

    Returns that row including its FRR, or None if FAR never reaches 0.
    """
    candidates = [row for row in sweep if float(row["far"]) == 0.0]
    if not candidates:
        return None
    return min(candidates, key=lambda row: float(row["threshold"]))
