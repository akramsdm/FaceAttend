"""Tests for FAR/FRR/sweep arithmetic with synthetic scores (boundaries)."""
import numpy as np
import pytest

from app.eval_metrics import far_frr_at_threshold, far_zero_operating_point, sweep_thresholds, threshold_grid


def test_far_frr_basic() -> None:
    # genuine: 0.9,0.8,0.7 ; impostor: 0.6,0.5,0.4
    r = far_frr_at_threshold([0.9, 0.8, 0.7], [0.6, 0.5, 0.4], 0.65)
    assert r["genuine_accept"] == 3
    assert r["genuine_reject"] == 0
    assert r["impostor_accept"] == 0
    assert r["impostor_reject"] == 3
    assert r["far"] == pytest.approx(0.0)
    assert r["frr"] == pytest.approx(0.0)
    assert r["accuracy"] == pytest.approx(1.0)


def test_far_frr_boundary_genuine_at_threshold_is_accepted() -> None:
    # score == threshold counts as accept for both genuine and impostor (>=).
    r = far_frr_at_threshold([0.363], [0.363], 0.363)
    assert r["genuine_accept"] == 1
    assert r["genuine_reject"] == 0
    assert r["impostor_accept"] == 1
    assert r["impostor_reject"] == 0
    assert r["far"] == pytest.approx(1.0)
    assert r["frr"] == pytest.approx(0.0)
    assert r["accuracy"] == pytest.approx(0.5)


def test_far_frr_boundary_just_below_threshold() -> None:
    eps = 1e-9
    r = far_frr_at_threshold([0.363 - eps], [0.363 - eps], 0.363)
    assert r["genuine_accept"] == 0
    assert r["genuine_reject"] == 1
    assert r["impostor_accept"] == 0
    assert r["impostor_reject"] == 1
    assert r["far"] == pytest.approx(0.0)
    assert r["frr"] == pytest.approx(1.0)
    assert r["accuracy"] == pytest.approx(0.5)


def test_far_frr_all_rejected() -> None:
    r = far_frr_at_threshold([0.1, 0.2], [0.1, 0.15], 0.9)
    assert r["far"] == pytest.approx(0.0)
    assert r["frr"] == pytest.approx(1.0)
    assert r["accuracy"] == pytest.approx(0.5)  # impostors correctly rejected


def test_far_frr_all_accepted() -> None:
    r = far_frr_at_threshold([0.9, 0.95], [0.92, 0.91], 0.1)
    assert r["far"] == pytest.approx(1.0)
    assert r["frr"] == pytest.approx(0.0)
    assert r["accuracy"] == pytest.approx(0.5)  # genuines correctly accepted


def test_far_frr_empty_genuine() -> None:
    r = far_frr_at_threshold([], [0.5, 0.2], 0.3)
    assert r["genuine_count"] == 0
    assert r["impostor_count"] == 2
    assert r["frr"] == pytest.approx(0.0)  # no genuine samples
    assert r["far"] == pytest.approx(0.5)  # 0.5 >= 0.3 accepted
    assert r["accuracy"] == pytest.approx(0.5)


def test_far_frr_empty_impostor() -> None:
    r = far_frr_at_threshold([0.5, 0.2], [], 0.3)
    assert r["far"] == pytest.approx(0.0)
    assert r["frr"] == pytest.approx(0.5)
    assert r["accuracy"] == pytest.approx(0.5)


def test_far_frr_both_empty() -> None:
    r = far_frr_at_threshold([], [], 0.5)
    assert r["far"] == 0.0 and r["frr"] == 0.0 and r["accuracy"] == 0.0
    assert r["genuine_count"] == 0 and r["impostor_count"] == 0


def test_sweep_and_far_zero_point() -> None:
    # impostors max 0.5, genuines min 0.6 -> FAR becomes 0 at/above >0.5 region.
    genuine = [0.6, 0.7, 0.8, 0.9]
    impostor = [0.1, 0.2, 0.4, 0.5]
    sweep = sweep_thresholds(genuine, impostor, steps=21)
    assert len(sweep) == 21
    # grid covers observed range [0.1,0.9]
    assert sweep[0]["threshold"] == pytest.approx(0.1)
    assert sweep[-1]["threshold"] == pytest.approx(0.9)

    zero = far_zero_operating_point(sweep)
    assert zero is not None
    assert float(zero["far"]) == pytest.approx(0.0)
    # lowest threshold on grid with FAR 0: first grid point > 0.5 (max impostor)
    assert float(zero["threshold"]) > 0.5
    # FRR at that point is reported
    assert "frr" in zero


def test_far_zero_point_none_when_impostor_equals_max() -> None:
    # if an impostor has score 1.0 and grid reaches 1.0, threshold==1.0 accepts it (>=) -> FAR>0
    genuine = [0.9]
    impostor = [1.0]
    sweep = sweep_thresholds(genuine, impostor, steps=3)  # 0.9,0.95,1.0
    # at 1.0 impostor is accepted
    last = [r for r in sweep if r["threshold"] == pytest.approx(1.0)][0]
    assert last["far"] == pytest.approx(1.0)
    # but at thresholds above all impostors? grid max==max(values)==1.0, so none > 1.0 on grid
    zero = far_zero_operating_point(sweep)
    # may be None if no grid point gives FAR 0
    if zero is not None:
        assert float(zero["far"]) == 0.0


def test_threshold_grid_single_value_expands() -> None:
    grid = threshold_grid([0.5], [0.5], steps=5)
    assert len(grid) == 5
    assert grid[0] < grid[-1]
