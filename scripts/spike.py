"""Standalone webcam spike: detect, align, embed and compare one face."""

import time
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
DETECTOR_MODEL = ROOT / "models" / "face_detection_yunet_2023mar.onnx"
RECOGNIZER_MODEL = ROOT / "models" / "face_recognition_sface_2021dec.onnx"
WINDOW = "Face attendance recognition spike"
SIMILARITY_FONT = cv2.FONT_HERSHEY_SIMPLEX


def load_models() -> tuple[cv2.FaceDetectorYN, cv2.FaceRecognizerSF]:
    for path in (DETECTOR_MODEL, RECOGNIZER_MODEL):
        if not path.exists():
            raise FileNotFoundError(f"{path} is missing; run scripts/download_models.py")

    detector = cv2.FaceDetectorYN.create(
        str(DETECTOR_MODEL), "", (320, 320), 0.9, 0.3, 5000
    )
    recognizer = cv2.FaceRecognizerSF.create(str(RECOGNIZER_MODEL), "")
    return detector, recognizer


def detect_face(
    detector: cv2.FaceDetectorYN, frame: cv2.typing.MatLike
) -> tuple[cv2.typing.MatLike | None, float]:
    height, width = frame.shape[:2]
    detector.setInputSize((width, height))
    started = time.perf_counter()
    _, faces = detector.detect(frame)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return faces, elapsed_ms


def embed_face(
    recognizer: cv2.FaceRecognizerSF,
    frame: cv2.typing.MatLike,
    face: cv2.typing.MatLike,
) -> tuple[cv2.typing.MatLike, float]:
    started = time.perf_counter()
    aligned = recognizer.alignCrop(frame, face)
    feature = recognizer.feature(aligned)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return feature, elapsed_ms


def draw_status(
    frame: cv2.typing.MatLike, face: cv2.typing.MatLike, similarity: float | None
) -> None:
    x, y, width, height = (int(value) for value in face[:4])
    cv2.rectangle(frame, (x, y), (x + width, y + height), (0, 255, 0), 2)
    if similarity is not None:
        label = f"similarity {similarity:.3f}"
        cv2.putText(
            frame,
            label,
            (x, max(y - 10, 20)),
            SIMILARITY_FONT,
            0.7,
            (0, 255, 0),
            2,
        )


def main() -> int:
    detector, recognizer = load_models()
    capture = cv2.VideoCapture(0)
    if not capture.isOpened():
        print("Could not open camera 0.")
        return 1

    enrolled = None
    print("Press 'e' to enrol the current face, 'q' to quit.")
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                print("Camera frame could not be read.")
                break

            faces, detect_ms = detect_face(detector, frame)
            face_count = 0 if faces is None else len(faces)
            message = f"faces={face_count} detect={detect_ms:.1f}ms"

            feature = None
            similarity = None
            if face_count == 1:
                feature, embed_ms = embed_face(recognizer, frame, faces[0])
                message += f" embed={embed_ms:.1f}ms"
                if enrolled is None:
                    message += " not_enrolled"
                else:
                    similarity = recognizer.match(
                        enrolled, feature, cv2.FaceRecognizerSF_FR_COSINE
                    )
                    message += f" cosine={similarity:.3f}"
                draw_status(frame, faces[0], similarity)
            elif face_count == 0:
                message += " waiting_for_face"
            else:
                message += " waiting_for_one_face"

            print(message)
            cv2.putText(frame, message, (10, 30), SIMILARITY_FONT, 0.7, (0, 255, 255), 2)
            cv2.imshow(WINDOW, frame)

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            if key == ord("e"):
                if feature is None:
                    print("Cannot enrol: exactly one face is required.")
                else:
                    enrolled = feature.copy()
                    print("Enrolled the current face. Show another face or photo to compare.")
    finally:
        capture.release()
        cv2.destroyAllWindows()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
