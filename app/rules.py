"""Pure matching and decision rules. No model or database code lives here."""

from collections.abc import Mapping, Sequence
from typing import Literal

import numpy as np

Gallery = Mapping[int, Sequence[np.ndarray]]
Decision = Literal["matched", "rejected", "ambiguous"]


def _normalised(vector: np.ndarray) -> np.ndarray:
    array = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(array))
    if norm == 0.0:
        return np.zeros_like(array)
    return array / norm


def _cosine_similarity(query: np.ndarray, vector: np.ndarray) -> float:
    candidate = _normalised(vector)
    return float(np.dot(query, candidate))


def match(
    embedding: np.ndarray, gallery: Gallery
) -> tuple[int | None, float | None, float | None]:
    """Return the best staff id, best score and second-best staff score.

    Multiple embeddings for one person are reduced to that person's best score.
    Empty galleries return ``(None, None, None)``.
    """

    query = _normalised(embedding)
    scores: dict[int, float] = {}

    for staff_id, vectors in gallery.items():
        if not vectors:
            continue
        scores[staff_id] = max(
            _cosine_similarity(query, vector) for vector in vectors
        )

    if not scores:
        return None, None, None

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best_staff_id, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else None
    return best_staff_id, best_score, second_score


def decide(
    best: float | None,
    second: float | None,
    threshold: float,
    margin: float,
) -> Decision:
    """Apply the threshold first, then the ambiguity margin."""

    if best is None or best < threshold:
        return "rejected"
    if second is not None and best - second < margin:
        return "ambiguous"
    return "matched"
