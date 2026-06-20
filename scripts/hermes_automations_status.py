#!/usr/bin/env python3
"""Profile-aware Hermes cron automation status.

This is a read-only support utility for operators and external control planes.
It reports every cron registry under a Hermes root:

* ``<root>/cron/jobs.json`` for the default/root profile
* ``<root>/profiles/<name>/cron/jobs.json`` for named profiles

The output includes a human summary plus a JSON payload between
``<HERMES_STATUS_JSON>`` markers so monitors can parse it without scraping
the prose.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import subprocess
from pathlib import Path
from typing import Any

STALE_FACTOR = 2.5
DAILY_GRACE_MIN = 6 * 60
LA_GLOBS = ("local.hermes.*.plist", "ai.hermes.*.plist", "com.alex.*.plist")


def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


def parse_ts(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def expected_cadence_min(schedule: Any) -> int | None:
    """Best-effort expected cadence in minutes from a Hermes schedule dict."""
    if not isinstance(schedule, dict):
        return None
    if schedule.get("kind") == "interval":
        minutes = schedule.get("minutes")
        if minutes:
            return int(minutes)
        hours = schedule.get("hours")
        return int(hours) * 60 if hours else None
    if schedule.get("kind") != "cron":
        return None

    parts = str(schedule.get("expr", "")).split()
    if len(parts) != 5:
        return None
    _minute, hour, _dom, _mon, dow = parts
    if dow not in ("*", "?"):
        if "-" in dow or "," in dow:
            return 24 * 60
        return 7 * 24 * 60
    if "," in hour:
        return max(1, (24 * 60) // max(1, len(hour.split(","))))
    return 24 * 60


def _status_from_job(job: dict[str, Any], age_min: float | None, cadence: int | None) -> tuple[str, str]:
    enabled = job.get("enabled", job.get("active", True))
    if not enabled:
        return "DISABLED", ""

    last_status = str(job.get("last_status") or job.get("lastStatus") or "").lower()
    last_error = str(job.get("last_error") or job.get("lastError") or "").strip()
    last_delivery_error = str(job.get("last_delivery_error") or job.get("lastDeliveryError") or "").strip()
    if last_status in {"error", "failed", "failure"} or last_error:
        return "FAILED", last_error or f"last_status={last_status}"
    if last_delivery_error:
        return "DELIVERY_FAILED", last_delivery_error

    if age_min is None:
        return "NEVER_RAN", "no last_run recorded"
    if cadence:
        if cadence >= 24 * 60:
            threshold = max(cadence * STALE_FACTOR, cadence + DAILY_GRACE_MIN)
        else:
            threshold = cadence * STALE_FACTOR
        if age_min > threshold:
            return "STALE", f"last ran {age_min / 60:.1f}h ago, expected ~every {cadence / 60:.1f}h"

    return "OK", ""


def _resolve_script_path(job: dict[str, Any], registry_path: Path) -> str | None:
    script = str(job.get("script") or "").strip()
    if not script:
        return None
    raw = Path(script).expanduser()
    if raw.is_absolute():
        return str(raw)
    profile_home = registry_path.parent.parent
    return str((profile_home / "scripts" / raw).resolve())


def classify_job(
    job: dict[str, Any],
    *,
    profile: str,
    registry_path: Path,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or now_local()
    schedule = job.get("schedule") or job.get("cron") or {}
    display = schedule.get("display") if isinstance(schedule, dict) else str(schedule)
    last = parse_ts(job.get("last_run") or job.get("lastRun") or job.get("last_run_at"))
    cadence = expected_cadence_min(schedule)
    age_min = (now - last).total_seconds() / 60.0 if last else None
    status, reason = _status_from_job(job, age_min, cadence)
    script_path = _resolve_script_path(job, registry_path)

    return {
        "profile": profile,
        "registry": str(registry_path),
        "id": job.get("id"),
        "name": job.get("name", "?"),
        "schedule": display,
        "enabled": bool(job.get("enabled", job.get("active", True))),
        "last_run": last.isoformat() if last else None,
        "age_hours": round(age_min / 60, 1) if age_min is not None else None,
        "expected_cadence_min": cadence,
        "last_status": job.get("last_status") or job.get("lastStatus"),
        "last_error": job.get("last_error") or job.get("lastError"),
        "last_delivery_error": job.get("last_delivery_error") or job.get("lastDeliveryError"),
        "script": script_path,
        "status": status,
        "reason": reason,
    }


def iter_registry_paths(root: Path) -> list[tuple[str, Path]]:
    paths: list[tuple[str, Path]] = []
    root_registry = root / "cron" / "jobs.json"
    if root_registry.exists():
        paths.append(("default", root_registry))
    profiles_dir = root / "profiles"
    for path in sorted(profiles_dir.glob("*/cron/jobs.json")) if profiles_dir.exists() else []:
        paths.append((path.parts[-3], path))
    return paths


def _jobs_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        jobs = payload
    elif isinstance(payload, dict):
        jobs = payload.get("jobs")
        if jobs is None:
            jobs = list(payload.values())
    else:
        jobs = []
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    return [job for job in jobs if isinstance(job, dict)]


def load_cron_jobs(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    jobs: list[dict[str, Any]] = []
    errors: list[str] = []
    now = now_local()
    for profile, registry_path in iter_registry_paths(root):
        try:
            payload = json.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{registry_path}: {exc}")
            continue
        for job in _jobs_from_payload(payload):
            jobs.append(classify_job(job, profile=profile, registry_path=registry_path, now=now))
    return jobs, errors


def launchctl_loaded(label: str) -> bool | None:
    uid = os.getuid()
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{label}"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if result.returncode == 0:
            return True
        legacy = subprocess.run(["launchctl", "list", label], capture_output=True, text=True, timeout=8, check=False)
        if legacy.returncode == 0:
            return True
    except (OSError, subprocess.TimeoutExpired):
        return None
    return None


def load_launchagents(home: Path) -> list[dict[str, Any]]:
    launch_agents_dir = home / "Library" / "LaunchAgents"
    agents: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pattern in LA_GLOBS:
        for raw_path in sorted(glob.glob(str(launch_agents_dir / pattern))):
            path = Path(raw_path)
            label = path.name.removesuffix(".plist")
            if label in seen:
                continue
            seen.add(label)
            agents.append({"label": label, "loaded": launchctl_loaded(label), "plist": str(path)})
    return agents


def build_payload(root: Path, *, include_launchagents: bool = True) -> dict[str, Any]:
    cron_jobs, errors = load_cron_jobs(root)
    launchagents = load_launchagents(Path.home()) if include_launchagents else []
    red_statuses = {"STALE", "NEVER_RAN", "FAILED", "DELIVERY_FAILED"}
    red = [job for job in cron_jobs if job["status"] in red_statuses]
    disabled = [job for job in cron_jobs if job["status"] == "DISABLED"]
    generated = now_local().isoformat()
    return {
        "generated": generated,
        "root": str(root),
        "summary": {
            "registries_total": len(iter_registry_paths(root)),
            "cron_total": len(cron_jobs),
            "launchagents_total": len(launchagents),
            "red": len(red),
            "disabled": len(disabled),
            "all_green": not red and not errors,
        },
        "red_jobs": red,
        "cron_jobs": cron_jobs,
        "launchagents": launchagents,
        "errors": errors,
    }


def print_report(payload: dict[str, Any]) -> None:
    print("=== HERMES AUTOMATIONS STATUS ===")
    print(f"generated: {payload['generated']}")
    print(f"root: {payload['root']}")
    if payload["errors"]:
        for error in payload["errors"]:
            print(f"!! {error}")
    summary = payload["summary"]
    print(
        "cron jobs: {cron_total} across {registries_total} registries | "
        "LaunchAgents: {launchagents_total} | RED: {red} | disabled: {disabled}".format(**summary)
    )
    print()

    if payload["red_jobs"]:
        print("RED - needs attention:")
        for job in payload["red_jobs"]:
            reason = job["reason"] or job["status"]
            print(f"  [{job['profile']}] {job['name']:<34} {job['status']:<16} {reason}")
        print()
    else:
        print("GREEN - all cron jobs within expected cadence.")
        print()

    print("All cron jobs:")
    for job in sorted(payload["cron_jobs"], key=lambda item: (item["status"] != "OK", item["profile"], item["name"])):
        age = f"{job['age_hours']}h ago" if job["age_hours"] is not None else "never"
        print(f"  [{job['status']:<15}] [{job['profile']:<12}] {job['name']:<34} {job['schedule']:<16} last={age}")
    print()

    if payload["launchagents"]:
        print("LaunchAgents (substrate):")
        for agent in payload["launchagents"]:
            loaded = {True: "loaded", False: "DOWN", None: "?"}[agent["loaded"]]
            print(f"  [{loaded:<6}] {agent['label']}")
        print()

    print("<HERMES_STATUS_JSON>")
    print(json.dumps(payload, indent=2))
    print("</HERMES_STATUS_JSON>")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=os.getenv("HERMES_AUTOMATIONS_HOME") or str(Path.home() / ".hermes"),
        help="Hermes root containing cron/ and profiles/ (default: ~/.hermes)",
    )
    parser.add_argument("--no-launchagents", action="store_true", help="skip LaunchAgent discovery")
    args = parser.parse_args(argv)
    payload = build_payload(Path(args.root).expanduser(), include_launchagents=not args.no_launchagents)
    print_report(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
