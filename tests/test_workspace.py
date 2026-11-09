import asyncio
import re
import sqlite3
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from flowdesk.storage import Database
from flowdesk.telegram_service import deliver_one, receive
from flowdesk.web import create_app
from flowdesk.workspace import add_message, change, sla_state
from tests.conftest import csrf, login, submit


def post(client, path, **values):
    return client.post(path, data={"csrf": csrf(client, "/admin"), **values})


def test_create_form_persistence_and_idempotency(client, app, settings):
    login(client)
    page = client.get("/admin/new")
    nonce = re.search(r'name="nonce" value="([^"]+)"', page.text)[1]
    data = dict(title="Нова тестова заявка", category="Консультація", name="Тестовий Клієнт",
                contact="test@example.com", description="Потрібна тестова консультація.", priority="high", nonce=nonce)
    for _ in range(2):
        assert post(client, "/admin/new", **data).status_code == 200
    assert app.state.database.counts()["new"] == 1
    restarted = Database(settings.database)
    assert restarted.get(1)["title"] == data["title"]
    assert post(client, "/admin/new", **{**data, "contact": "bad"}).status_code == 400


def test_fields_history_conflict_filters_and_kpi(client, app):
    submit(client)
    login(client)
    response = post(client, "/admin/requests/1/change", status="waiting", priority="high", assignee="admin", version="1")
    assert response.status_code == 200
    assert 'data-kpi="waiting">1<' in response.text
    assert app.state.database.get(1)["version"] == 2
    assert post(client, "/admin/requests/1/change", status="resolved", version="1").status_code == 409
    assert post(client, "/admin/requests/1/change", status="waiting", version="2").status_code == 200
    with app.state.database.connect() as db:
        assert db.execute("SELECT count(*) FROM events WHERE field='status'").fetchone()[0] == 1
    rows, total = app.state.database.list_requests(status="waiting", priority="high", assignee="admin", query="марта", sort="priority")
    assert total == 1 and rows[0]["id"] == 1
    assert app.state.database.list_requests(priority="low", query="марта")[1] == 0
    assert post(client, "/admin/requests/1/change", assignee="other", version="2").status_code == 400
    history = client.get("/admin/requests/1?tab=history")
    assert "Нова → Очікує" in history.text


def test_messages_are_idempotent_isolated_and_internal(client, app, settings):
    submit(client)
    login(client)
    post(client, "/admin/seed")
    for _ in range(2):
        assert post(client, "/admin/requests/1/messages", kind="note", body="Внутрішній секрет тесту", nonce="a" * 20).status_code == 200
    with app.state.database.connect() as db:
        messages = db.execute("SELECT * FROM messages WHERE request_id=1").fetchall()
    assert len(messages) == 1 and messages[0]["delivery"] == "local"
    assert "Внутрішній секрет тесту" not in client.get("/demo").text
    assert "Внутрішній секрет тесту" not in client.get("/admin/requests/2").text
    assert "Внутрішній секрет тесту" in client.get("/admin/requests/1").text
    with TestClient(create_app(settings), base_url="http://localhost") as restarted:
        login(restarted)
        assert "Внутрішній секрет тесту" in restarted.get("/admin/requests/1").text
    assert post(client, "/admin/requests/1/messages", kind="reply", body="Changed body", nonce="a" * 20).status_code == 409
    assert post(client, "/admin/requests/1/messages", kind="note", body=" ", nonce="b" * 20).status_code == 400
    assert post(client, "/admin/requests/1/messages", kind="incoming", body="Симуляція клієнта", nonce="c" * 20).status_code == 200


def test_archive_and_clear_preserve_real_tickets(client, app):
    submit(client)
    login(client)
    assert post(client, "/admin/requests/1/change", archived="1", version="1").status_code == 400
    assert post(client, "/admin/requests/1/change", archived="1", version="1", confirmation="yes").status_code == 200
    assert app.state.database.counts()["new"] == 0
    assert app.state.database.list_requests(archived="1")[1] == 1
    assert post(client, "/admin/requests/1/messages", kind="note", body="test", nonce="d" * 20).status_code == 409
    assert post(client, "/admin/requests/1/change", archived="0", version="2", confirmation="yes").status_code == 200
    post(client, "/admin/requests/1/messages", kind="note", body="test", nonce="e" * 20)
    post(client, "/admin/clear", confirmation="ВИДАЛИТИ")
    with app.state.database.connect() as db:
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_operator_permissions_and_revocation(settings):
    configured = replace(settings, operator_password="operator-test-password")
    with TestClient(create_app(configured), base_url="http://localhost") as client:
        client.post("/admin/login", data={"csrf": csrf(client, "/admin/login"), "password": configured.operator_password})
        assert client.get("/admin").status_code == 200
        for path, data in [("/admin/seed", {}), ("/admin/clear", {"confirmation": "ВИДАЛИТИ"}), ("/admin/settings", {"sla_hours": "4"})]:
            assert post(client, path, **data).status_code == 403
        assert client.get("/admin/clear").status_code == 403
        with TestClient(create_app(replace(configured, operator_password="changed-test-password")), base_url="http://localhost") as restarted:
            restarted.cookies.update(client.cookies)
            assert restarted.get("/admin", follow_redirects=False).status_code == 303


@pytest.mark.parametrize("path", ["/admin/new", "/admin/overview", "/admin/settings"])
def test_new_pages_require_auth(client, path):
    assert client.get(path, follow_redirects=False).status_code == 303


@pytest.mark.parametrize("path", ["/admin/new", "/admin/settings", "/admin/requests/1/change", "/admin/requests/1/messages"])
def test_new_mutations_require_auth_and_csrf(client, path):
    assert client.post(path).url.path == "/admin/login"
    login(client)
    assert client.post(path).status_code == 403


def test_sla_rule_and_internal_notes_do_not_stop_clock(client, app):
    submit(client)
    login(client)
    with app.state.database.connect() as db:
        db.execute("UPDATE requests SET created_at='2000-01-01T00:00:00+00:00'")
    row = app.state.database.get(1)
    assert not sla_state(row, [], 0)["risk"]
    assert sla_state(row, [{"kind": "note", "delivery": "local"}], 4)["risk"]
    assert not sla_state(row, [{"kind": "reply", "delivery": "sent"}], 4)["risk"]
    assert sla_state(row, [{"kind": "reply", "delivery": "failed"}], 4)["risk"]
    assert app.state.database.list_requests(attention="risk")[1] == 0
    assert post(client, "/admin/settings", sla_hours="4").status_code == 200
    assert app.state.database.list_requests(attention="risk")[1] == 1
    assert post(client, "/admin/settings", sla_hours="-1").status_code == 400


def test_legacy_migration_keeps_every_field_and_id(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL,category TEXT NOT NULL,name TEXT NOT NULL,
            contact TEXT NOT NULL,description TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','working','closed')),
            notes TEXT NOT NULL DEFAULT '',source TEXT NOT NULL,is_demo INTEGER NOT NULL,fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL,submitted_at REAL NOT NULL)""")
        db.execute("INSERT INTO requests VALUES(41,'actor','category','name','contact','description','closed','private note','telegram',0,'fp','2020-01-01T00:00:00+00:00',1)")
    migrated = Database(path)
    original = migrated.get(41)
    assert original["notes"] == "private note" and original["source"] == "telegram"
    assert original["title"] == "description"
    change(migrated, 41, {"status": "waiting"}, "test", "1")
    assert Database(path).get(41)["status"] == "waiting"


def test_telegram_inbound_dedup_and_outbound_transport(app, monkeypatch):
    database = app.state.database
    created = None
    values = ["/start", "Консультація", "Клієнт Демо", "demo@example.com", "Потрібна тестова консультація", "Підтвердити"]
    for number, text in enumerate(values):
        _, created = receive(database, 123, number, text)
    assert created == 1
    assert receive(database, 123, 5, "Підтвердити") == (None, None)
    receive(database, 123, 6, "Додаткові подробиці")
    receive(database, 123, 6, "Додаткові подробиці")
    with database.connect() as db:
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
    with pytest.raises(Exception) as error:
        add_message(database, 1, "reply", "Вітаємо", "operator", "f" * 20)
    assert error.value.status_code == 409
    monkeypatch.setenv("TELEGRAM_REPLIES_ENABLED", "true")
    add_message(database, 1, "note", "Не надсилати клієнту", "operator", "g" * 20)
    add_message(database, 1, "reply", "Вітаємо", "operator", "h" * 20)

    class FakeBot:
        sent = []

        async def send_message(self, chat, text):
            self.sent.append((chat, text))

    bot = FakeBot()
    assert asyncio.run(deliver_one(database, bot))
    assert not asyncio.run(deliver_one(database, bot))
    assert bot.sent == [(123, "Вітаємо")]
    with database.connect() as db:
        assert db.execute("SELECT delivery FROM messages WHERE kind='reply'").fetchone()[0] == "sent"


def test_telegram_failure_is_not_retried_automatically(app, monkeypatch):
    database = app.state.database
    database.seed()
    with database.connect() as db:
        db.execute("UPDATE requests SET source='telegram',is_demo=0,actor='telegram:123' WHERE id=2")
    monkeypatch.setenv("TELEGRAM_REPLIES_ENABLED", "true")
    add_message(database, 2, "reply", "Test", "operator", "j" * 20)

    class FailedBot:
        async def send_message(self, chat, text):
            raise RuntimeError("Fake network failure")

    assert asyncio.run(deliver_one(database, FailedBot()))
    assert not asyncio.run(deliver_one(database, FailedBot()))
    with database.connect() as db:
        assert db.execute("SELECT delivery FROM messages WHERE nonce=?", ("j" * 20,)).fetchone()[0] == "failed"
