import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.main import app
from app.models import Attendance, Consent, FaceEmbedding, Staff
from app.recognition import FrameAnalysis
from app.routers.staff import get_gallery, get_recognizer


class FakeRecognizer:
    def __init__(self, statuses: list[str] | None = None) -> None:
        self.statuses = statuses or ["ok"]
        self.analysed: list[bytes] = []
        self.embedded = 0
        self.model_name = "SFace"
        self.model_version = "2021dec"

    def analyse_frame(self, image_bytes: bytes) -> FrameAnalysis:
        index = len(self.analysed)
        self.analysed.append(image_bytes)
        status = self.statuses[min(index, len(self.statuses) - 1)]

        if status == "ok":
            return FrameAnalysis(
                status="ok",
                frame=np.zeros((16, 16, 3), dtype=np.uint8),
                face_count=1,
                face=np.zeros(15, dtype=np.float32),
                box=(0, 0, 16, 16),
                landmarks=np.zeros((5, 2), dtype=np.float32),
            )
        return FrameAnalysis(
            status=status,
            frame=np.zeros((16, 16, 3), dtype=np.uint8),
            face_count=0 if status == "no_face" else 1,
        )

    def embed(self, frame: np.ndarray, face: np.ndarray) -> np.ndarray:
        self.embedded += 1
        vector = np.array([1.0, float(self.embedded)], dtype=np.float32)
        padded = np.zeros(128, dtype=np.float32)
        padded[: vector.size] = vector
        return padded / np.linalg.norm(padded)


class FakeGallery:
    def __init__(self) -> None:
        self.refresh_count = 0

    def refresh(self) -> None:
        self.refresh_count += 1


@pytest.fixture()
def fake_recognizer() -> FakeRecognizer:
    return FakeRecognizer()


@pytest.fixture()
def fake_gallery() -> FakeGallery:
    return FakeGallery()


@pytest.fixture()
def staff_client(
    authenticated_client: TestClient,
    fake_recognizer: FakeRecognizer,
    fake_gallery: FakeGallery,
) -> TestClient:
    app.dependency_overrides[get_recognizer] = lambda: fake_recognizer
    app.dependency_overrides[get_gallery] = lambda: fake_gallery
    return authenticated_client


def create_staff(
    client: TestClient,
    staff_code: str = "S001",
    name: str = "Ada Lovelace",
) -> dict:
    response = client.post(
        "/api/admin/staff",
        json={
            "staff_code": staff_code,
            "name": name,
            "department": "Engineering",
            "shift": "day",
        },
    )
    assert response.status_code == 201
    return response.json()


def enrol(
    client: TestClient,
    staff_id: int,
    count: int = 3,
    consent: bool = True,
):
    files = [
        ("images", (f"face-{index}.jpg", f"image-{index}".encode(), "image/jpeg"))
        for index in range(count)
    ]
    return client.post(
        f"/api/admin/staff/{staff_id}/enrol",
        data={"consent": "true" if consent else "false"},
        files=files,
    )


def count_rows(
    session_factory: sessionmaker[Session], model: type
) -> int:
    with session_factory() as session:
        return (
            session.scalar(select(func.count()).select_from(model))
            or 0
        )


def test_staff_routes_require_admin(client: TestClient) -> None:
    assert client.get("/api/admin/staff").status_code == 401
    assert (
        client.post(
            "/api/admin/staff",
            json={"staff_code": "S001", "name": "Ada Lovelace"},
        ).status_code
        == 401
    )


def test_staff_crud_and_deactivate(
    staff_client: TestClient,
    fake_gallery: FakeGallery,
) -> None:
    created = create_staff(staff_client)
    staff_id = created["id"]

    assert created["enrolled"] is False
    assert created["embedding_count"] == 0

    listing = staff_client.get("/api/admin/staff", params={"q": "ada"})
    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()] == [staff_id]

    fetched = staff_client.get(f"/api/admin/staff/{staff_id}")
    assert fetched.status_code == 200
    assert fetched.json()["staff_code"] == "S001"

    updated = staff_client.patch(
        f"/api/admin/staff/{staff_id}",
        json={"name": "Ada Byron", "department": "Research"},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Ada Byron"
    assert updated.json()["department"] == "Research"

    deactivated = staff_client.post(
        f"/api/admin/staff/{staff_id}/deactivate"
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["active"] is False
    assert fake_gallery.refresh_count == 1

    active_only = staff_client.get(
        "/api/admin/staff", params={"active": "true"}
    )
    assert active_only.status_code == 200
    assert active_only.json() == []

    reactivated = staff_client.patch(
        f"/api/admin/staff/{staff_id}", json={"active": True}
    )
    assert reactivated.status_code == 200
    assert reactivated.json()["active"] is True
    assert fake_gallery.refresh_count == 2


def test_enrol_with_good_images(
    staff_client: TestClient,
    admin_id: int,
    fake_recognizer: FakeRecognizer,
    fake_gallery: FakeGallery,
    session_factory: sessionmaker[Session],
) -> None:
    staff = create_staff(staff_client)
    staff_id = staff["id"]

    response = enrol(staff_client, staff_id, count=3)

    assert response.status_code == 200
    payload = response.json()
    assert payload["staff_id"] == staff_id
    assert payload["consent_text_version"] == "v1"
    assert payload["accepted"] == 3
    assert payload["stored_embeddings"] == 3
    assert [image["accepted"] for image in payload["images"]] == [
        True,
        True,
        True,
    ]
    assert fake_recognizer.analysed == [
        b"image-0",
        b"image-1",
        b"image-2",
    ]
    assert fake_recognizer.embedded == 3
    assert fake_gallery.refresh_count == 1

    with session_factory() as session:
        embeddings = list(session.scalars(select(FaceEmbedding)))
        assert len(embeddings) == 3
        assert all(embedding.vector.shape == (128,) for embedding in embeddings)
        assert all(embedding.model_name == "SFace" for embedding in embeddings)
        consent = session.scalar(select(Consent))
        assert consent is not None
        assert consent.staff_id == staff_id
        assert consent.text_version == "v1"
        assert consent.recorded_by_admin_id == admin_id
        assert consent.given_at is not None

    fetched = staff_client.get(f"/api/admin/staff/{staff_id}")
    assert fetched.status_code == 200
    assert fetched.json()["enrolled"] is True
    assert fetched.json()["embedding_count"] == 3


def test_enrol_without_consent_is_blocked(
    staff_client: TestClient,
    fake_recognizer: FakeRecognizer,
    fake_gallery: FakeGallery,
    session_factory: sessionmaker[Session],
) -> None:
    staff = create_staff(staff_client)

    response = enrol(staff_client, staff["id"], count=3, consent=False)

    assert response.status_code == 400
    assert response.json()["detail"] == "Consent is required before enrolment"
    assert fake_recognizer.analysed == []
    assert fake_gallery.refresh_count == 0
    assert count_rows(session_factory, FaceEmbedding) == 0
    assert count_rows(session_factory, Consent) == 0


def test_enrol_with_too_few_accepted_images(
    staff_client: TestClient,
    fake_recognizer: FakeRecognizer,
    fake_gallery: FakeGallery,
    session_factory: sessionmaker[Session],
) -> None:
    staff = create_staff(staff_client)
    fake_recognizer.statuses = ["ok", "no_face", "blurry"]

    response = enrol(staff_client, staff["id"], count=3)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["message"] == (
        "At least 3 acceptable images are required; got 1."
    )
    assert [image["status"] for image in detail["images"]] == [
        "ok",
        "no_face",
        "blurry",
    ]
    assert [image["reason"] for image in detail["images"]] == [
        None,
        "No face detected.",
        "Face is blurry.",
    ]
    assert fake_gallery.refresh_count == 0
    assert count_rows(session_factory, FaceEmbedding) == 0
    assert count_rows(session_factory, Consent) == 0


def test_enrol_stores_only_accepted_images(
    staff_client: TestClient,
    fake_recognizer: FakeRecognizer,
    session_factory: sessionmaker[Session],
) -> None:
    staff = create_staff(staff_client)
    fake_recognizer.statuses = [
        "ok",
        "no_face",
        "ok",
        "too_small",
        "ok",
    ]

    response = enrol(staff_client, staff["id"], count=5)

    assert response.status_code == 200
    payload = response.json()
    assert payload["accepted"] == 3
    assert [image["accepted"] for image in payload["images"]] == [
        True,
        False,
        True,
        False,
        True,
    ]
    assert payload["images"][1]["reason"] == "No face detected."
    assert payload["images"][3]["reason"] == "Face is too small."
    assert fake_recognizer.embedded == 3
    assert count_rows(session_factory, FaceEmbedding) == 3


def test_re_enrol_replaces_old_embeddings_and_consent_history(
    staff_client: TestClient,
    fake_gallery: FakeGallery,
    session_factory: sessionmaker[Session],
) -> None:
    staff = create_staff(staff_client)
    staff_id = staff["id"]
    assert enrol(staff_client, staff_id).status_code == 200

    with session_factory() as session:
        first_vectors = {
            vector.tobytes()
            for vector in session.scalars(select(FaceEmbedding.vector))
        }

    assert enrol(staff_client, staff_id).status_code == 200

    with session_factory() as session:
        second_vectors = {
            vector.tobytes()
            for vector in session.scalars(select(FaceEmbedding.vector))
        }
        assert len(second_vectors) == 3
        assert second_vectors.isdisjoint(first_vectors)
        assert (
            session.scalar(
                select(func.count())
                .select_from(Consent)
                .where(Consent.staff_id == staff_id)
            )
            == 2
        )

    assert fake_gallery.refresh_count == 2


def test_delete_removes_embeddings_but_keeps_attendance(
    staff_client: TestClient,
    fake_gallery: FakeGallery,
    session_factory: sessionmaker[Session],
) -> None:
    staff = create_staff(staff_client)
    staff_id = staff["id"]
    assert enrol(staff_client, staff_id).status_code == 200

    with session_factory() as session:
        session.add(
            Attendance(staff_id=staff_id, type="in", method="face")
        )
        session.commit()

    response = staff_client.delete(f"/api/admin/staff/{staff_id}")

    assert response.status_code == 200
    assert response.json() == {"status": "deleted", "staff_id": staff_id}
    assert fake_gallery.refresh_count == 2

    with session_factory() as session:
        stored = session.get(Staff, staff_id)
        assert stored is not None
        assert stored.anonymised is True
        assert stored.active is False
        assert stored.name == ""
        assert (
            session.scalar(
                select(func.count())
                .select_from(FaceEmbedding)
                .where(FaceEmbedding.staff_id == staff_id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(Consent)
                .where(Consent.staff_id == staff_id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(Attendance)
                .where(Attendance.staff_id == staff_id)
            )
            == 1
        )

    assert staff_client.get(f"/api/admin/staff/{staff_id}").status_code == 200
    assert staff_client.delete(f"/api/admin/staff/{staff_id}").status_code == 404
