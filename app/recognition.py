"""Face detection, quality checks, embedding and the in-memory gallery cache."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Literal

import cv2
import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import rules
from app.config import get_settings
from app.db import SessionLocal
from app.models import FaceEmbedding, Staff

ROOT = Path(__file__).resolve().parents[1]
DETECTOR_MODEL = ROOT / "models" / "face_detection_yunet_2023mar.onnx"
RECOGNIZER_MODEL = ROOT / "models" / "face_recognition_sface_2021dec.onnx"
MODEL_NAME = "SFace"
MODEL_VERSION = "2021dec"

AnalysisStatus = Literal[
    "ok", "no_face", "multiple_faces", "too_small", "blurry"
]
MatchResult = tuple[int | None, float | None, float | None]


@dataclass(frozen=True)
class FrameAnalysis:
    status: AnalysisStatus
    frame: np.ndarray
    face_count: int
    face: np.ndarray | None = None
    box: tuple[int, int, int, int] | None = None
    landmarks: np.ndarray | None = None


class Recognizer:
    """Load both OpenCV models once and expose analyse/embed operations."""

    def __init__(
        self,
        min_face_width: int | None = None,
        blur_threshold: float | None = None,
        detection_score_threshold: float = 0.8,
    ) -> None:
        settings = get_settings()
        self.min_face_width = (
            settings.min_face_width if min_face_width is None else min_face_width
        )
        self.blur_threshold = (
            settings.blur_threshold if blur_threshold is None else blur_threshold
        )
        self.detection_score_threshold = detection_score_threshold
        self.model_name = MODEL_NAME
        self.model_version = MODEL_VERSION

        for path in (DETECTOR_MODEL, RECOGNIZER_MODEL):
            if not path.exists():
                raise FileNotFoundError(
                    f"{path} is missing; run scripts/download_models.py"
                )

        self._detector = cv2.FaceDetectorYN.create(
            str(DETECTOR_MODEL),
            "",
            (320, 320),
            self.detection_score_threshold,
            0.3,
            5000,
        )
        self._recognizer = cv2.FaceRecognizerSF.create(str(RECOGNIZER_MODEL), "")

    def analyse_frame(self, image_bytes: bytes) -> FrameAnalysis:
        frame = self._decode(image_bytes)
        height, width = frame.shape[:2]
        self._detector.setInputSize((width, height))
        _, faces = self._detector.detect(frame)
        face_count = 0 if faces is None else len(faces)

        if face_count == 0:
            return FrameAnalysis(status="no_face", frame=frame, face_count=0)
        if face_count > 1:
            return FrameAnalysis(
                status="multiple_faces", frame=frame, face_count=face_count
            )

        face = faces[0].copy()
        box = (int(face[0]), int(face[1]), int(face[2]), int(face[3]))
        landmarks = face[4:14].reshape(5, 2).astype(np.float32)
        result = {
            "frame": frame,
            "face_count": 1,
            "face": face,
            "box": box,
            "landmarks": landmarks,
        }

        if box[2] < self.min_face_width:
            return FrameAnalysis(status="too_small", **result)

        crop = self._crop(frame, box)
        if crop.size == 0 or self._laplacian_variance(crop) < self.blur_threshold:
            return FrameAnalysis(status="blurry", **result)

        return FrameAnalysis(status="ok", **result)

    def embed(self, frame: np.ndarray, face: np.ndarray) -> np.ndarray:
        aligned = self._recognizer.alignCrop(frame, face)
        feature = self._recognizer.feature(aligned)
        vector = np.asarray(feature, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vector))
        if norm == 0.0:
            raise ValueError("face embedding is empty")
        return (vector / norm).astype(np.float32, copy=False)

    @staticmethod
    def _decode(image_bytes: bytes) -> np.ndarray:
        buffer = np.frombuffer(image_bytes, dtype=np.uint8)
        frame = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError("could not decode image")
        return frame

    @staticmethod
    def _crop(frame: np.ndarray, box: tuple[int, int, int, int]) -> np.ndarray:
        x, y, width, height = box
        image_height, image_width = frame.shape[:2]
        x1, y1 = max(0, x), max(0, y)
        x2, y2 = min(image_width, x + width), min(image_height, y + height)
        return frame[y1:y2, x1:x2]

    @staticmethod
    def _laplacian_variance(crop: np.ndarray) -> float:
        grey = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        return float(cv2.Laplacian(grey, cv2.CV_64F).var())


class GalleryCache:
    """In-memory staff embeddings, reloaded from the database on refresh()."""

    def __init__(
        self, session_factory: Callable[[], Session] = SessionLocal
    ) -> None:
        self._session_factory = session_factory
        self._lock = Lock()
        self._gallery: dict[int, list[np.ndarray]] = {}
        self.refresh()

    def refresh(self) -> None:
        with self._session_factory() as session:
            rows = session.execute(
                select(FaceEmbedding.staff_id, FaceEmbedding.vector)
                .join(Staff, Staff.id == FaceEmbedding.staff_id)
                .where(Staff.active.is_(True), Staff.anonymised.is_(False))
            ).all()

        gallery: dict[int, list[np.ndarray]] = {}
        for staff_id, vector in rows:
            gallery.setdefault(staff_id, []).append(
                np.asarray(vector, dtype=np.float32).copy()
            )

        with self._lock:
            self._gallery = gallery

    def snapshot(self) -> dict[int, list[np.ndarray]]:
        with self._lock:
            return {
                staff_id: list(vectors)
                for staff_id, vectors in self._gallery.items()
            }

    def match_embedding(self, embedding: np.ndarray) -> MatchResult:
        return rules.match(embedding, self.snapshot())
