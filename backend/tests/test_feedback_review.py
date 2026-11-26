import os

import pytest
from fastapi.testclient import TestClient
from passlib.context import CryptContext
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def review_session(tmp_path, monkeypatch):
    # These defaults allow the app to load without a local .env.
    for setting in (
        "MYSQL_USER",
        "MYSQL_PASSWORD",
        "MYSQL_DATABASE",
        "MYSQL_ROOT_PASSWORD",
        "MINIO_ACCESS_KEY",
        "MINIO_SECRET_KEY",
        "JWT_SECRET_KEY",
    ):
        if setting not in os.environ:
            monkeypatch.setenv(setting, "qa-local-only")

    from app.database import get_db
    from app.main import app
    from app.middleware import audit
    from app.models import Base, Material, ModelVersion, Project, User, UserProjectMapping
    from app.models.user import UserRole
    from app.models.user_project_mapping import AccessLevel

    engine = create_engine(f"sqlite:///{tmp_path / 'feedback.sqlite'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    sessions = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    login = {"email": "reviewer@example.com", "password": "qa-local-password"}
    password_context = CryptContext(schemes=["bcrypt"])
    with sessions.begin() as db:
        doctor = User(
            name="QA Reviewer",
            email=login["email"],
            hashed_password=password_context.hash(login["password"]),
            role=UserRole.doctor,
        )
        material = Material(name="QA sample material", physical_parameters={})
        db.add_all([doctor, material])
        db.flush()
        project = Project(name="QA feedback review", owner_id=doctor.user_id)
        db.add(project)
        db.flush()
        db.add(UserProjectMapping(
            project_id=project.project_id,
            user_id=doctor.user_id,
            access_level=AccessLevel.read_only,
        ))
        versions = []
        for number in (1, 2):
            version = ModelVersion(
                project_id=project.project_id,
                uploader_id=doctor.user_id,
                material_id=material.material_id,
                version_number=number,
                file_url=f"/demo/sample-v{number}.stl",
                hash_value=str(number) * 64,
            )
            db.add(version)
            versions.append(version)
        db.flush()
        paths = [
            f"/api/v1/projects/{project.project_id}/versions/{version.version_id}/feedbacks"
            for version in versions
        ]
        doctor_id = doctor.user_id

    def test_db():
        with sessions() as db:
            yield db

    monkeypatch.setitem(app.dependency_overrides, get_db, test_db)
    monkeypatch.setattr(audit, "SessionLocal", sessions)
    try:
        with TestClient(app) as client:
            response = client.post("/api/v1/auth/login", json=login)
            assert response.status_code == 200, response.text
            client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
            yield client, paths[0], paths[1], doctor_id
    finally:
        engine.dispose()


def test_point_feedback_persists_on_selected_version(review_session):
    """MS2 T5: a saved point and its text are read back only on v2."""
    client, v1, v2, doctor_id = review_session
    feedback = {
        "content": "請確認固定板邊緣貼合。",
        "coordinates": {"x": 19.4, "y": 7.3, "z": 0.8},
    }

    response = client.post(v2, json=feedback)
    assert response.status_code == 201, response.text
    saved = response.json()
    assert saved["author_id"] == doctor_id
    assert saved["content"] == feedback["content"]
    assert saved["coordinates"] == feedback["coordinates"]

    reloaded = client.get(v2)
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json() == [saved]

    other_version = client.get(v1)
    assert other_version.status_code == 200, other_version.text
    assert other_version.json() == []


def test_point_feedback_requires_all_three_coordinates(review_session):
    client, _, v2, _ = review_session
    response = client.post(v2, json={
        "content": "缺少 Z 座標",
        "coordinates": {"x": 19.4, "y": 7.3},
    })

    assert response.status_code == 422, response.text
    assert any(error["loc"] == ["body", "coordinates", "z"] for error in response.json()["detail"])
    reloaded = client.get(v2)
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json() == []
