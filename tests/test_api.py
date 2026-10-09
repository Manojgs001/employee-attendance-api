"""
Integration and API endpoint tests using TestClient and mongomock.
"""
from datetime import datetime, timezone
import mongomock
import pytest
from starlette.testclient import TestClient

import app.main as app_main
from app.main import app


@pytest.fixture(autouse=True)
def setup_mock_db(monkeypatch):
    """Replaces app.main.db and app.main.client with a fresh mongomock client for each test."""
    mock_client = mongomock.MongoClient()
    mock_db = mock_client["attendance_test_db"]
    mock_db.employees.create_index([("emp_code", 1)], unique=True)
    mock_db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    # Mock ping command on admin
    mock_client.admin.command = lambda cmd, *args, **kwargs: {"ok": 1}

    monkeypatch.setattr(app_main, "client", mock_client)
    monkeypatch.setattr(app_main, "db", mock_db)
    yield mock_db


@pytest.fixture
def client():
    return TestClient(app)


def test_health(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_create_and_list_employee(client):
    # 1. Create employee
    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert "created_at" in data
    assert isinstance(data["created_at"], int)
    assert "_id" not in data

    # 2. Equal shift_start and shift_end -> 422
    payload_bad = {**payload, "emp_code": "EMP0002", "shift_end": "09:30"}
    res_bad = client.post("/employees", json=payload_bad)
    assert res_bad.status_code == 422

    # 3. Duplicate emp_code -> 409
    res_dup = client.post("/employees", json=payload)
    assert res_dup.status_code == 409

    # 4. List employees
    res_list = client.get("/employees")
    assert res_list.status_code == 200
    list_data = res_list.json()
    assert list_data["total"] == 1
    assert len(list_data["items"]) == 1
    assert list_data["items"][0]["emp_code"] == "EMP0001"


def test_punch_in_and_out(client):
    # Seed employee
    emp_payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    client.post("/employees", json=emp_payload)

    # 1. Punch in unknown employee -> 404
    res_unk = client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert res_unk.status_code == 404

    # 2. Punch in on 2026-07-06 at 09:28 IST (03:58 UTC) = 1783310280000 ms
    # 2026-07-06 03:58:00 UTC epoch ms
    ts_in = int(datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)
    punch_in_payload = {
        "emp_code": "EMP0001",
        "punched_at": ts_in,
        "status": "PRESENT",
    }
    res_in = client.post("/attendance/punch-in", json=punch_in_payload)
    assert res_in.status_code == 201
    in_data = res_in.json()
    assert in_data["date"] == "2026-07-06"
    assert in_data["late_minutes"] == 0
    assert in_data["work_hours"] is None
    assert in_data["punch_out"] is None
    assert in_data["history"] == []
    assert "_id" not in in_data

    # 3. Duplicate punch in for same day -> 409
    res_in_dup = client.post("/attendance/punch-in", json=punch_in_payload)
    assert res_in_dup.status_code == 409

    # 4. Punch out: 2026-07-06 at 18:35 IST (13:05 UTC)
    ts_out = int(datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc).timestamp() * 1000)
    punch_out_payload = {
        "emp_code": "EMP0001",
        "punched_at": ts_out,
    }
    res_out = client.post("/attendance/punch-out", json=punch_out_payload)
    assert res_out.status_code == 200
    out_data = res_out.json()
    assert out_data["work_hours"] == 9.12
    assert out_data["overtime_minutes"] == 0
    assert out_data["half_day"] is False
    assert out_data["punch_out"] == ts_out

    # 5. Punch out again on already closed record -> 409
    res_out_dup = client.post("/attendance/punch-out", json=punch_out_payload)
    assert res_out_dup.status_code == 409


def test_list_attendance(client):
    emp_payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    client.post("/employees", json=emp_payload)

    ts_in = int(datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})

    # List attendance
    res = client.get("/attendance")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert data["items"][0]["emp_code"] == "EMP0001"

    # Bad date range -> 422
    res_bad = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
    assert res_bad.status_code == 422


def test_regularize_attendance(client):
    emp_payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    client.post("/employees", json=emp_payload)

    # Punched in at 10:05 IST (04:35 UTC) -> 35 min late
    ts_in = int(datetime(2026, 7, 7, 4, 35, 0, tzinfo=timezone.utc).timestamp() * 1000)
    ts_out = int(datetime(2026, 7, 7, 13, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts_in})
    client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts_out})

    # Correction: change punch-in to 09:28 IST (03:58 UTC)
    new_in = int(datetime(2026, 7, 7, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)
    reg_payload = {
        "punch_in": new_in,
        "reason": "biometric glitch",
        "regularized_by": "hr.admin",
    }
    res_reg = client.patch("/attendance/EMP0001/2026-07-07", json=reg_payload)
    assert res_reg.status_code == 200
    reg_data = res_reg.json()
    assert reg_data["late_minutes"] == 0
    assert reg_data["work_hours"] == 9.03
    assert len(reg_data["history"]) == 1
    assert reg_data["history"][0]["by"] == "hr.admin"
    assert reg_data["history"][0]["reason"] == "biometric glitch"
    assert "late_minutes" in reg_data["history"][0]["changes"]
    assert reg_data["history"][0]["changes"]["late_minutes"] == {"from": 35, "to": 0}

    # Request that changes nothing -> 422
    res_no_change = client.patch("/attendance/EMP0001/2026-07-07", json=reg_payload)
    assert res_no_change.status_code == 422
