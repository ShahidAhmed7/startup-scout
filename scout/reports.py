"""Friday and monthly reports. Filled in phases 3 and 5."""
from scout.db import repo


def friday(conn) -> None:
    rid = repo.start_run(conn, "friday")
    repo.finish_run(conn, rid, "ok", {"note": "not built yet"})


def monthly(conn) -> None:
    rid = repo.start_run(conn, "monthly")
    repo.finish_run(conn, rid, "ok", {"note": "not built yet"})
