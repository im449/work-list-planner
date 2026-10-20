import os
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def demo_client(tmp_path, monkeypatch):
    database_path = tmp_path / "planner-test.sqlite3"
    demo_dir = tmp_path / "demo-sessions"
    monkeypatch.setattr(main, "DB_PATH", str(database_path))
    monkeypatch.setattr(main, "DEMO_DB_DIR", str(demo_dir))
    main.init_db()
    with TestClient(main.app) as client:
        yield client, database_path, demo_dir


def create_demo(client):
    response = client.post("/api/demo/sessions")
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def create_regular_user(client, username="regular_demo_test"):
    registered = client.post(
        "/register",
        data={"username": username, "password": "pass12345"},
    )
    assert registered.status_code == 200, registered.text
    response = client.post(
        "/token",
        data={"username": username, "password": "pass12345"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def test_demo_sessions_have_separate_tasks_budget_and_savings(demo_client):
    client, _, _ = demo_client
    first = create_demo(client)
    second = create_demo(client)

    task = client.post("/tasks", json={"title": "Только в первой демосессии"}, headers=first)
    assert task.status_code == 200, task.text
    assert any(
        item["title"] == "Только в первой демосессии"
        for item in client.get("/tasks", headers=first).json()
    )
    assert client.get("/tasks", headers=second).json() == []

    budget = client.post(
        "/api/budget/items",
        headers=first,
        json={
            "title": "Демо-расход",
            "amount_kopecks": 12345,
            "item_type": "expense",
            "category": "Тестовая категория",
            "due_date": "2026-10-20",
            "mandatory": False,
            "status": "planned",
            "notes": "",
            "repeat_monthly": False,
        },
    )
    assert budget.status_code == 200, budget.text
    first_budget = client.get("/api/budget/items?year=2026", headers=first)
    second_budget = client.get("/api/budget/items?year=2026", headers=second)
    assert any(item["title"] == "Демо-расход" for item in first_budget.json())
    assert all(item["title"] != "Демо-расход" for item in second_budget.json())

    goal = client.post(
        "/api/savings/goals",
        headers=first,
        json={
            "name": "Демо-накопление",
            "goal": 1000,
            "days": 3,
            "start_date": "2026-10-20",
            "end_date": "2026-10-22",
        },
    )
    assert goal.status_code == 200, goal.text
    assert any(
        item["name"] == "Демо-накопление"
        for item in client.get("/api/savings/goals", headers=first).json()
    )
    assert all(
        item["name"] != "Демо-накопление"
        for item in client.get("/api/savings/goals", headers=second).json()
    )


def test_demo_token_cannot_access_account_or_admin_routes(demo_client):
    client, _, _ = demo_client
    demo_headers = create_demo(client)

    assert client.get("/users/me", headers=demo_headers).status_code == 401
    assert client.post("/users/me/2fa/setup", headers=demo_headers, json={"current_password": "x"}).status_code == 401
    assert client.get("/admin/users", headers=demo_headers).status_code == 401


def test_regular_token_cannot_end_demo_session(demo_client):
    client, _, _ = demo_client
    regular_headers = create_regular_user(client)
    response = client.delete("/api/demo/sessions/current", headers=regular_headers)
    assert response.status_code == 403


def test_demo_session_ends_and_removes_only_its_database(demo_client):
    client, database_path, demo_dir = demo_client
    first = create_demo(client)
    second = create_demo(client)

    from jose import jwt

    first_session_id = jwt.decode(
        first["Authorization"].removeprefix("Bearer "),
        main.SECRET_KEY,
        algorithms=[main.ALGORITHM],
    )["sid"]
    second_session_id = jwt.decode(
        second["Authorization"].removeprefix("Bearer "),
        main.SECRET_KEY,
        algorithms=[main.ALGORITHM],
    )["sid"]
    conn = sqlite3.connect(database_path)
    try:
        first_db = conn.execute(
            "SELECT db_path FROM demo_sessions WHERE session_id = ?",
            (first_session_id,),
        ).fetchone()[0]
        second_db = conn.execute(
            "SELECT db_path FROM demo_sessions WHERE session_id = ?",
            (second_session_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert os.path.isfile(first_db)
    assert os.path.isfile(second_db)

    ended = client.delete("/api/demo/sessions/current", headers=first)
    assert ended.status_code == 200, ended.text
    assert not os.path.exists(first_db)
    assert os.path.exists(second_db)
    assert client.get("/tasks", headers=first).status_code == 401
    assert client.get("/tasks", headers=second).status_code == 200


def test_idle_demo_session_expires_and_database_is_removed(demo_client):
    client, database_path, _ = demo_client
    headers = create_demo(client)
    conn = sqlite3.connect(database_path)
    try:
        session_id, db_path = conn.execute(
            "SELECT session_id, db_path FROM demo_sessions"
        ).fetchone()
        conn.execute(
            "UPDATE demo_sessions SET last_activity = ? WHERE session_id = ?",
            (int(time.time()) - main.DEMO_IDLE_TIMEOUT_SECONDS - 1, session_id),
        )
        conn.commit()
    finally:
        conn.close()

    response = client.get("/tasks", headers=headers)
    assert response.status_code == 401
    assert not os.path.exists(db_path)


def test_demo_session_limit_per_ip(demo_client):
    client, _, _ = demo_client
    sessions = [create_demo(client) for _ in range(3)]
    response = client.post("/api/demo/sessions")
    assert response.status_code == 429
    for headers in sessions:
        assert client.delete("/api/demo/sessions/current", headers=headers).status_code == 200


def test_demo_task_record_limit(demo_client):
    client, database_path, _ = demo_client
    headers = create_demo(client)
    conn = sqlite3.connect(database_path)
    try:
        session_id, db_path = conn.execute(
            "SELECT session_id, db_path FROM demo_sessions"
        ).fetchone()
    finally:
        conn.close()

    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO tasks (title, user_id) VALUES (?, ?)",
            [(f"Тестовая задача {i}", 1) for i in range(main.DEMO_MAX_TASKS)],
        )
        conn.commit()
    finally:
        conn.close()

    response = client.post("/tasks", json={"title": "Сверх лимита"}, headers=headers)
    assert response.status_code == 429


def test_demo_savings_calendar_entry_limit(demo_client):
    client, _, _ = demo_client
    headers = create_demo(client)
    goals = []
    for name in ("Копилка 1", "Копилка 2"):
        response = client.post(
            "/api/savings/goals",
            headers=headers,
            json={
                "name": name,
                "goal": 1000,
                "days": 3650,
                "start_date": "2026-01-01",
                "end_date": "2035-12-29",
            },
        )
        assert response.status_code == 200, response.text
        goals.append(response.json()["id"])

    registry = sqlite3.connect(str(demo_client[1]))
    try:
        session_id, db_path = registry.execute(
            "SELECT session_id, db_path FROM demo_sessions"
        ).fetchone()
    finally:
        registry.close()

    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO savings_goal_entries (goal_id, day, amount, paid) VALUES (?, ?, 0, 0)",
            [(goal_id, day) for goal_id in goals for day in range(1, 2501)],
        )
        conn.commit()
    finally:
        conn.close()

    response = client.put(
        f"/api/savings/2501?goal_id={goals[0]}",
        headers=headers,
        json={"amount": 1, "paid": True},
    )
    assert response.status_code == 429
