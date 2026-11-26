from scout import budget


class Conn:
    def __init__(self, gpu_seconds):
        self.gpu = gpu_seconds


def test_spend_and_stop_threshold(monkeypatch):
    monkeypatch.setattr(budget.repo, "gpu_seconds_this_month", lambda conn: conn.gpu)
    # $1 CPU allowance + 0.000694/s
    assert budget.month_spend(Conn(0)) == 1.0
    assert not budget.gpu_blocked(Conn(35000))          # ~$25.3 (the old $25 stop, raised 2026-11-26)
    assert not budget.gpu_blocked(Conn(40000))          # ~$28.8
    assert budget.gpu_blocked(Conn(42000))              # ~$30.1 -> stop
