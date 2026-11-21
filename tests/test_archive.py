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
            return Rows(
                [
                    {
                        "id": 7,
                        "run_date": "2026-11-19",
                        "market": "bangladesh",
                        "model_id": "gemini-4-hydrogen",
                        "name": "Render <script>",
                        "country": "USA",
                        "category": "marketing",
                        "status": None,
                        "bd": 8,
                        "gl": 7,
                    }
                ]
            )
        return Rows([])


def client(monkeypatch, pw="s3cret"):
    if pw:
        monkeypatch.setenv("ARCHIVE_PASSWORD", pw)
    else:
        monkeypatch.delenv("ARCHIVE_PASSWORD", raising=False)
    app = FastAPI()
    archive.add_routes(app, Conn)
    return TestClient(app)


def test_not_signed_in_goes_to_login(monkeypatch):
    c = client(monkeypatch)
    r = c.get("/archive", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/archive/login"
    assert (
        c.get("/archive", auth=("owner", "wrong"), follow_redirects=False).status_code
        == 303
    )
    r = c.get("/archive", auth=("owner", "s3cret"))
    assert r.status_code == 200 and "Every card (1)" in r.text


def test_password_form_sets_session(monkeypatch):
    c = client(monkeypatch)
    assert "Wrong password" in c.post("/archive/login", data={"password": "nope"}).text
    r = c.post("/archive/login", data={"password": "s3cret"}, follow_redirects=False)
    assert r.status_code == 303 and "scout_session" in r.headers["set-cookie"]


def test_whatsapp_link_signs_in_and_expires(monkeypatch):
    c = client(monkeypatch)
    link = archive.login_link("https://x")
    token = link.split("t=")[1]
    r = c.get("/archive/login", params={"t": token}, follow_redirects=False)
    assert r.status_code == 303 and "scout_session" in r.headers["set-cookie"]
    assert "expired" in c.get("/archive/login", params={"t": "123.abc"}).text
    assert not archive.check_token(
        "link", token.replace(token.split(".")[0], "1")
    )  # tampered expiry


def test_html_is_escaped(monkeypatch):
    r = client(monkeypatch).get("/archive", auth=("x", "s3cret"))
    assert "<script>" not in r.text and "&lt;script&gt;" in r.text


def test_archive_off_without_secret(monkeypatch):
    assert (
        client(monkeypatch, pw=None).get("/archive", auth=("x", "")).status_code == 401
    )
    assert not archive.check_token("link", archive.make_token("link", 60))


def test_filter_form_with_empty_fields_works(monkeypatch):
    """A browser submits every field, empty ones as '' (this broke the Global filter on 2026-11-19)."""
    c = client(monkeypatch)
    for params in (
        {
            "q": "",
            "market": "global",
            "country": "",
            "sector": "",
            "min": "",
            "status": "",
        },
        {
            "q": "",
            "market": "bangladesh",
            "country": "",
            "sector": "",
            "min": "7",
            "status": "",
        },
        {"min": "abc"},
    ):
        r = c.get("/archive", params=params, auth=("x", "s3cret"))
        assert r.status_code == 200 and "Every card" in r.text


def test_decimal_threshold_is_passed_to_sql(monkeypatch):
    seen = []

    class CaptureConn(Conn):
        def execute(self, sql, args=None):
            if "where" in sql:
                seen.append(args)
            return super().execute(sql, args)

    monkeypatch.setenv("ARCHIVE_PASSWORD", "test-secret")
    app = FastAPI()
    archive.add_routes(app, CaptureConn)
    response = TestClient(app).get(
        "/archive", params={"min": "6.5"}, auth=("owner", "test-secret")
    )
    assert response.status_code == 200 and any(6.5 in (args or []) for args in seen)
