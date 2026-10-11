from fastapi.testclient import TestClient

from todo.app import app

client = TestClient(app)


def test_create_then_list():
    created = client.post("/todos", params={"title": "write the readme"}).json()
    titles = [t["title"] for t in client.get("/todos").json()]
    assert created["id"] and "write the readme" in titles
