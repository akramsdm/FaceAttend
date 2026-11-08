"""Landmark-based head-turn challenge (optional spoof check).

YuNet landmarks are 5 (x, y) points: right eye, left eye, nose tip, right mouth
corner, left mouth corner (OpenCV FaceDetectorYN order).
"""
from __future__ import annotations

import numpy as np

# Minimum normalized nose offset change (relative to inter-eye distance) to accept a turn.
MIN_TURN_DELTA = 0.08


def _eye_midpoint(landmarks: np.ndarray) -> np.ndarray:
    lm = np.asarray(landmarks, dtype=np.float32).reshape(5, 2)
    return (lm[0] + lm[1]) / 2.0


def _inter_eye_distance(landmarks: np.ndarray) -> float:
    lm = np.asarray(landmarks, dtype=np.float32).reshape(5, 2)
    d = float(np.linalg.norm(lm[1] - lm[0]))
    return d if d > 1e-6 else 1.0


def nose_offset(landmarks: np.ndarray) -> float:
    """Nose x relative to eye midpoint, normalized by inter-eye distance.

    Positive means the nose is to the image-right of the eye midpoint.
    """
    lm = np.asarray(landmarks, dtype=np.float32).reshape(5, 2)
    nose = lm[2]
    mid = _eye_midpoint(lm)
    return float((nose[0] - mid[0]) / _inter_eye_distance(lm))


def confirm_head_turn(
    before: np.ndarray,
    after: np.ndarray,
    requested: str,
    min_delta: float = MIN_TURN_DELTA,
) -> bool:
    """Return True if `after` moved in the requested direction vs `before`.

    `requested` is 'left' or 'right' (user's head turn). In a non-mirrored
    camera frame, turning the head to the user's right moves facial features
    toward image-left (more negative nose offset); turning to the user's left
    moves toward image-right (more positive). This matches the kiosk asking the
    user to turn their head left/right.
    """
    if requested not in ("left", "right"):
        return False
    delta = nose_offset(after) - nose_offset(before)
    if requested == "left":
        return delta >= min_delta
    return delta <= -min_delta
