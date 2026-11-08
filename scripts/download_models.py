"""Download the pre-trained OpenCV models used by the recognition spike."""

import argparse
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = ROOT / "models"

MODELS = {
    "face_detection_yunet_2023mar.onnx": (
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
        "face_detection_yunet/face_detection_yunet_2023mar.onnx"
    ),
    "face_recognition_sface_2021dec.onnx": (
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
        "face_recognition_sface/face_recognition_sface_2021dec.onnx"
    ),
}
MINIMUM_BYTES = 100_000
USER_AGENT = "face-attendance-course-project/1.0"


def download(url: str, target: Path, attempts: int = 3) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    partial = target.with_name(target.name + ".part")
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                with partial.open("wb") as handle:
                    while chunk := response.read(1024 * 1024):
                        handle.write(chunk)
            partial.replace(target)
            return
        except (urllib.error.URLError, OSError) as error:
            partial.unlink(missing_ok=True)
            if attempt == attempts:
                raise
            print(
                f"download attempt {attempt} failed ({error}); retrying",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(2 * attempt)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="download even if the file exists")
    args = parser.parse_args()

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    for name, url in MODELS.items():
        target = MODELS_DIR / name
        if target.exists() and not args.force:
            print(f"{name}: already present")
        else:
            print(f"Downloading {name} ...", flush=True)
            try:
                download(url, target)
            except (urllib.error.URLError, OSError) as error:
                print(f"{name}: download failed: {error}", file=sys.stderr)
                return 1
            print(f"{name}: downloaded")

        size = target.stat().st_size
        if size < MINIMUM_BYTES:
            print(f"{name}: unexpected size {size:,} bytes", file=sys.stderr)
            return 1
        print(f"{name}: {size:,} bytes ({size / (1024 * 1024):.1f} MiB)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
