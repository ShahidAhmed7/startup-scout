from fastapi import FastAPI
from fastapi.testclient import TestClient

from scout import archive


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        if "from cards c join startups" in sql:
            return Rows([{"id": 7, "run_date": "2026-11-19", "market": "bangladesh", "model_id": "gemini-4-hydrogen",
                          "name": "Render <script>", "country": "USA", "category": "marketing", "status": None,
                          "bd": 8, "gl": 7}])
        return Rows([])


def client(monkeypatch, pw="s3cret"):
    if pw:
        monkeypatch.setenv("ARCHIVE_PASSWORD", pw)
    else:
        monkeypatch.delenv("ARCHIVE_PASSWORD", raising=False)
    app = FastAPI()
    archive.add_routes(app, Conn)
    return TestClient(app)


def test_password_required(monkeypatch):
    c = client(monkeypatch)
    assert c.get("/archive").status_code == 401
    assert c.get("/archive", auth=("owner", "wrong")).status_code == 401
    r = c.get("/archive", auth=("owner", "s3cret"))
    assert r.status_code == 200 and "Every card (1)" in r.text


def test_html_is_escaped(monkeypatch):
    r = client(monkeypatch).get("/archive", auth=("x", "s3cret"))
    assert "<script>" not in r.text and "&lt;script&gt;" in r.text


def test_archive_off_without_secret(monkeypatch):
    assert client(monkeypatch, pw=None).get("/archive", auth=("x", "")).status_code == 401
