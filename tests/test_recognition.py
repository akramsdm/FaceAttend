from pathlib import Path

import cv2
import numpy as np
import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.models import FaceEmbedding, Staff, delete_staff_data
from app.recognition import (
    DETECTOR_MODEL,
    RECOGNIZER_MODEL,
    GalleryCache,
    Recognizer,
)

FIXTURES = Path(__file__).parent / "fixtures"
ONE_FACE = FIXTURES / "one_face.jpg"
NO_FACE = FIXTURES / "no_face.jpg"
TWO_FACES = FIXTURES / "two_faces.jpg"


def read_fixture(path: Path) -> bytes:
    if not path.exists():
        pytest.skip(f"fixture not added: {path}")
    return path.read_bytes()


@pytest.fixture(scope="module")
def recognizer() -> Recognizer:
    if not DETECTOR_MODEL.exists() or not RECOGNIZER_MODEL.exists():
        pytest.skip("models not downloaded; run scripts/download_models.py")
    return Recognizer(min_face_width=1, blur_threshold=0.0)


def test_analyse_frame_one_face(recognizer: Recognizer) -> None:
    result = recognizer.analyse_frame(read_fixture(ONE_FACE))

    assert result.status == "ok"
    assert result.face_count == 1
    assert result.face is not None
    assert result.box is not None
    assert result.landmarks is not None
    assert result.landmarks.shape == (5, 2)
    assert result.frame.shape[2] == 3


def test_analyse_frame_no_face(recognizer: Recognizer) -> None:
    result = recognizer.analyse_frame(read_fixture(NO_FACE))

    assert result.status == "no_face"
    assert result.face_count == 0
    assert result.face is None
    assert result.box is None
    assert result.landmarks is None


def test_analyse_frame_multiple_faces(recognizer: Recognizer) -> None:
    result = recognizer.analyse_frame(read_fixture(TWO_FACES))

    assert result.status == "multiple_faces"
    assert result.face_count >= 2
    assert result.face is None
    assert result.box is None


def test_analyse_frame_blank_image_has_no_face(recognizer: Recognizer) -> None:
    blank = np.zeros((240, 320, 3), dtype=np.uint8)
    encoded, buffer = cv2.imencode(".jpg", blank)
    assert encoded

    result = recognizer.analyse_frame(buffer.tobytes())

    assert result.status == "no_face"
    assert result.face_count == 0


def test_analyse_frame_rejects_undecodable_bytes(recognizer: Recognizer) -> None:
    with pytest.raises(ValueError, match="could not decode image"):
        recognizer.analyse_frame(b"not an image")


def test_min_face_width_is_configurable(
    recognizer: Recognizer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recognizer, "min_face_width", 1_000_000)
    result = recognizer.analyse_frame(read_fixture(ONE_FACE))

    assert result.status == "too_small"
    assert result.face is not None


def test_blur_threshold_is_configurable(
    recognizer: Recognizer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recognizer, "blur_threshold", 1e12)
    result = recognizer.analyse_frame(read_fixture(ONE_FACE))

    assert result.status == "blurry"
    assert result.face is not None


def test_embed_returns_a_normalised_vector(recognizer: Recognizer) -> None:
    result = recognizer.analyse_frame(read_fixture(ONE_FACE))
    assert result.status == "ok"
    assert result.face is not None

    vector = recognizer.embed(result.frame, result.face)

    assert vector.shape == (128,)
    assert vector.dtype == np.float32
    assert np.linalg.norm(vector) == pytest.approx(1.0, abs=1e-5)


def test_gallery_cache_skips_inactive_staff_and_refreshes(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        active = Staff(staff_code="A1", name="Active Person")
        inactive = Staff(staff_code="I1", name="Inactive Person", active=False)
        session.add_all([active, inactive])
        session.flush()
        session.add_all(
            [
                FaceEmbedding(
                    staff_id=active.id,
                    vector=np.array([1.0, 0.0], dtype=np.float32),
                    model_name="SFace",
                    model_version="2021dec",
                ),
                FaceEmbedding(
                    staff_id=inactive.id,
                    vector=np.array([0.0, 1.0], dtype=np.float32),
                    model_name="SFace",
                    model_version="2021dec",
                ),
            ]
        )
        session.commit()
        active_id = active.id

    cache = GalleryCache(session_factory)
    assert set(cache.snapshot()) == {active_id}

    best_id, best_score, second_score = cache.match_embedding(
        np.array([1.0, 0.0], dtype=np.float32)
    )
    assert best_id == active_id
    assert best_score == pytest.approx(1.0)
    assert second_score is None

    with session_factory() as session:
        second = Staff(staff_code="A2", name="Second Person")
        session.add(second)
        session.flush()
        session.add(
            FaceEmbedding(
                staff_id=second.id,
                vector=np.array([0.0, 1.0], dtype=np.float32),
                model_name="SFace",
                model_version="2021dec",
            )
        )
        session.commit()
        second_id = second.id

    cache.refresh()
    assert set(cache.snapshot()) == {active_id, second_id}

    with session_factory() as session:
        assert delete_staff_data(session, active_id) is True

    cache.refresh()
    assert active_id not in cache.snapshot()
    assert second_id in cache.snapshot()
