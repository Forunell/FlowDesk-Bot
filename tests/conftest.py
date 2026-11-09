import re

import pytest
from fastapi.testclient import TestClient

from flowdesk.config import Settings
from flowdesk.web import create_app

PASSWORD = "test-only-phrase-123456"


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path / "isolated-test.sqlite3", PASSWORD)


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://localhost") as client:
        yield client


def csrf(client, path="/demo"):
    response = client.get(path)
    return re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)


def send(client, message=None):
    data = {"csrf": csrf(client)}
    if message is not None:
        data["message"] = message
    return client.post("/demo/message", data=data)


def fill(client, name="Марта Приклад", description="Потрібно налаштувати тестовий ноутбук."):
    for value in ["/start", "Комп’ютерна допомога", name, "demo@example.com", description]:
        response = send(client, value)
        assert response.status_code == 200
    return response


def submit(client, **kwargs):
    fill(client, **kwargs)
    return send(client, "Підтвердити")


def login(client):
    response = client.post("/admin/login", data={"csrf": csrf(client, "/admin/login"), "password": PASSWORD})
    assert response.url.path == "/admin"
    return response
