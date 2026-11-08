import numpy as np
import pytest

from app.rules import decide, match


def vector_with_similarity(similarity: float) -> np.ndarray:
    """A unit vector whose cosine similarity to [1, 0] is the given value."""

    return np.array(
        [similarity, np.sqrt(max(0.0, 1.0 - similarity**2))], dtype=np.float32
    )


def test_match_empty_gallery() -> None:
    best_id, best_score, second_score = match(
        np.array([1.0, 0.0], dtype=np.float32), {}
    )

    assert best_id is None
    assert best_score is None
    assert second_score is None
    assert decide(best_score, second_score, 0.363, 0.05) == "rejected"


def test_match_single_person_uses_best_embedding() -> None:
    best_id, best_score, second_score = match(
        np.array([1.0, 0.0], dtype=np.float32),
        {
            7: [
                np.array([0.0, 1.0], dtype=np.float32),
                np.array([1.0, 0.0], dtype=np.float32),
            ]
        },
    )

    assert best_id == 7
    assert best_score == pytest.approx(1.0)
    assert second_score is None
    assert decide(best_score, second_score, 0.363, 0.05) == "matched"


def test_match_reduces_each_person_to_their_best_score() -> None:
    best_id, best_score, second_score = match(
        np.array([1.0, 0.0], dtype=np.float32),
        {
            1: [vector_with_similarity(0.4), vector_with_similarity(0.95)],
            2: [vector_with_similarity(0.9)],
        },
    )

    assert best_id == 1
    assert best_score == pytest.approx(0.95, abs=1e-6)
    assert second_score == pytest.approx(0.9, abs=1e-6)


def test_match_returns_second_best_staff() -> None:
    best_id, best_score, second_score = match(
        np.array([1.0, 0.0], dtype=np.float32),
        {1: [vector_with_similarity(0.7)], 2: [vector_with_similarity(0.6)]},
    )

    assert best_id == 1
    assert best_score == pytest.approx(0.7, abs=1e-6)
    assert second_score == pytest.approx(0.6, abs=1e-6)


def test_match_ignores_people_with_no_embeddings() -> None:
    best_id, best_score, second_score = match(
        np.array([1.0, 0.0], dtype=np.float32),
        {1: [], 2: [vector_with_similarity(0.8)]},
    )

    assert best_id == 2
    assert best_score == pytest.approx(0.8, abs=1e-6)
    assert second_score is None


def test_decide_at_exact_threshold_is_matched() -> None:
    assert decide(0.363, None, 0.363, 0.05) == "matched"
    assert decide(0.3629, None, 0.363, 0.05) == "rejected"


def test_decide_ambiguous_inside_margin() -> None:
    assert decide(0.90, 0.86, 0.363, 0.05) == "ambiguous"


def test_decide_at_exact_margin_is_matched() -> None:
    assert decide(0.90, 0.85, 0.363, 0.05) == "matched"


def test_decide_rejects_missing_best_score() -> None:
    assert decide(None, None, 0.363, 0.05) == "rejected"
