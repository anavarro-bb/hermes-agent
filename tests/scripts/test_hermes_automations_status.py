from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "hermes_automations_status.py"
SPEC = importlib.util.spec_from_file_location("hermes_automations_status", MODULE_PATH)
assert SPEC and SPEC.loader
status = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(status)


def _write_jobs(path: Path, jobs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")


def _daily_job(**overrides):
    base = {
        "id": "2eae0a2e25f7",
        "name": "hermes-daily-upgrade-radar",
        "schedule": {"kind": "cron", "expr": "30 20 * * *", "display": "30 20 * * *"},
        "enabled": True,
        "script": "hermes-daily-upgrade-radar.sh",
        "no_agent": True,
        "last_run_at": "2026-06-20T20:30:05+02:00",
        "last_status": "ok",
    }
    base.update(overrides)
    return base


def test_load_cron_jobs_scans_default_and_profile_registries(tmp_path: Path) -> None:
    root = tmp_path / "hermes"
    _write_jobs(
        root / "cron" / "jobs.json",
        [_daily_job(last_run_at="2026-06-17T20:44:52+02:00")],
    )
    _write_jobs(
        root / "profiles" / "agentloop" / "cron" / "jobs.json",
        [
            _daily_job(
                last_status="error",
                last_error=f"Script not found: {root}/profiles/agentloop/scripts/hermes-daily-upgrade-radar.sh",
                last_delivery_error="delivery error: Telegram send failed: Unauthorized",
            )
        ],
    )

    jobs, errors = status.load_cron_jobs(root)

    assert errors == []
    assert {job["profile"] for job in jobs} == {"default", "agentloop"}
    by_profile = {job["profile"]: job for job in jobs}
    assert by_profile["default"]["status"] == "STALE"
    assert by_profile["agentloop"]["status"] == "FAILED"
    assert by_profile["agentloop"]["last_delivery_error"] == "delivery error: Telegram send failed: Unauthorized"
    assert by_profile["agentloop"]["script"].endswith("/profiles/agentloop/scripts/hermes-daily-upgrade-radar.sh")


def test_classify_job_reports_delivery_failure_before_staleness(tmp_path: Path) -> None:
    registry = tmp_path / "hermes" / "cron" / "jobs.json"
    job = _daily_job(
        last_run_at=(dt.datetime.now().astimezone() - dt.timedelta(minutes=10)).isoformat(),
        last_delivery_error="delivery error: Telegram send failed: Unauthorized",
    )

    classified = status.classify_job(job, profile="default", registry_path=registry)

    assert classified["status"] == "DELIVERY_FAILED"
    assert classified["reason"] == "delivery error: Telegram send failed: Unauthorized"


def test_build_payload_marks_red_when_profile_job_failed(tmp_path: Path) -> None:
    root = tmp_path / "hermes"
    _write_jobs(root / "profiles" / "agentloop" / "cron" / "jobs.json", [_daily_job(last_status="error")])

    payload = status.build_payload(root, include_launchagents=False)

    assert payload["summary"]["registries_total"] == 1
    assert payload["summary"]["cron_total"] == 1
    assert payload["summary"]["red"] == 1
    assert payload["summary"]["all_green"] is False
