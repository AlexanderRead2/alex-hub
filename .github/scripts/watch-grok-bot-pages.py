#!/usr/bin/env python3
"""Fetch official Grok Bot pages, hash stable visible text, and notify on real changes.

The first run (no previous hashes) only writes a baseline and does not call the webhook.
Intended to run from .github/workflows/grok-bot-watch.yml.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from difflib import unified_diff
from html.parser import HTMLParser
from pathlib import Path

PAGES = [
    "https://x.ai/changelog/bot",
    "https://x.ai/pricing",
    "https://docs.x.ai/grok-bot",
    "https://x.ai/bot",
    "https://cursor.com/download/bot",
]

USER_AGENT = (
    "Mozilla/5.0 (compatible; AlexHubPageWatcher/1.0; "
    "+https://github.com/AlexanderRead2/alex-hub) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MAX_DIFF_CHARS = 400
FETCH_RETRIES = 3
FETCH_TIMEOUT = 30

ISO_TS = re.compile(
    r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
UNIX_TS = re.compile(r"\b1[6-9]\d{8,12}\b")
BUILD_HASH = re.compile(r"\b[a-fA-F0-9]{10,64}\b")
DPL_ID = re.compile(r"\bdpl_[A-Za-z0-9]+\b")
NONCE = re.compile(r"\bnonce-[A-Za-z0-9+/=_-]+\b")


class VisibleTextExtractor(HTMLParser):
    SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP_TAGS:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        return " ".join(self._parts)


def visible_text(raw_html: str) -> str:
    parser = VisibleTextExtractor()
    parser.feed(raw_html)
    parser.close()
    text = html.unescape(parser.text())
    text = re.sub(r"\s+", " ", text).strip()
    text = ISO_TS.sub(" ", text)
    text = UNIX_TS.sub(" ", text)
    text = DPL_ID.sub(" ", text)
    text = NONCE.sub(" ", text)
    text = BUILD_HASH.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch(url: str) -> tuple[int, str]:
    last_error: Exception | None = None
    for attempt in range(1, FETCH_RETRIES + 1):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-GB,en;q=0.9",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, raw
        except urllib.error.HTTPError as exc:
            last_error = exc
            body = exc.read().decode("utf-8", "replace") if exc.fp else ""
            print(
                f"fetch {url} attempt {attempt}/{FETCH_RETRIES}: HTTP {exc.code}",
                file=sys.stderr,
            )
            if exc.code in {429, 403, 500, 502, 503, 504} and attempt < FETCH_RETRIES:
                time.sleep(attempt * 2)
                continue
            raise RuntimeError(f"Failed to fetch {url}: HTTP {exc.code}") from exc
        except Exception as exc:  # noqa: BLE001 — retry network errors
            last_error = exc
            print(
                f"fetch {url} attempt {attempt}/{FETCH_RETRIES}: {type(exc).__name__}",
                file=sys.stderr,
            )
            if attempt < FETCH_RETRIES:
                time.sleep(attempt * 2)
                continue
            raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def wrap_text(text: str, width: int = 80) -> list[str]:
    words = text.split()
    lines_out: list[str] = []
    buf: list[str] = []
    size = 0
    for word in words:
        add = len(word) + (1 if buf else 0)
        if buf and size + add > width:
            lines_out.append(" ".join(buf))
            buf = [word]
            size = len(word)
        else:
            buf.append(word)
            size += add
    if buf:
        lines_out.append(" ".join(buf))
    return lines_out or [""]


def short_diff(old: str, new: str) -> str:
    if not old:
        snippet = new[:MAX_DIFF_CHARS]
        return snippet + ("…" if len(new) > MAX_DIFF_CHARS else "")
    rendered = list(
        unified_diff(
            wrap_text(old),
            wrap_text(new),
            fromfile="previous",
            tofile="current",
            lineterm="",
            n=2,
        )
    )
    text = "\n".join(rendered)
    if not text.strip():
        text = new[:MAX_DIFF_CHARS]
    if len(text) > MAX_DIFF_CHARS:
        text = text[: MAX_DIFF_CHARS - 1].rstrip() + "…"
    return text


def load_state(path: Path) -> dict:
    if not path.is_file():
        return {"version": 1, "pages": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "pages": {}}
    if not isinstance(data, dict):
        return {"version": 1, "pages": {}}
    pages = data.get("pages")
    if not isinstance(pages, dict):
        data["pages"] = {}
    return data


def post_webhook(payload: dict) -> None:
    url = os.environ.get("WEBHOOK_URL", "").strip()
    secret = os.environ.get("ROUTINE_WEBHOOK_SECRET", "")
    if not url:
        raise RuntimeError("WEBHOOK_URL is not set")
    if not secret:
        raise RuntimeError("ROUTINE_WEBHOOK_SECRET is not set")
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch Grok Bot pages for real text changes")
    parser.add_argument("--prev", required=True, help="Path to previous watch-state.json")
    parser.add_argument("--out", required=True, help="Path to write new watch-state.json")
    parser.add_argument(
        "--changed-flag",
        required=True,
        help="Touched when the state file should be committed",
    )
    args = parser.parse_args()

    prev = load_state(Path(args.prev))
    prev_pages: dict = prev.get("pages") or {}
    checked_at = now_iso()
    new_pages: dict = {}
    changed: list[dict] = []
    new_baselines: list[str] = []
    errors: list[str] = []

    for url in PAGES:
        try:
            status, raw = fetch(url)
            text = visible_text(raw)
            digest = sha256_text(text)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url}: {exc}")
            print(f"ERROR {url}: {exc}", file=sys.stderr)
            continue

        entry = {
            "sha256": digest,
            "fetched_at": checked_at,
            "http_status": status,
            "text": text,
        }
        new_pages[url] = entry
        old = prev_pages.get(url) if isinstance(prev_pages.get(url), dict) else None
        old_hash = old.get("sha256") if old else None
        if not old_hash:
            new_baselines.append(url)
            print(f"BASELINE {digest[:16]} {url}")
            continue
        if old_hash == digest:
            print(f"UNCHANGED {digest[:16]} {url}")
            continue
        snippet = short_diff(str(old.get("text") or ""), text)
        changed.append({"url": url, "diff": snippet})
        print(f"CHANGED {old_hash[:16]} -> {digest[:16]} {url}")

    if errors and not new_pages:
        write_github_summary(["## Grok Bot page watcher", "", "All page fetches failed."] + [f"- {e}" for e in errors])
        return 1

    # If any fetch failed, keep previous snapshots for those URLs so we do not
    # drop history, and skip the webhook so a partial view cannot look like a change.
    if errors:
        for url in PAGES:
            if url not in new_pages and isinstance(prev_pages.get(url), dict):
                new_pages[url] = prev_pages[url]
        Path(args.out).write_text(
            json.dumps({"version": 1, "updated_at": checked_at, "pages": new_pages}, ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        write_github_summary(
            ["## Grok Bot page watcher", "", "One or more fetches failed; state was not committed and the webhook was not called."]
            + [f"- {e}" for e in errors]
        )
        return 1

    state = {"version": 1, "updated_at": checked_at, "pages": new_pages}
    Path(args.out).write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    is_first_run = not any(
        isinstance(prev_pages.get(url), dict) and prev_pages[url].get("sha256") for url in PAGES
    )

    summary = [
        "## Grok Bot page watcher",
        "",
        f"Checked at `{checked_at}`.",
        f"Pages unchanged: {len(PAGES) - len(changed) - len(new_baselines)}.",
        f"New baselines: {len(new_baselines)}.",
        f"Changed: {len(changed)}.",
    ]
    if is_first_run or (new_baselines and not changed):
        summary += ["", "First run or new pages only: recorded baseline hashes and did not call the webhook."]
        Path(args.changed_flag).write_text("baseline\n", encoding="utf-8")
        write_github_summary(summary)
        print("Recorded baseline; webhook not called.")
        return 0

    if not changed:
        # Hashes match; no need to rewrite the state branch.
        write_github_summary(summary + ["", "No text changes."])
        print("No text changes.")
        return 0

    payload = {
        "source": "grok-bot-page-watcher",
        "checked_at": checked_at,
        "changed_pages": changed,
    }
    post_webhook(payload)
    Path(args.changed_flag).write_text("changed\n", encoding="utf-8")
    summary += ["", "Webhook notified for:"] + [f"- {item['url']}" for item in changed]
    write_github_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
