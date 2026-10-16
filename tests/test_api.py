from fastapi.testclient import TestClient
from main import app, limiter, db

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


def test_old_token_is_rejected_after_token_version_changes():
    headers = register_and_login("token_version_test")
    assert client.get("/users/me", headers=headers).status_code == 200
    conn = db()
    try:
        conn.execute(
            "UPDATE users SET token_version = token_version + 1 WHERE username = ?",
            ("token_version_test",),
        )
        conn.commit()
    finally:
        conn.close()
    assert client.get("/users/me", headers=headers).status_code == 401


def test_change_password_invalidates_old_token():
    headers = register_and_login("password_change_test")

    resp = client.post(
        "/users/me/password",
        json={
            "current_password": "pass12345",
            "new_password": "new-secure-pass-123",
        },
        headers=headers,
    )
    assert resp.status_code == 200
    assert client.get("/users/me", headers=headers).status_code == 401

    old_login = client.post(
        "/token",
        data={"username": "password_change_test", "password": "pass12345"},
    )
    assert old_login.status_code == 401

    new_login = client.post(
        "/token",
        data={
            "username": "password_change_test",
            "password": "new-secure-pass-123",
        },
    )
    assert new_login.status_code == 200


def test_change_password_rejects_wrong_current_password():
    headers = register_and_login("password_wrong_current_test")
    resp = client.post(
        "/users/me/password",
        json={
            "current_password": "wrong-password",
            "new_password": "new-secure-pass-123",
        },
        headers=headers,
    )
    assert resp.status_code == 400
    assert client.get("/users/me", headers=headers).status_code == 200


def test_change_password_rejects_short_new_password():
    headers = register_and_login("password_short_test")
    resp = client.post(
        "/users/me/password",
        json={
            "current_password": "pass12345",
            "new_password": "short",
        },
        headers=headers,
    )
    assert resp.status_code == 400
    assert client.get("/users/me", headers=headers).status_code == 200


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

# === ТЕСТЫ ГОДОВОГО БЮДЖЕТА ===

def create_budget_item(headers, **overrides):
    item = {
        "title": "Оплата интернета",
        "amount_kopecks": 150000,
        "item_type": "expense",
        "category": "Связь",
        "due_date": "2026-01-15",
        "mandatory": True,
        "status": "planned",
        "notes": "",
    }
    item.update(overrides)
    return client.post("/api/budget/items", json=item, headers=headers)


def test_budget_requires_auth():
    response = client.get("/api/budget/items?year=2026")
    assert response.status_code == 401


def test_budget_create_and_filter_by_year():
    headers = register_and_login("budget_create_test")
    created = create_budget_item(headers)
    assert created.status_code == 200, created.text
    item = created.json()
    assert item["title"] == "Оплата интернета"
    assert item["amount_kopecks"] == 150000
    assert item["mandatory"] is True
    assert item["user_id"] > 0

    other_year = create_budget_item(
        headers,
        title="Платёж следующего года",
        due_date="2027-02-01",
        item_type="expense",
    )
    assert other_year.status_code == 200, other_year.text

    response = client.get("/api/budget/items?year=2026", headers=headers)
    assert response.status_code == 200
    items = response.json()
    assert item["id"] in [entry["id"] for entry in items]
    assert other_year.json()["id"] not in [entry["id"] for entry in items]


def test_budget_create_income():
    headers = register_and_login("budget_income_test")
    response = create_budget_item(
        headers,
        title="Зарплата",
        amount_kopecks=12500000,
        item_type="income",
        category="Зарплата",
        due_date="2026-03-05",
        mandatory=False,
    )
    assert response.status_code == 200, response.text
    assert response.json()["item_type"] == "income"
    assert response.json()["amount_kopecks"] == 12500000


def test_budget_rejects_invalid_amount_and_type():
    headers = register_and_login("budget_validation_test")

    zero = create_budget_item(headers, amount_kopecks=0)
    assert zero.status_code == 422

    negative = create_budget_item(headers, amount_kopecks=-100)
    assert negative.status_code == 422

    bad_type = create_budget_item(headers, item_type="transfer")
    assert bad_type.status_code == 422


def test_budget_update_moves_item_to_another_month():
    headers = register_and_login("budget_move_test")
    created = create_budget_item(headers, due_date="2026-01-15")
    assert created.status_code == 200, created.text
    item_id = created.json()["id"]

    updated = client.put(
        f"/api/budget/items/{item_id}",
        json={"due_date": "2026-04-20", "status": "paid"},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["due_date"] == "2026-04-20"
    assert updated.json()["status"] == "paid"

    january = client.get("/api/budget/items?year=2026", headers=headers)
    assert item_id in [item["id"] for item in january.json()]
    assert next(item for item in january.json() if item["id"] == item_id)["due_date"] == "2026-04-20"


def test_budget_prevents_access_to_another_users_item():
    owner_headers = register_and_login("budget_owner_test")
    other_headers = register_and_login("budget_other_test")

    created = create_budget_item(owner_headers)
    assert created.status_code == 200, created.text
    item_id = created.json()["id"]

    listing = client.get("/api/budget/items?year=2026", headers=other_headers)
    assert listing.status_code == 200
    assert item_id not in [item["id"] for item in listing.json()]

    update = client.put(
        f"/api/budget/items/{item_id}",
        json={"title": "Чужая операция"},
        headers=other_headers,
    )
    assert update.status_code == 404

    delete = client.delete(
        f"/api/budget/items/{item_id}",
        headers=other_headers,
    )
    assert delete.status_code == 404

    owner_listing = client.get("/api/budget/items?year=2026", headers=owner_headers)
    assert item_id in [item["id"] for item in owner_listing.json()]


def test_budget_delete_item():
    headers = register_and_login("budget_delete_test")
    created = create_budget_item(headers)
    assert created.status_code == 200, created.text
    item_id = created.json()["id"]

    deleted = client.delete(f"/api/budget/items/{item_id}", headers=headers)
    assert deleted.status_code == 200, deleted.text

    listing = client.get("/api/budget/items?year=2026", headers=headers)
    assert item_id not in [item["id"] for item in listing.json()]

# === ДОПОЛНИТЕЛЬНЫЕ ГРАНИЧНЫЕ ТЕСТЫ БЮДЖЕТА ===

def test_budget_rejects_invalid_date_and_blank_title():
    headers = register_and_login("budget_edge_validation")

    invalid_date = create_budget_item(headers, due_date="2026-02-30")
    assert invalid_date.status_code == 422

    blank_title = create_budget_item(headers, title="")
    assert blank_title.status_code == 422


def test_budget_rejects_invalid_year_range():
    headers = register_and_login("budget_edge_year")

    too_early = client.get(
        "/api/budget/items?year=1899",
        headers=headers,
    )
    assert too_early.status_code == 400

    too_late = client.get(
        "/api/budget/items?year=2201",
        headers=headers,
    )
    assert too_late.status_code == 400


def test_budget_partial_update_preserves_other_fields():
    headers = register_and_login("budget_edge_partial_update")
    created = create_budget_item(headers)
    assert created.status_code == 200, created.text
    original = created.json()

    updated = client.put(
        f"/api/budget/items/{original['id']}",
        json={"status": "paid"},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text

    result = updated.json()
    assert result["status"] == "paid"
    assert result["title"] == original["title"]
    assert result["amount_kopecks"] == original["amount_kopecks"]
    assert result["due_date"] == original["due_date"]


def test_budget_rejects_invalid_update_values():
    headers = register_and_login("budget_edge_update_validation")
    created = create_budget_item(headers)
    assert created.status_code == 200, created.text
    item_id = created.json()["id"]

    invalid_amount = client.put(
        f"/api/budget/items/{item_id}",
        json={"amount_kopecks": 0},
        headers=headers,
    )
    assert invalid_amount.status_code == 422

    invalid_status = client.put(
        f"/api/budget/items/{item_id}",
        json={"status": "cancelled"},
        headers=headers,
    )
    assert invalid_status.status_code == 422

# === ТЕСТЫ СВОДКИ ГОДОВОГО БЮДЖЕТА ===

def test_budget_summary_returns_twelve_months_and_totals():
    headers = register_and_login("budget_summary_totals")

    income = create_budget_item(
        headers,
        title="Зарплата",
        amount_kopecks=30000000,
        item_type="income",
        due_date="2026-01-05",
    )
    expense = create_budget_item(
        headers,
        title="Аренда",
        amount_kopecks=10000000,
        item_type="expense",
        due_date="2026-01-10",
        mandatory=True,
        status="paid",
    )
    unpaid = create_budget_item(
        headers,
        title="Покупки",
        amount_kopecks=2500000,
        item_type="expense",
        due_date="2026-01-20",
        mandatory=False,
        status="planned",
    )
    next_year = create_budget_item(
        headers,
        title="Платёж следующего года",
        amount_kopecks=5000000,
        item_type="expense",
        due_date="2027-01-10",
    )

    for response in (income, expense, unpaid, next_year):
        assert response.status_code == 200, response.text

    response = client.get(
        "/api/budget/summary?year=2026",
        headers=headers,
    )
    assert response.status_code == 200, response.text

    result = response.json()
    assert result["year"] == 2026
    assert len(result["months"]) == 12

    january = result["months"][0]
    assert january["month"] == 1
    assert january["income_kopecks"] == 30000000
    assert january["expense_kopecks"] == 12500000
    assert january["balance_kopecks"] == 17500000
    assert january["paid_expense_kopecks"] == 10000000
    assert january["mandatory_expense_kopecks"] == 10000000

    february = result["months"][1]
    assert february["income_kopecks"] == 0
    assert february["expense_kopecks"] == 0
    assert february["balance_kopecks"] == 0


def test_budget_summary_requires_auth_and_valid_year():
    unauthorized = client.get("/api/budget/summary?year=2026")
    assert unauthorized.status_code == 401

    headers = register_and_login("budget_summary_year")
    invalid = client.get(
        "/api/budget/summary?year=2201",
        headers=headers,
    )
    assert invalid.status_code == 400

def test_budget_summary_isolated_between_users():
    owner_headers = register_and_login("budget_summary_owner")
    other_headers = register_and_login("budget_summary_other")

    created = create_budget_item(
        owner_headers,
        title="Личный доход",
        amount_kopecks=9876543,
        item_type="income",
        due_date="2026-06-10",
    )
    assert created.status_code == 200, created.text

    owner_summary = client.get(
        "/api/budget/summary?year=2026",
        headers=owner_headers,
    )
    other_summary = client.get(
        "/api/budget/summary?year=2026",
        headers=other_headers,
    )

    assert owner_summary.status_code == 200, owner_summary.text
    assert other_summary.status_code == 200, other_summary.text

    owner_june = owner_summary.json()["months"][5]
    other_june = other_summary.json()["months"][5]

    assert owner_june["income_kopecks"] == 9876543
    assert other_june["income_kopecks"] == 0


def test_budget_rejects_whitespace_only_title_and_category():
    headers = register_and_login("budget_whitespace_validation")

    blank_title = create_budget_item(headers, title="   ")
    assert blank_title.status_code == 422

    blank_category = create_budget_item(headers, category="   ")
    assert blank_category.status_code == 422


def test_budget_update_rejects_null_and_whitespace_fields():
    headers = register_and_login("budget_null_validation")
    created = create_budget_item(headers)
    assert created.status_code == 200, created.text
    item_id = created.json()["id"]

    for payload in (
        {"title": None},
        {"amount_kopecks": None},
        {"due_date": None},
        {"title": "   "},
        {"category": "   "},
    ):
        response = client.put(
            f"/api/budget/items/{item_id}",
            json=payload,
            headers=headers,
        )
        assert response.status_code == 422, (
            f"Payload {payload!r}: {response.status_code} {response.text}"
        )



def test_budget_monthly_recurrence_creates_twelve_items():
    headers = register_and_login("budget_monthly_expense")
    response = create_budget_item(
        headers,
        title="Аренда",
        amount_kopecks=3500000,
        due_date="2028-01-31",
        repeat_monthly=True,
        status="paid",
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["repeat_monthly"] is True
    assert data["created_count"] == 12

    items_response = client.get(
        "/api/budget/items?year=2028", headers=headers
    )
    assert items_response.status_code == 200
    items = [x for x in items_response.json() if x["title"] == "Аренда"]
    assert len(items) == 12
    assert [x["due_date"] for x in items][1] == "2028-02-29"
    assert [x["due_date"] for x in items][-1] == "2028-12-31"
    assert all(x["status"] == "planned" for x in items)
    assert all(x["amount_kopecks"] == 3500000 for x in items)


def test_budget_monthly_income_and_independent_updates():
    headers = register_and_login("budget_monthly_income")
    response = create_budget_item(
        headers,
        title="Зарплата",
        amount_kopecks=12000000,
        item_type="income",
        due_date="2026-01-10",
        repeat_monthly=True,
    )
    assert response.status_code == 200, response.text
    assert response.json()["created_count"] == 12

    items = client.get(
        "/api/budget/items?year=2026", headers=headers
    ).json()
    salaries = [x for x in items if x["title"] == "Зарплата"]
    assert len(salaries) == 12
    assert all(x["item_type"] == "income" for x in salaries)

    march = next(x for x in salaries if x["due_date"] == "2026-03-10")
    updated = client.put(
        f"/api/budget/items/{march['id']}",
        json={"amount_kopecks": 13000000},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text

    refreshed = client.get(
        "/api/budget/items?year=2026", headers=headers
    ).json()
    salaries = [x for x in refreshed if x["title"] == "Зарплата"]
    assert len(salaries) == 12
    assert next(x for x in salaries if x["due_date"] == "2026-03-10")[
        "amount_kopecks"
    ] == 13000000
    assert next(x for x in salaries if x["due_date"] == "2026-04-10")[
        "amount_kopecks"
    ] == 12000000


def test_budget_monthly_recurrence_isolated_between_users():
    owner = register_and_login("budget_monthly_owner")
    other = register_and_login("budget_monthly_other")
    response = create_budget_item(
        owner,
        title="Интернет",
        due_date="2026-01-15",
        repeat_monthly=True,
    )
    assert response.status_code == 200, response.text

    other_items = client.get(
        "/api/budget/items?year=2026", headers=other
    )
    assert other_items.status_code == 200
    assert all(x["title"] != "Интернет" for x in other_items.json())
