import base64
import json
import os
import time

os.environ["EDITOR_USERNAME"] = "tusengland"
os.environ["EDITOR_PASSWORD"] = "tus1234"
os.environ["JWT_SECRET"] = "test-secret"
os.environ["JWT_TTL_SECONDS"] = "3600"
os.environ["GITHUB_TOKEN"] = "fake-token"
os.environ["GITHUB_OWNER"] = "cvuong233"
os.environ["GITHUB_REPO"] = "agent-presentation"
os.environ["GITHUB_PATH"] = "lastwar/players.json"
os.environ["GITHUB_BRANCH"] = "master"

import app as app_module
import jwt as pyjwt
from unittest.mock import patch, MagicMock

SAMPLE_DATA = {
    "next_lineup": {
        "sm": {
            "A": {
                "main": [{"r": "R5", "name": "Lerxinhiu", "reason": "R5"}],
                "subs": [{"r": "R3", "name": "RùaNgáo", "power": 46.5, "reason": "x"}],
                "dayoff": []
            }
        }
    }
}


def make_get_response(data, sha="abc123"):
    content = json.dumps(data)
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    m = MagicMock()
    m.json.return_value = {"content": b64, "sha": sha}
    m.raise_for_status.return_value = None
    return m


def client():
    app_module.app.testing = True
    return app_module.app.test_client()


def test_login_success():
    c = client()
    r = c.post("/api/login", json={"username": "tusengland", "password": "tus1234"})
    assert r.status_code == 200
    body = r.get_json()
    assert "token" in body
    decoded = pyjwt.decode(body["token"], "test-secret", algorithms=["HS256"])
    assert decoded["sub"] == "editor"


def test_login_wrong_password():
    c = client()
    r = c.post("/api/login", json={"username": "tusengland", "password": "nope"})
    assert r.status_code == 401
    assert r.get_json()["error"] == "invalid_credentials"


def test_login_rate_limit():
    app_module._login_attempts.clear()
    c = client()
    for _ in range(5):
        r = c.post("/api/login", json={"username": "x", "password": "y"}, environ_base={"REMOTE_ADDR": "9.9.9.9"})
        assert r.status_code == 401
    r = c.post("/api/login", json={"username": "x", "password": "y"}, environ_base={"REMOTE_ADDR": "9.9.9.9"})
    assert r.status_code == 429
    app_module._login_attempts.clear()


def test_positions_requires_auth():
    c = client()
    r = c.post("/api/positions", json={"edits": []})
    assert r.status_code == 401


def test_positions_rejects_bad_token():
    c = client()
    r = c.post("/api/positions", json={"edits": []}, headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401
    assert r.get_json()["error"] == "invalid_token"


def test_positions_rejects_expired_token():
    now = int(time.time())
    expired = pyjwt.encode({"sub": "editor", "iat": now - 100, "exp": now - 10}, "test-secret", algorithm="HS256")
    c = client()
    r = c.post("/api/positions", json={"edits": []}, headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401
    assert r.get_json()["error"] == "token_expired"


def test_positions_save_success():
    c = client()
    token = app_module.make_token()
    put_resp = MagicMock(status_code=200)
    with patch("app.requests.get", return_value=make_get_response(SAMPLE_DATA)) as mget, \
         patch("app.requests.put", return_value=put_resp) as mput:
        r = c.post(
            "/api/positions",
            json={"edits": [{"mode": "sm", "slot": "A", "section": "main", "name": "Lerxinhiu", "pos": "Tank Left"}]},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["applied"] == ["Lerxinhiu"]
    # check what was actually PUT
    put_kwargs = mput.call_args.kwargs
    sent_content = base64.b64decode(put_kwargs["json"]["content"]).decode("utf-8")
    sent_data = json.loads(sent_content)
    assert sent_data["next_lineup"]["sm"]["A"]["main"][0]["pos"] == "Tank Left"
    assert put_kwargs["json"]["sha"] == "abc123"
    assert put_kwargs["json"]["branch"] == "master"


def test_positions_clear_pos_when_empty_string():
    c = client()
    token = app_module.make_token()
    data_with_pos = json.loads(json.dumps(SAMPLE_DATA))
    data_with_pos["next_lineup"]["sm"]["A"]["main"][0]["pos"] = "OldValue"
    put_resp = MagicMock(status_code=200)
    with patch("app.requests.get", return_value=make_get_response(data_with_pos)), \
         patch("app.requests.put", return_value=put_resp) as mput:
        r = c.post(
            "/api/positions",
            json={"edits": [{"mode": "sm", "slot": "A", "section": "main", "name": "Lerxinhiu", "pos": ""}]},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200
    put_kwargs = mput.call_args.kwargs
    sent_data = json.loads(base64.b64decode(put_kwargs["json"]["content"]).decode("utf-8"))
    assert "pos" not in sent_data["next_lineup"]["sm"]["A"]["main"][0]


def test_positions_unknown_player_skipped():
    c = client()
    token = app_module.make_token()
    with patch("app.requests.get", return_value=make_get_response(SAMPLE_DATA)):
        r = c.post(
            "/api/positions",
            json={"edits": [{"mode": "sm", "slot": "A", "section": "main", "name": "GhostPlayer", "pos": "X"}]},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 400
    assert r.get_json()["skipped"] == ["GhostPlayer"]


def test_positions_retries_on_409_then_succeeds():
    c = client()
    token = app_module.make_token()
    responses = [MagicMock(status_code=409), MagicMock(status_code=200)]
    with patch("app.requests.get", return_value=make_get_response(SAMPLE_DATA)), \
         patch("app.requests.put", side_effect=responses) as mput:
        r = c.post(
            "/api/positions",
            json={"edits": [{"mode": "sm", "slot": "A", "section": "main", "name": "Lerxinhiu", "pos": "Y"}]},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200
    assert mput.call_count == 2


def test_positions_github_put_hard_failure():
    c = client()
    token = app_module.make_token()
    with patch("app.requests.get", return_value=make_get_response(SAMPLE_DATA)), \
         patch("app.requests.put", return_value=MagicMock(status_code=422, text="unprocessable")):
        r = c.post(
            "/api/positions",
            json={"edits": [{"mode": "sm", "slot": "A", "section": "main", "name": "Lerxinhiu", "pos": "Y"}]},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 502
    assert r.get_json()["error"] == "github_put_failed"


def test_health():
    c = client()
    r = c.get("/health")
    assert r.status_code == 200
    assert r.get_json()["status"] == "ok"


def test_cors_header_present():
    c = client()
    r = c.get("/health")
    assert r.headers.get("Access-Control-Allow-Origin") == "*"
