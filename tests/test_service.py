import json
import pytest
from app.service import app, events_store

@pytest.fixture(autouse=True)
def setup_env_and_store(monkeypatch):
    events_store.clear()
    monkeypatch.setenv("REPORTER_TOKEN", "test-reporter-secret")
    monkeypatch.setenv("OPERATOR_TOKEN", "test-operator-secret")

@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client

def test_auth_sequence_unauthorized_first(client):
    # 規則：未認證者送錯格式，必須先拿 401 而非 400
    res = client.post("/events", json={"bad": "payload"})
    assert res.status_code == 401

def test_fixtures_validation(client):
    # 1. 成功測資
    with open("tests/fixtures/valid_event.json") as f:
        valid_data = json.load(f)
    res = client.post(
        "/events",
        json=valid_data,
        headers={"Authorization": "Bearer test-reporter-secret"}
    )
    assert res.status_code == 201
    assert "received_at" in res.get_json()

    # 409 重複送出
    res_duplicate = client.post(
        "/events",
        json=valid_data,
        headers={"Authorization": "Bearer test-reporter-secret"}
    )
    assert res_duplicate.status_code == 409

    # 2. 拒絕測資：無時區
    with open("tests/fixtures/invalid_no_tz.json") as f:
        invalid_tz = json.load(f)
    res_tz = client.post(
        "/events",
        json=invalid_tz,
        headers={"Authorization": "Bearer test-reporter-secret"}
    )
    assert res_tz.status_code == 400
    assert res_tz.get_json().get("field") == "observed_at"

    # 3. 拒絕測資：多餘欄位
    with open("tests/fixtures/invalid_extra_field.json") as f:
        extra_field = json.load(f)
    res_extra = client.post(
        "/events",
        json=extra_field,
        headers={"Authorization": "Bearer test-reporter-secret"}
    )
    assert res_extra.status_code == 400

def test_operator_permissions(client):
    # 準備一筆資料
    with open("tests/fixtures/valid_event.json") as f:
        valid_data = json.load(f)
    client.post("/events", json=valid_data, headers={"Authorization": "Bearer test-reporter-secret"})

    # reporter 不能讀取清單 (403)
    res_rep_get = client.get("/events", headers={"Authorization": "Bearer test-reporter-secret"})
    assert res_rep_get.status_code == 403

    # operator 可以讀取清單 (200)
    res_op_get = client.get("/events", headers={"Authorization": "Bearer test-operator-secret"})
    assert res_op_get.status_code == 200
    assert len(res_op_get.get_json()) == 1
