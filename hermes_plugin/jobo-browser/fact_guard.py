"""No made-up postal codes or street numbers typed by hand (GHO-101).

NEED_FIELD already refuses them (module4/agent_driver.py). This is the same
check for browser_type, so a field the agent types into directly is held to
the same rule: the field's accessible name comes from the latest snapshot
(account_guard._REF_NAMES), the rule from policy/made_up_values.py.

Fails open on anything unexpected; a matching rule fails closed.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]


def _load_rules():
    spec = importlib.util.spec_from_file_location(
        "jobo_browser_made_up_values", _REPO / "policy" / "made_up_values.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rules = _load_rules()


def _personal_dir() -> Path:
    """Same lookup as policy/personal.py: JOBO_PERSONAL_DIR, else personal/, else the example."""
    raw = os.environ.get("JOBO_PERSONAL_DIR", "").strip()
    if raw:
        pdir = Path(raw).expanduser()
        return pdir if pdir.is_absolute() else _REPO / pdir
    return _REPO / "personal" if (_REPO / "personal").is_dir() else _REPO / "personal.example"


def facts_text() -> str:
    """The text the driver checks: candidate_facts.md, then candidate.json."""
    pdir = _personal_dir()
    try:
        facts = (pdir / "candidate_facts.md").read_text(encoding="utf-8").strip("\n")
    except OSError:
        facts = ""
    try:
        data = json.loads((pdir / "candidate.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return facts + "\n" + json.dumps(data if isinstance(data, dict) else {})


def check_tool_call(tool_name: str, args: dict | None, ref_name) -> str | None:
    """A block message for browser_type of a made-up value, else None."""
    if tool_name != "browser_type" or not isinstance(args, dict):
        return None
    name = ref_name(args.get("ref"))
    if not name:
        return None
    # "combobox Pin Code" -> "Pin Code": the role word is not part of the label.
    label = name.split(" ", 1)[1] if " " in name else name
    text = str(args.get("text") or "")
    kind = rules.refusal(label, text, facts_text())
    if not kind:
        return None
    return "BLOCKED by jobo: " + rules.agent_message(kind, label, text)
