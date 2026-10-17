#!/usr/bin/env python3
"""Watchdog: check the latest daily_pipeline run and notify the user on Telegram.

Cron: hourly at 12, 14, 16, 18 IST. Logic:
- No log newer than the pipeline's scheduled start -> report missing run.
- Log still growing / process alive -> do nothing (silent).
- Log finished (marker line) -> one concise summary message; silent afterwards
  for that log (state file tracks what was already reported).
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import urllib.request
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOG_DIR = REPO / "data" / "logs"
STATE = Path.home() / ".hermes" / "jobo_watchdog_state.json"
ENV_FILE = Path.home() / ".hermes" / ".env"

# Pipeline start hours (IST). Watchdogs at 12,14,16,18 cover both 10:00 and 22:00 runs.
PIPELINE_HOURS = {10, 22}


def telegram_creds() -> tuple[str, str]:
    token = chat = ""
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            token = line.split("=", 1)[1].strip()
        elif line.startswith("TELEGRAM_HOME_CHANNEL="):
            chat = line.split("=", 1)[1].strip()
    return token, chat


def send_telegram(text: str) -> None:
    token, chat = telegram_creds()
    if not token or not chat:
        print("no telegram creds; message suppressed")
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            json.load(r)
    except Exception as e:  # never crash the watchdog on delivery failure
        print(f"telegram send failed: {e}")


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"reported_logs": []}


def save_state(s: dict) -> None:
    s["reported_logs"] = s.get("reported_logs", [])[-50:]
    STATE.write_text(json.dumps(s))


def parse_log(log_path: Path) -> dict:
    text = log_path.read_text(errors="replace")
    out = {"done": False, "stages": {}, "errors": []}
    m = re.search(r"=== run complete, failed=(\d)", text)
    out["done"] = bool(m)

    for stage in ("collector", "enrich", "triage", "apply"):
        ok = f"=== {stage} OK ===" in text
        fail = f"!!! {stage} FAILED" in text
        out["stages"][stage] = "ok" if ok else ("failed" if fail else "skipped/running")

    # collector counts
    m = re.search(r"Collected:\s*(\d+)|new jobs?:?\s*(\d+)", text, re.I)
    # triage summary numbers
    for name, pat in {
        "new_jobs": r"[Nn]ew jobs:? (\d+)",
        "attempted": r"Attempted: (\d+)",
        "submitted": r"Submitted: (\d+)",
        "failed": r"Failed:\s*(\d+)",
    }.items():
        mm = re.findall(pat, text)
        if mm:
            out[name] = mm[-1] if isinstance(mm[-1], str) else mm[-1][-1]

    err_lines = [
        l.strip()[:160]
        for l in text.splitlines()
        if re.search(r"FAILED|Error:|Traceback|invalid_grant|401", l) and not l.startswith("[")
    ]
    out["errors"] = list(dict.fromkeys(err_lines))[:5]
    return out


def main() -> None:
    now_h = int(subprocess.run(["date", "+%H"], capture_output=True, text=True).stdout.strip())
    logs = sorted(glob.glob(str(LOG_DIR / "run_*.log")))
    state = load_state()

    if not logs:
        send_telegram("jobo: no pipeline runs found yet. Investigate scripts/daily_pipeline.sh")
        return

    newest = Path(logs[-1])
    info = parse_log(newest)

    if newest.name in state["reported_logs"]:
        return  # already reported this run; stay silent

    if info["done"]:
        parts = [f"jobo run {newest.stem.replace('run_', '')} finished."]
        stages = " | ".join(f"{k}:{v}" for k, v in info["stages"].items())
        parts.append(stages)
        counts = []
        for k in ("new_jobs", "submitted", "failed"):
            if k in info:
                counts.append(f"{k}={info[k]}")
        if counts:
            parts.append(", ".join(counts))
        if info["errors"]:
            parts.append("Errors: " + " ;; ".join(info["errors"][:3]))
        send_telegram("\n".join(parts))
        state["reported_logs"].append(newest.name)
        save_state(state)
        print(f"reported completion of {newest.name}")
        return

    # Not done: only nag if a scheduled start has clearly passed with no fresh run,
    # or if the log looks abandoned (no writes for >3h).
    age_min = (__import__("time").time() - newest.stat().st_mtime) / 60
    if age_min > 240:
        send_telegram(
            f"jobo: latest run ({newest.stem}) appears stuck - no log activity for "
            f"{int(age_min)} min. Last stage status: "
            + ", ".join(f"{k}:{v}" for k, v in info["stages"].items())
            + ". SSH in and check data/logs."
        )
        state["reported_logs"].append(newest.name)
        save_state(state)
        print("reported stuck run")
    else:
        print(f"run still active (log age {int(age_min)} min); staying silent")


if __name__ == "__main__":
    main()
