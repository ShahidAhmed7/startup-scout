"""Resume this task after an observed usage-limit error. Stops when completion marker exists.
No credentials, no model changes, no paid API, no bypass of sandbox or approvals.
"""

import argparse
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "data/redesign"


def usage_limited_events(tail):
    for line in tail.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        payload = event.get("payload") or {}
        if event.get("type") not in ("event_msg", "error") or payload.get(
            "type"
        ) not in ("error", "turn_aborted", "warning"):
            continue
        message = json.dumps(payload).lower()
        if any(
            term in message
            for term in (
                "usage_limit_reached",
                "usage limit has been reached",
                "you've hit your usage limit",
                "you have hit your usage limit",
                "rate_limit_exceeded",
            )
        ):
            return True
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--thread", required=True)
    parser.add_argument("--rollout", required=True, type=Path)
    a = parser.parse_args()
    last_retry = 0
    if (STATE / "resume.log").exists():
        for line in (STATE / "resume.log").read_text().splitlines():
            try:
                last_retry = max(last_retry, json.loads(line).get("time", 0))
            except ValueError:
                pass
    while not (STATE / "COMPLETE").exists():
        try:
            # Inspect only the latest events; never print conversation contents.
            with a.rollout.open("rb") as f:
                f.seek(max(0, a.rollout.stat().st_size - 65536))
                tail = f.read().decode(errors="replace").lower()
            limited = usage_limited_events(tail)
            if limited and time.time() - last_retry >= 3600:
                result = subprocess.run(
                    [
                        "codex",
                        "queue",
                        "--thread",
                        a.thread,
                        "--resume",
                        "--message",
                        "Continue the authorised unattended REDESIGN.md implementation from IMPLEMENTATION.md. "
                        "Usage has had time to reset. Preserve existing work; use Flash-Lite for live tests; "
                        "manually review real results; finish deployment and push when verified. "
                        "Update the checkpoint. Create data/redesign/COMPLETE only after all work is verified.",
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                last_retry = time.time()
                with (STATE / "resume.log").open("a") as f:
                    f.write(
                        json.dumps(
                            {"time": last_retry, "queue_exit": result.returncode}
                        )
                        + "\n"
                    )
        except (OSError, subprocess.TimeoutExpired):
            pass
        time.sleep(60)


if __name__ == "__main__":
    main()
