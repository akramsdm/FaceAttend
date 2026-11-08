import numpy as np
import pytest

from app.spoof import confirm_head_turn, nose_offset

# YuNet order: right eye, left eye, nose tip, right mouth, left mouth
# Eyes centered at x=50; nose at midpoint => offset 0
FACE_CENTER = np.array([[40, 40], [60, 40], [50, 55], [42, 65], [58, 65]], dtype=np.float32)


def shift_nose(face, dx):
    f = face.copy()
    f[2, 0] += dx
    return f


def test_nose_offset_center_is_zero():
    assert nose_offset(FACE_CENTER) == pytest.approx(0.0, abs=1e-6)


def test_nose_offset_right_positive_left_negative():
    assert nose_offset(shift_nose(FACE_CENTER, 5)) > 0
    assert nose_offset(shift_nose(FACE_CENTER, -5)) < 0


def test_confirm_head_turn_left():
    after = shift_nose(FACE_CENTER, 4)  # more positive (image-right) = user turned left
    assert confirm_head_turn(FACE_CENTER, after, "left") is True
    assert confirm_head_turn(FACE_CENTER, after, "right") is False


def test_confirm_head_turn_right():
    after = shift_nose(FACE_CENTER, -4)  # more negative = user turned right
    assert confirm_head_turn(FACE_CENTER, after, "right") is True
    assert confirm_head_turn(FACE_CENTER, after, "left") is False


def test_confirm_head_turn_insufficient_movement_rejected():
    tiny = shift_nose(FACE_CENTER, 0.5)
    assert confirm_head_turn(FACE_CENTER, tiny, "left") is False
    assert confirm_head_turn(FACE_CENTER, tiny, "right") is False


def test_confirm_head_turn_invalid_direction():
    after = shift_nose(FACE_CENTER, 5)
    assert confirm_head_turn(FACE_CENTER, after, "up") is False
