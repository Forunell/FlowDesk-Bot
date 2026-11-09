import csv
import io
import json
import socket
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from flowdesk.dialogue import interact, validate
from flowdesk.storage import csv_safe
from flowdesk.web import COOKIE, create_app

from .conftest import csrf, fill, login, send, submit


def test_complete_demo_works_without_network(client, app, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Demo attempted a network connection")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    response = submit(client)
    assert "FD-0001" in response.text
    row = app.state.database.get(1)
    assert row["name"] == "Марта Приклад"
    assert row["is_demo"] == 1 and row["status"] == "new"


def test_cancel_edit_and_second_request(client, app):
    fill(client)
    send(client, "Редагувати")
    send(client, "Ім’я")
    response = send(client, "Софія Тестова")
    assert "Софія Тестова" in response.text
    send(client, "Скасувати")
    assert app.state.database.counts()["new"] == 0
    submit(client, name="Данило Демо")
    with app.state.database.connect() as db:
        db.execute("UPDATE requests SET submitted_at=submitted_at-31")
    submit(client, name="Лада Приклад")
    assert app.state.database.counts()["new"] == 2


def test_edit_each_field_and_back(client, app):
    fill(client)
    for label, value in [("Категорія", "Консультація"), ("Ім’я", "Лада Демо"),
                         ("Контакт", "@demo_user"), ("Опис", "Оновлений навчальний запит")]:
        send(client, "Редагувати")
        send(client, label)
        send(client, value)
    send(client, "Редагувати")
    send(client, "Назад")
    send(client, "Підтвердити")
    row = app.state.database.get(1)
    assert (row["category"], row["name"], row["contact"], row["description"]) == (
        "Консультація", "Лада Демо", "@demo_user", "Оновлений навчальний запит")


def test_empty_nontext_invalid_and_oversized(client, app):
    send(client, "/start")
    assert "Надішліть текстове" in send(client).text
    send(client, "Невідома послуга")
    send(client, "Комп’ютерна допомога")
    send(client, "А")
    assert app.state.database.counts()["new"] == 0
    send(client, "Марта Приклад")
    assert "Перевірте контакт" in send(client, "no-contact").text
    send(client, "demo@example.com")
    send(client, "Мало")
    response = send(client, "x" * 2001)
    assert "Повідомлення задовге" in response.text
    assert app.state.database.counts()["new"] == 0
    token = csrf(client)
    response = client.post("/demo/message", data={"csrf": token}, files={"message": ("note.txt", b"test")})
    assert "Надішліть текстове" in response.text


@pytest.mark.parametrize("contact", ["+380000000000", "demo@example.com", "@demo_user"])
def test_valid_contact(contact):
    assert validate("contact", contact) is None


@pytest.mark.parametrize("contact", ["abc", "@a", "+0123", "a@b", "<script>", "a\n@example.com"])
def test_invalid_contact(contact):
    assert validate("contact", contact)


def test_duplicate_confirmation_and_cooldown(client, app):
    submit(client)
    send(client, "Підтвердити")
    assert app.state.database.counts()["new"] == 1
    response = submit(client)
    assert "Повтор не створено" in response.text
    response = submit(client, description="Інший демонстраційний запит")
    assert "Зачекайте 30 секунд" in response.text
    assert app.state.database.counts()["new"] == 1
    with app.state.database.connect() as db:
        db.execute("UPDATE requests SET submitted_at=submitted_at-31")
    send(client, "Підтвердити")
    assert app.state.database.counts()["new"] == 2


def test_atomic_concurrent_confirmation(client, app):
    fill(client)
    database = app.state.database
    session, _ = database.session(client.cookies.get(COOKIE))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: interact(database, session["id"], "Підтвердити"), range(2)))
    assert sum(created is not None for _, created in results) == 1
    assert database.counts()["new"] == 1


@pytest.mark.parametrize("path", ["/admin", "/admin/export.csv", "/admin/requests/1", "/admin/clear"])
def test_admin_get_routes_protected(client, path):
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


@pytest.mark.parametrize("path", ["/admin/seed", "/admin/clear", "/admin/requests/1", "/admin/logout"])
def test_admin_post_routes_protected(client, path):
    response = client.post(path, data={"csrf": csrf(client)}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_csrf_session_rotation_cookie_and_logout(client):
    response = client.get("/demo")
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    original_cookie = client.cookies.get(COOKIE)
    assert client.post("/demo/message", data={"message": "/start"}).status_code == 403
    assert client.post("/admin/login", data={"password": "wrong"}).status_code == 403
    login(client)
    assert original_cookie != client.cookies.get(COOKIE)
    for path in ["/admin/seed", "/admin/clear", "/admin/requests/1", "/admin/logout"]:
        assert client.post(path, data={"csrf": "forged"}).status_code == 403
    cookie = client.cookies.get(COOKIE)
    client.post("/admin/logout", data={"csrf": csrf(client, "/admin")})
    client.cookies.set(COOKIE, cookie, domain="localhost.local", path="/")
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_login_rate_limit_persists_restart(client, settings):
    for _ in range(5):
        response = client.post("/admin/login", data={"csrf": csrf(client, "/admin/login"), "password": "wrong"})
        assert response.status_code == 401
    with TestClient(create_app(settings), base_url="http://localhost") as restarted:
        response = restarted.post("/admin/login", data={"csrf": csrf(restarted, "/admin/login"), "password": settings.password})
        assert response.status_code == 429


def test_status_notes_search_filters_and_csv(client, app):
    submit(client)
    login(client)
    response = client.post("/admin/requests/1", data={"csrf": csrf(client, "/admin"), "status": "working", "notes": "=SUM(1,2)", "version": "1"})
    assert "Зміни збережено" in response.text
    assert app.state.database.get(1)["status"] == "working"
    assert "Марта Приклад" in client.get("/admin?query=марта&status=working").text
    assert "Марта Приклад" in client.get("/admin?query=FD-0001").text
    assert "Нічого не знайдено" in client.get("/admin?status=closed").text
    assert "Нічого не знайдено" in client.get("/admin?start=2099-01-01").text
    assert "Нічого не знайдено" in client.get("/admin?end=2000-01-01").text
    response = client.get("/admin/export.csv?status=working")
    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.text.lstrip("\ufeff"))))
    assert len(rows) == 2 and rows[1][6] == "'=SUM(1,2)"
    assert len(list(csv.reader(io.StringIO(client.get("/admin/export.csv?status=closed").text)))) == 1
    client.post("/admin/requests/1", data={"csrf": csrf(client, "/admin"), "status": "closed", "notes": "Готово", "version": "2"})
    assert app.state.database.get(1)["status"] == "closed"


@pytest.mark.parametrize("value", ["=1+1", "+cmd", "-1", "@SUM(1)", "  =1", "\ttext", "\rtext", "\ntext"])
def test_formula_injection(value):
    assert csv_safe(value).startswith("'")


def test_html_injection_is_escaped(client):
    response = submit(client, name="<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in response.text
    login(client)
    response = client.get("/admin/requests/1")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "<script>alert(1)</script>" not in response.text


def test_seed_idempotency_and_demo_only_clear(client, app):
    login(client)
    for _ in range(2):
        client.post("/admin/seed", data={"csrf": csrf(client, "/admin")})
    assert sum(app.state.database.counts().values()) == 6
    with app.state.database.connect() as db:
        db.execute("UPDATE requests SET source='telegram', is_demo=0 WHERE id=1")
    token = csrf(client, "/admin")
    assert client.post("/admin/clear", data={"csrf": token, "confirmation": "wrong"}).status_code == 400
    assert sum(app.state.database.counts().values()) == 6
    client.post("/admin/clear", data={"csrf": token, "confirmation": "ВИДАЛИТИ"})
    assert sum(app.state.database.counts().values()) == 1
    assert app.state.database.get(1)["source"] == "telegram"


def test_persistence_restart_including_draft(client, settings, app):
    submit(client)
    send(client, "/start")
    send(client, "Консультація")
    with TestClient(create_app(settings), base_url="http://localhost") as restarted:
        restarted.cookies.update(client.cookies)
        response = restarted.get("/demo")
        assert "Як до вас звертатися" in response.text
        login(restarted)
        assert "Марта Приклад" in restarted.get("/admin").text
    assert app.state.database.path == settings.database


def test_server_validation_security_headers_and_host(client):
    response = client.get("/demo")
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert client.get("/demo", headers={"host": "attacker.example"}).status_code == 400
    assert client.post("/demo/message", content="x" * 17000).status_code == 413
    login(client)
    for query in ["status=unknown", "start=bad", "start=2026-02-02&end=2026-01-01", "page=bad"]:
        assert client.get("/admin?" + query).status_code == 400
    assert client.get("/admin/requests/999").status_code == 404
    token = csrf(client, "/admin")
    assert client.post("/admin/requests/1", data={"csrf": token, "status": "unknown"}).status_code == 400
    assert client.post("/admin/requests/1", data={"csrf": token, "status": "new", "notes": "x" * 5001}).status_code == 400


def test_secure_cookie_mode(settings):
    from dataclasses import replace
    app = create_app(replace(settings, secure_cookie=True))
    with TestClient(app, base_url="https://localhost") as client:
        assert "Secure" in client.get("/demo").headers["set-cookie"]


def test_password_change_revokes_old_admin_session(client, settings):
    from dataclasses import replace
    login(client)
    changed = create_app(replace(settings, password="a-new-test-only-password"))
    with TestClient(changed, base_url="http://localhost") as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get("/admin", follow_redirects=False).status_code == 303


def test_no_text_shared_telegram_state_machine(app):
    database = app.state.database
    session, _ = database.session()
    state, created = interact(database, session["id"], None, "telegram")
    assert created is None
    assert "Надішліть текстове" in state["history"][-1]["text"]
    with database.connect() as db:
        persisted = json.loads(db.execute("SELECT conversation FROM sessions WHERE id=?", (session["id"],)).fetchone()[0])
    assert persisted == state
