#!/usr/bin/env python3
"""Poll Launch Library 2 for upcoming Starship launches and notify on changes.

The first run (no previous state) only writes a baseline and does not call the
webhook. Intended to run from .github/workflows/tek-starship-watch.yml.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

USER_AGENT = (
    "AlexHubTekStarshipWatch/1.0 "
    "(+https://github.com/AlexanderRead2/alex-hub)"
)
FETCH_TIMEOUT = 30
GO_SOON_WINDOW = timedelta(hours=3)
TRACKED_FIELDS = (
    "id",
    "name",
    "net",
    "window_start",
    "window_end",
    "status_abbrev",
    "status_name",
)
API_VERSIONS = ("2.3.0", "2.2.0")
UPCOMING_PATH = "/launches/upcoming/?search=Starship&limit=10"
PREVIOUS_PATH = "/launches/previous/?search=Starship&limit=3"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")


def api_url(version: str, path: str) -> str:
    return f"https://ll.thespacedevs.com/{version}{path}"


def fetch_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        status = resp.status
        raw = resp.read()
    if status < 200 or status >= 300:
        raise RuntimeError(f"HTTP {status}")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("unexpected JSON payload")
    return data


def fetch_upcoming() -> tuple[str, list[dict[str, Any]]] | None:
    """Return (api_version, results) or None if every attempt failed."""
    for version in API_VERSIONS:
        url = api_url(version, UPCOMING_PATH)
        try:
            data = fetch_json(url)
            results = data.get("results")
            if not isinstance(results, list):
                raise RuntimeError("missing results list")
            print(f"Fetched upcoming launches from Launch Library {version}.")
            return version, results
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            # Do not dump response bodies; keep this quiet and try the fallback.
            print(f"Launch Library {version} upcoming request failed: {type(exc).__name__}", file=sys.stderr)
    return None


def fetch_previous(version: str) -> list[dict[str, Any]]:
    try:
        data = fetch_json(api_url(version, PREVIOUS_PATH))
        results = data.get("results")
        return results if isinstance(results, list) else []
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, RuntimeError):
        print(f"Launch Library {version} previous request failed; recording removal without final status.", file=sys.stderr)
        return []


def is_starship(launch: dict[str, Any]) -> bool:
    name = str(launch.get("name") or "").lower()
    cfg = ((launch.get("rocket") or {}).get("configuration") or {})
    if not isinstance(cfg, dict):
        cfg = {}
    rocket_text = " ".join(str(cfg.get(key) or "") for key in ("name", "full_name", "family")).lower()
    return "starship" in rocket_text or "starship" in name


def extract(launch: dict[str, Any]) -> dict[str, Any]:
    status = launch.get("status") or {}
    if not isinstance(status, dict):
        status = {}
    return {
        "id": launch.get("id"),
        "name": launch.get("name"),
        "net": launch.get("net"),
        "window_start": launch.get("window_start"),
        "window_end": launch.get("window_end"),
        "status_abbrev": status.get("abbrev"),
        "status_name": status.get("name"),
    }


def snapshot_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(left.get(field) == right.get(field) for field in TRACKED_FIELDS)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "launches": [], "go_soon_sent": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "launches": [], "go_soon_sent": []}
    if not isinstance(data, dict):
        return {"version": 1, "launches": [], "go_soon_sent": []}
    launches = data.get("launches")
    if not isinstance(launches, list):
        data["launches"] = []
    go_soon = data.get("go_soon_sent")
    if not isinstance(go_soon, list):
        data["go_soon_sent"] = []
    return data


def parse_net(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def go_soon_key(launch: dict[str, Any]) -> str:
    return f"{launch.get('id')}|{launch.get('net')}"


def is_go_soon(launch: dict[str, Any], now: datetime) -> bool:
    abbrev = str(launch.get("status_abbrev") or "").strip().lower()
    if abbrev != "go":
        return False
    net = parse_net(launch.get("net"))
    if net is None:
        return False
    delta = net - now
    return timedelta(0) < delta <= GO_SOON_WINDOW


def post_webhook(payload: dict[str, Any]) -> None:
    url = os.environ.get("WEBHOOK_URL", "").strip()
    secret = os.environ.get("TEK_LAUNCH_WEBHOOK_SECRET", "")
    if not url:
        raise RuntimeError("WEBHOOK_URL is not set")
    if not secret:
        raise RuntimeError("TEK_LAUNCH_WEBHOOK_SECRET is not set")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain;q=0.9, */*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            resp.read()
    except urllib.error.HTTPError as exc:
        # Do not print request headers or the secret.
        print(f"webhook POST failed: HTTP {exc.code}", file=sys.stderr)
        raise RuntimeError(f"webhook POST failed: HTTP {exc.code}") from exc
    if status < 200 or status >= 300:
        raise RuntimeError(f"webhook POST failed: HTTP {status}")
    print(f"webhook POST succeeded: HTTP {status}")


def write_github_summary(lines: list[str]) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    with open(summary, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch upcoming Starship launches for schedule changes")
    parser.add_argument("--prev", required=True, help="Path to previous tek-starship.json")
    parser.add_argument("--out", required=True, help="Path to write new tek-starship.json")
    parser.add_argument(
        "--changed-flag",
        required=True,
        help="Touched when the state file should be committed",
    )
    args = parser.parse_args()

    fetched = fetch_upcoming()
    if fetched is None:
        write_github_summary(
            [
                "## Tek Starship watcher",
                "",
                "Launch Library request failed; state was not changed and the webhook was not called.",
            ]
        )
        print("Launch Library request failed; leaving state unchanged.")
        return 0

    api_version, raw_results = fetched
    current = [extract(item) for item in raw_results if isinstance(item, dict) and is_starship(item)]
    current_by_id = {item["id"]: item for item in current if item.get("id")}

    prev = load_state(Path(args.prev))
    prev_launches = [item for item in (prev.get("launches") or []) if isinstance(item, dict) and item.get("id")]
    prev_by_id = {item["id"]: item for item in prev_launches}
    prev_go_soon = [str(key) for key in (prev.get("go_soon_sent") or [])]
    checked_at = now_iso()
    now = now_utc()

    # First run: record a baseline only, even if a launch is already inside the reminder window.
    is_first_run = not prev_by_id
    go_soon_sent = [key for key in prev_go_soon if key in {go_soon_key(item) for item in current}]

    new_state = {
        "version": 1,
        "updated_at": checked_at,
        "api_version": api_version,
        "launches": current,
        "go_soon_sent": go_soon_sent,
    }

    summary = [
        "## Tek Starship watcher",
        "",
        f"Checked at `{checked_at}`.",
        f"Tracked Starship launches: {len(current)}.",
    ]

    if is_first_run:
        write_json(Path(args.out), new_state)
        Path(args.changed_flag).write_text("baseline\n", encoding="utf-8")
        summary += ["", "First run: recorded baseline and did not call the webhook."]
        write_github_summary(summary)
        print("Recorded baseline; webhook not called.")
        return 0

    changes: list[dict[str, Any]] = []

    for launch_id, item in current_by_id.items():
        old = prev_by_id.get(launch_id)
        if old is None:
            changes.append({"type": "new", "name": item.get("name"), "old": None, "new": item})
        elif not snapshot_equal(old, item):
            changes.append({"type": "updated", "name": item.get("name"), "old": old, "new": item})

    removed_ids = [launch_id for launch_id in prev_by_id if launch_id not in current_by_id]
    previous_results: list[dict[str, Any]] | None = None
    if removed_ids:
        previous_results = fetch_previous(api_version)
        previous_by_id = {
            extracted["id"]: extracted
            for item in previous_results
            if isinstance(item, dict) and is_starship(item)
            for extracted in [extract(item)]
            if extracted.get("id")
        }
        for launch_id in removed_ids:
            old = prev_by_id[launch_id]
            changes.append(
                {
                    "type": "removed",
                    "name": old.get("name"),
                    "old": old,
                    "new": previous_by_id.get(launch_id),
                }
            )

    for item in current:
        if is_go_soon(item, now) and go_soon_key(item) not in go_soon_sent:
            changes.append({"type": "go_soon", "name": item.get("name"), "old": item, "new": item})
            go_soon_sent.append(go_soon_key(item))

    new_state["go_soon_sent"] = go_soon_sent

    if not changes:
        write_github_summary(summary + ["", "No Starship launch changes."])
        print("No Starship launch changes.")
        return 0

    payload = {"source": "tek-starship-watch", "changes": changes}
    post_webhook(payload)
    write_json(Path(args.out), new_state)
    Path(args.changed_flag).write_text("changed\n", encoding="utf-8")
    summary += ["", "Webhook notified for:"] + [
        f"- {item.get('type')}: {item.get('name')}" for item in changes
    ]
    write_github_summary(summary)
    print("Posted " + ", ".join(f"{item.get('type')}:{item.get('name')}" for item in changes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
