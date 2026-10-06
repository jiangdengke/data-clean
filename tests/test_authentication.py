from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import AppSettings


def create_test_client(tmp_path: Path, cookie_secure: bool = False) -> TestClient:
    application = create_app(AppSettings(admin_password="test-password", data_directory=tmp_path, cookie_secure=cookie_secure))
    return TestClient(application)


def test_report_and_connection_endpoints_require_authentication(tmp_path: Path) -> None:
    with create_test_client(tmp_path) as client:
        assert client.get("/api/connection").status_code == 401
        assert client.get("/api/scans/not-a-uuid/report").status_code == 401


def test_login_sets_http_only_secure_same_site_cookie(tmp_path: Path) -> None:
    with create_test_client(tmp_path, cookie_secure=True) as client:
        response = client.post("/api/login", json={"password": "test-password"})
        assert response.status_code == 200
        set_cookie = response.headers["set-cookie"]
        assert "HttpOnly" in set_cookie
        assert "Secure" in set_cookie
        assert "SameSite=lax" in set_cookie


def test_connection_response_never_returns_secret_access_key(tmp_path: Path) -> None:
    with create_test_client(tmp_path) as client:
        client.post("/api/login", json={"password": "test-password"})
        response = client.post("/api/connection", json={"endpoint": "https://example.invalid", "access_key_id": "access-id", "secret_access_key": "secret-value", "source_bucket": "source-bucket"})
        assert response.status_code == 200
        assert "secret-value" not in response.text
        assert response.json() == {"configured": True, "endpoint": "https://example.invalid", "source_bucket": "source-bucket"}
