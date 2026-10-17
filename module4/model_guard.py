"""Model guard: the jobo Hermes profile must resolve to a Sonnet build.

16,193 apply-stage calls have gone to claude-sonnet-5[1m] and zero to Opus
(scripts/setup_hermes_profile.sh pins model.default and empties
fallback_providers after the 2026-09-26 incident where Hermes silently fell
back to an expired free model and 101 jobs failed in a row). Nothing in the
repo asserts this stays true at runtime - a `hermes update` or a profile
re-clone could move `model.default` or repopulate `fallback_providers`
without anything noticing. This module reads the real on-disk profile
config and says so if it ever drifts; scripts/test_model_guard.py wires it
into the regression suite.

Deliberately not a full YAML parser (no PyYAML dependency in this repo): the
profile config is machine-written by Hermes itself, so its two keys of
interest (`model.default`, top-level `fallback_providers`) are always emitted
in the plain `key: value` / `key: []` forms handled here.
"""

from __future__ import annotations

import os
import pwd
import re
from pathlib import Path

PROFILE_CONFIG_RELPATH = ".hermes/profiles/jobo/config.yaml"
REQUIRED_MODEL_PREFIX = "claude-sonnet-5"


def _passwd_home() -> Path:
    # Not Path.home()/$HOME: this process may itself be running with HOME
    # already redirected (e.g. inside an agent sandbox), which would just
    # repeat the redirection instead of finding the real jobo/hermes home.
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def profile_config_path(host_home: Path | None = None) -> Path:
    base = host_home or _passwd_home()
    return base / PROFILE_CONFIG_RELPATH


def _strip_comment(value: str) -> str:
    # YAML scalars here are never quoted strings containing '#', so a plain
    # split is safe for this machine-written config.
    return value.split("#", 1)[0].strip()


def parse_model_default(text: str) -> str | None:
    """`model:\n  default: <value>\n  ...` -> <value>, or None if absent."""
    lines = text.splitlines()
    in_model_block = False
    for line in lines:
        if not in_model_block:
            if line.strip() == "model:" and not line.startswith((" ", "\t")):
                in_model_block = True
            continue
        if line.strip() and not line.startswith((" ", "\t")):
            break  # next top-level key; model: block ended
        stripped = line.strip()
        if stripped.startswith("default:"):
            return _strip_comment(stripped[len("default:"):]).strip("'\"")
    return None


def parse_fallback_providers(text: str) -> list[str]:
    """Top-level `fallback_providers: [...]` or a `- item` block -> list."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("fallback_providers:"):
            continue
        rest = _strip_comment(line[len("fallback_providers:"):])
        if rest in ("", "~", "null"):
            items = []
            for follow in lines[i + 1:]:
                if follow.strip().startswith("- "):
                    items.append(follow.strip()[2:].strip())
                elif follow.strip() == "":
                    continue
                else:
                    break
            return items
        inline = re.fullmatch(r"\[(.*)\]", rest)
        if inline:
            body = inline.group(1).strip()
            return [p.strip().strip("'\"") for p in body.split(",") if p.strip()]
        return [rest.strip("'\"")]
    return []


def check_profile(config_path: Path) -> list[str]:
    """Return violations; an empty list means the profile is healthy."""
    if not config_path.is_file():
        return [f"hermes profile config not found at {config_path}"]
    text = config_path.read_text()
    violations: list[str] = []
    model = parse_model_default(text)
    if not model:
        violations.append("model.default is missing from the profile config")
    elif not model.startswith(REQUIRED_MODEL_PREFIX):
        violations.append(
            f"model.default resolved to {model!r}, expected a {REQUIRED_MODEL_PREFIX}* build"
        )
    fallbacks = parse_fallback_providers(text)
    if fallbacks:
        violations.append(
            f"fallback_providers is non-empty ({fallbacks!r}); a provider outage could "
            "silently fall back to a non-Sonnet model"
        )
    return violations
