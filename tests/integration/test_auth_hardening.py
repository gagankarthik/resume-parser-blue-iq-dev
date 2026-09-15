"""Auth hardening.

  * A7 - the login/signup throttle keyed on the FIRST X-Forwarded-For hop, which the
         client controls: rotating it bypassed the limit
  * A8 - hmac.compare_digest on str raises TypeError for non-ASCII input, turning a
         bad admin/session token into a 500; a session token for a deactivated
         company was still accepted
"""

from fastapi.testclient import TestClient

import app.api.dependencies as deps
import app.api.v1.endpoints.account as account
import app.api.v1.endpoints.auth as auth
from app.core.config import get_settings
from app.core.security import create_account_token
from app.main import app

client = TestClient(app)


def test_login_throttle_ignores_a_spoofed_forwarded_for(monkeypatch):
    monkeypatch.setattr(get_settings(), "auth_rate_limit_per_minute", 2)
    monkeypatch.setattr(auth.db, "get_company_by_email", lambda email: None)

    codes = [
        client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": "x"},
                    headers={"X-Forwarded-For": f"10.0.0.{i}"}).status_code
        for i in range(3)
    ]

    assert codes == [401, 401, 429]


def test_non_ascii_admin_token_is_401(monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_api_token", "x" * 40)
    resp = client.get("/api/v1/admin/companies", headers={"X-Admin-Token": "été".encode("latin-1")})
    assert resp.status_code == 401


def test_non_ascii_session_token_is_401():
    resp = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer abc.é".encode("latin-1")})
    assert resp.status_code == 401


def test_session_token_for_a_deactivated_company_is_rejected(monkeypatch):
    token = create_account_token("acme-1", get_settings().auth_secret)
    monkeypatch.setattr(deps, "_company_is_active", lambda company_id: False)
    monkeypatch.setattr(account.db, "list_api_keys_for_company", lambda company_id: [])

    resp = client.get("/api/v1/account/keys", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 403
    assert resp.json()["error"]["error_code"] == "ACCOUNT_DEACTIVATED"
