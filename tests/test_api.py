import os
import tempfile

# Путь к тестовой базе должен быть установлен ДО импорта main
tmp_db = os.path.join(tempfile.gettempdir(), "test_planner.db")
if os.path.exists(tmp_db):
    os.remove(tmp_db)
os.environ["DB_PATH"] = tmp_db
os.environ["SECRET_KEY"] = "test-secret-key"

from fastapi.testclient import TestClient
from main import app

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
