from fastapi.testclient import TestClient
from main import app, limiter

# Отключаем rate limit только в отдельном процессе тестов.
# В работающем production-процессе эта настройка не меняется.
limiter.enabled = False

client = TestClient(app)


def register_and_login(username):
    client.post("/register", data={"username": username, "password": "pass12345"})
    resp = client.post("/token", data={"username": username, "password": "pass12345"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_register_and_login():
    resp = client.post("/register", data={"username": "alice_test", "password": "pass12345"})
    assert resp.status_code == 200
    resp = client.post("/token", data={"username": "alice_test", "password": "pass12345"})
    assert resp.status_code == 200
    assert "access_token" in resp.json()


def test_login_with_wrong_password():
    resp = client.post("/token", data={"username": "alice_test", "password": "wrong"})
    assert resp.status_code == 401


def test_tasks_require_auth():
    resp = client.get("/tasks")
    assert resp.status_code == 401


def test_create_and_list_task():
    headers = register_and_login("bob_test")
    resp = client.post("/tasks", json={"title": "Написать тесты"}, headers=headers)
    assert resp.status_code == 200
    task_id = resp.json()["id"]

    resp = client.get("/tasks", headers=headers)
    assert resp.status_code == 200
    titles = [t["title"] for t in resp.json()]
    assert "Написать тесты" in titles


def test_update_task_status():
    headers = register_and_login("carol_test")
    resp = client.post("/tasks", json={"title": "Задача 2"}, headers=headers)
    task_id = resp.json()["id"]

    resp = client.put(f"/tasks/{task_id}", json={"status": "done"}, headers=headers)
    assert resp.status_code == 200

    resp = client.get("/tasks", headers=headers)
    statuses = {t["id"]: t["status"] for t in resp.json()}
    assert statuses[task_id] == "done"


def test_delete_task():
    headers = register_and_login("dave_test")
    resp = client.post("/tasks", json={"title": "На удаление"}, headers=headers)
    task_id = resp.json()["id"]

    resp = client.delete(f"/tasks/{task_id}", headers=headers)
    assert resp.status_code == 200

    resp = client.get("/tasks", headers=headers)
    assert task_id not in [t["id"] for t in resp.json()]

# === ТЕСТЫ КОПИЛКИ ===

def create_goal(headers, name="Тестовая цель", goal=1000, days=3):
    return client.post(
        "/api/savings/goals",
        json={
            "name": name,
            "goal": goal,
            "days": days,
            "start_date": "2026-01-01",
            "end_date": "2026-01-03",
        },
        headers=headers,
    )


def test_savings_goals_create_and_list():
    headers = register_and_login("savings_list_test")
    first = create_goal(headers, "Отпуск", 5000)
    second = create_goal(headers, "Резерв", 10000)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["name"] == "Отпуск"
    assert second.json()["goal"] == 10000

    response = client.get("/api/savings/goals", headers=headers)
    assert response.status_code == 200
    goals = response.json()
    names = [goal["name"] for goal in goals]
    assert "Отпуск" in names
    assert "Резерв" in names


def test_savings_goal_update():
    headers = register_and_login("savings_update_test")
    created = create_goal(headers)
    assert created.status_code == 200, created.text
    goal_id = created.json()["id"]

    response = client.put(
        f"/api/savings/goals/{goal_id}",
        json={
            "name": "Обновлённая цель",
            "goal": 2500,
            "days": 3,
            "start_date": "2026-01-01",
            "end_date": "2026-01-03",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Обновлённая цель"
    assert response.json()["goal"] == 2500


def test_savings_calendar_save_and_read():
    headers = register_and_login("savings_calendar_test")
    created = create_goal(headers)
    assert created.status_code == 200, created.text
    goal_id = created.json()["id"]

    saved = client.put(
        f"/api/savings/1?goal_id={goal_id}",
        json={"amount": 125.50, "paid": True},
        headers=headers,
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["amount"] == 125.50
    assert saved.json()["paid"] is True

    response = client.get(
        f"/api/savings?goal_id={goal_id}",
        headers=headers,
    )
    assert response.status_code == 200
    calendar = response.json()
    assert len(calendar) == 3
    assert calendar[0]["amount"] == 125.50
    assert calendar[0]["paid"] is True
    assert calendar[1]["amount"] == 0
    assert calendar[1]["paid"] is False


def test_savings_summary_counts_only_paid_entries():
    headers = register_and_login("savings_summary_test")
    created = create_goal(headers, "Сводка", 1000)
    assert created.status_code == 200, created.text
    goal_id = created.json()["id"]

    for day, amount, paid in [(1, 100, True), (2, 50, False)]:
        response = client.put(
            f"/api/savings/{day}?goal_id={goal_id}",
            json={"amount": amount, "paid": paid},
            headers=headers,
        )
        assert response.status_code == 200, response.text

    response = client.get("/api/savings/summary", headers=headers)
    assert response.status_code == 200, response.text
    summary = response.json()
    target = next(item for item in summary["goals"] if item["id"] == goal_id)
    assert target["saved"] == 100
    assert target["remaining"] == 900
    assert target["progress_percent"] == 10


def test_savings_archive_and_restore():
    headers = register_and_login("savings_archive_test")
    created = create_goal(headers, "В архив", 500)
    assert created.status_code == 200, created.text
    goal_id = created.json()["id"]

    archived = client.post(
        f"/api/savings/goals/{goal_id}/archive",
        headers=headers,
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived"] is True
    # Архивирование последней активной цели создаёт замену.
    assert archived.json()["replacement_id"] is not None

    archive_list = client.get(
        "/api/savings/goals?archived=true",
        headers=headers,
    )
    assert archive_list.status_code == 200
    assert any(item["id"] == goal_id for item in archive_list.json())

    restored = client.post(
        f"/api/savings/goals/{goal_id}/restore",
        headers=headers,
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["id"] == goal_id

    active = client.get("/api/savings/goals", headers=headers)
    assert active.status_code == 200
    assert any(item["id"] == goal_id for item in active.json())


def test_savings_rejects_negative_amount():
    headers = register_and_login("savings_invalid_test")
    created = create_goal(headers)
    assert created.status_code == 200, created.text
    goal_id = created.json()["id"]

    response = client.put(
        f"/api/savings/1?goal_id={goal_id}",
        json={"amount": -1, "paid": True},
        headers=headers,
    )
    assert response.status_code == 400


def test_savings_goal_is_private_to_owner():
    owner_headers = register_and_login("savings_owner_test")
    other_headers = register_and_login("savings_other_test")

    created = create_goal(owner_headers, "Личная цель")
    assert created.status_code == 200, created.text
    goal_id = created.json()["id"]

    response = client.put(
        f"/api/savings/goals/{goal_id}",
        json={"name": "Чужое изменение"},
        headers=other_headers,
    )
    assert response.status_code == 404


def test_cannot_delete_last_active_savings_goal():
    headers = register_and_login("savings_delete_test")
    first = create_goal(headers, "Первая")
    second = create_goal(headers, "Вторая")
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    deleted = client.delete(
        f"/api/savings/goals/{first.json()['id']}",
        headers=headers,
    )
    assert deleted.status_code == 200, deleted.text

    last = client.delete(
        f"/api/savings/goals/{second.json()['id']}",
        headers=headers,
    )
    assert last.status_code == 400


def test_savings_goal_rejects_invalid_parameters():
    headers = register_and_login("savings_bad_params_test")

    invalid_payloads = [
        {"name": "Нулевая цель", "goal": 0, "days": 3},
        {"name": "Отрицательная цель", "goal": -1, "days": 3},
        {"name": "Слишком большая цель", "goal": 1000000000001, "days": 3},
        {"name": "Нулевой срок", "goal": 1000, "days": 0},
        {"name": "Слишком большой срок", "goal": 1000, "days": 3651},
        {"name": "   ", "goal": 1000, "days": 3},
    ]

    for payload in invalid_payloads:
        response = client.post(
            "/api/savings/goals",
            json=payload,
            headers=headers,
        )
        assert response.status_code == 400, (
            f"Ожидался HTTP 400 для {payload}, "
            f"получено {response.status_code}: {response.text}"
        )


def test_savings_goal_rejects_invalid_date_ranges():
    headers = register_and_login("savings_bad_dates_test")

    invalid_payloads = [
        {
            "name": "Несуществующая дата",
            "goal": 1000,
            "days": 3,
            "start_date": "2026-02-30",
            "end_date": "2026-03-04",
        },
        {
            "name": "Обратный период",
            "goal": 1000,
            "days": 3,
            "start_date": "2026-01-03",
            "end_date": "2026-01-01",
        },
        {
            "name": "Слишком длинный период",
            "goal": 1000,
            "days": 3,
            "start_date": "2020-01-01",
            "end_date": "2030-01-01",
        },
    ]

    for payload in invalid_payloads:
        response = client.post(
            "/api/savings/goals",
            json=payload,
            headers=headers,
        )
        assert response.status_code == 400, (
            f"Ожидался HTTP 400 для {payload}, "
            f"получено {response.status_code}: {response.text}"
        )


def test_savings_calendar_rejects_out_of_range_days():
    headers = register_and_login("savings_bad_day_test")
    created = create_goal(headers)
    assert created.status_code == 200, created.text
    goal_id = created.json()["id"]

    for day in (0, 4, 3651):
        response = client.put(
            f"/api/savings/{day}?goal_id={goal_id}",
            json={"amount": 10, "paid": True},
            headers=headers,
        )
        assert response.status_code == 400, (
            f"День {day}: ожидался HTTP 400, "
            f"получено {response.status_code}: {response.text}"
        )


def test_savings_calendar_rejects_non_finite_amounts():
    headers = register_and_login("savings_nonfinite_test")
    created = create_goal(headers)
    assert created.status_code == 200, created.text
    goal_id = created.json()["id"]

    for amount in ("NaN", "Infinity", "-Infinity"):
        payload = (
            '{"amount": ' + amount + ', "paid": true}'
        )
        response = client.put(
            f"/api/savings/1?goal_id={goal_id}",
            content=payload,
            headers={**headers, "Content-Type": "application/json"},
        )
        assert response.status_code == 400, (
            f"Сумма {amount}: ожидался HTTP 400, "
            f"получено {response.status_code}: {response.text}"
        )


def test_savings_calendar_cannot_shrink_below_saved_days():
    headers = register_and_login("savings_shrink_calendar_test")
    created = create_goal(headers)
    assert created.status_code == 200, created.text
    goal_id = created.json()["id"]

    saved = client.put(
        f"/api/savings/3?goal_id={goal_id}",
        json={"amount": 300, "paid": True},
        headers=headers,
    )
    assert saved.status_code == 200, saved.text

    response = client.put(
        f"/api/savings/goals/{goal_id}",
        json={
            "name": "Тестовая цель",
            "goal": 1000,
            "days": 2,
            "start_date": "2026-01-01",
            "end_date": "2026-01-02",
        },
        headers=headers,
    )
    assert response.status_code == 400, response.text

    calendar = client.get(
        f"/api/savings?goal_id={goal_id}",
        headers=headers,
    )
    assert calendar.status_code == 200, calendar.text
    assert len(calendar.json()) == 3
    assert calendar.json()[2]["amount"] == 300


def test_savings_goal_is_private_for_calendar_archive_and_delete():
    owner_headers = register_and_login("savings_private_owner_test")
    other_headers = register_and_login("savings_private_other_test")

    created = create_goal(owner_headers, "Чужая цель")
    assert created.status_code == 200, created.text
    goal_id = created.json()["id"]

    # Чужой пользователь не может прочитать календарь или настройки.
    response = client.get(
        f"/api/savings?goal_id={goal_id}",
        headers=other_headers,
    )
    assert response.status_code == 404

    response = client.get(
        f"/api/savings/settings?goal_id={goal_id}",
        headers=other_headers,
    )
    assert response.status_code == 404

    # Нельзя записать данные в чужую цель.
    response = client.put(
        f"/api/savings/1?goal_id={goal_id}",
        json={"amount": 999, "paid": True},
        headers=other_headers,
    )
    assert response.status_code == 404

    # Нельзя архивировать или удалить чужую цель.
    response = client.post(
        f"/api/savings/goals/{goal_id}/archive",
        headers=other_headers,
    )
    assert response.status_code == 404

    response = client.delete(
        f"/api/savings/goals/{goal_id}",
        headers=other_headers,
    )
    assert response.status_code == 404

    # Проверяем, что владелец по-прежнему видит исходные данные.
    calendar = client.get(
        f"/api/savings?goal_id={goal_id}",
        headers=owner_headers,
    )
    assert calendar.status_code == 200, calendar.text
    assert calendar.json()[0]["amount"] == 0
    assert calendar.json()[0]["paid"] is False


def test_archiving_goal_preserves_saved_entries():
    headers = register_and_login("savings_archive_data_test")
    created = create_goal(headers, "Цель с данными")
    assert created.status_code == 200, created.text
    goal_id = created.json()["id"]

    saved = client.put(
        f"/api/savings/2?goal_id={goal_id}",
        json={"amount": 250, "paid": True},
        headers=headers,
    )
    assert saved.status_code == 200, saved.text

    archived = client.post(
        f"/api/savings/goals/{goal_id}/archive",
        headers=headers,
    )
    assert archived.status_code == 200, archived.text

    archived_list = client.get(
        "/api/savings/goals?archived=true",
        headers=headers,
    )
    assert archived_list.status_code == 200, archived_list.text
    assert any(item["id"] == goal_id for item in archived_list.json())

    restored = client.post(
        f"/api/savings/goals/{goal_id}/restore",
        headers=headers,
    )
    assert restored.status_code == 200, restored.text

    calendar = client.get(
        f"/api/savings?goal_id={goal_id}",
        headers=headers,
    )
    assert calendar.status_code == 200, calendar.text
    assert calendar.json()[1]["amount"] == 250
    assert calendar.json()[1]["paid"] is True
