#!/usr/bin/env python
"""A portal whose account keeps a password an earlier run set (GHO-62).

What happened: Wingtip job 929 (2026-09-18, before the one-password rule)
reset Wingtip's Avature account to a variant of the standard password and
submitted. Every later Wingtip job (818, 1318, 1481, 1594, 1708) was told to
sign in with the standard password, which the portal refused ("The username
or password may be incorrect"), then to reset to it, which the portal also
refused ("Be different from your previous 3 passwords"). Five jobs stuck.

secrets.json `site_passwords` records that host's password. Covers:
  [1] policy.personal lookup: host, subdomain and parent-domain matches only
  [2] plugin guard: the recorded password passes and is what a password field
      gets on that host; elsewhere the standard rule is unchanged
  [3] driver: NEED_FIELD password gets the recorded one while a tab is on the
      host; NEED_PASSWORD_RESET for that host is answered with "sign in"
      and never reads the mailbox
  [4] prompt names the recorded password for that host only
  [5] audit redacts the recorded password

Offline. Run via
    bash scripts/run_regression_tests.sh test_site_passwords
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

HOST = "careers.wingtip.example"
KNOWN = "Wingtip@Known42"

# Fictional candidate dir: the example person plus one recorded site password.
_TMP = Path(tempfile.mkdtemp(prefix="jobo_site_pw_"))
shutil.copytree(REPO / "personal.example", _TMP / "personal")
_secrets = json.loads((_TMP / "personal" / "secrets.json").read_text())
_secrets["site_passwords"] = {HOST: KNOWN, "nodot": "Ignored@Pw99"}
(_TMP / "personal" / "secrets.json").write_text(json.dumps(_secrets))
os.environ["JOBO_PERSONAL_DIR"] = str(_TMP / "personal")
os.environ.pop("JOBO_ACCOUNT_PASSWORD", None)

from policy import personal  # noqa: E402

personal.reload()

from module4 import agent_driver, form_filler, password_reset, prompts, submission_audit  # noqa: E402

PLUGIN_DIR = REPO / "hermes_plugin" / "jobo-browser"
results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, note: str = "") -> None:
    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label:62s} {note}")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_personal():
    print("\n[1] policy.personal.site_password")
    std = personal.account_password()
    check("standard password still loads", bool(std) and std != KNOWN)
    check("exact host", personal.site_password(HOST) == KNOWN)
    check("agent's parent domain (NEED_PASSWORD_RESET: wingtip.example)",
          personal.site_password("wingtip.example") == KNOWN)
    check("subdomain of the host", personal.site_password("eu." + HOST) == KNOWN)
    check("unrelated host", personal.site_password("careers.other.example") == "")
    check("bare TLD never matches", personal.site_password("example") == "")
    check("dotless key ignored", "nodot" not in personal.site_passwords())


def _snap(url: str, ref_label: str = "Password") -> str:
    return json.dumps({"success": True, "url": url,
                       "snapshot": f'- textbox "{ref_label}" [required, ref=e17]\n- button "LOGIN" [ref=e19]'})


def test_plugin_guard():
    print("\n[2] plugin guard")
    g = _load("jobo_site_pw_guard", PLUGIN_DIR / "account_guard.py")
    std = g.STANDARD_PASSWORD
    check("guard sees the recorded password", g.SITE_PASSWORDS.get(HOST) == KNOWN)

    # Fresh handoff turn (new Hermes process): host not seen yet.
    g.reset_state()
    check("recorded password typed before any snapshot is not blocked",
          g.check_tool_call("browser_type", {"ref": "e17", "text": KNOWN}) is None)
    check("...and not rewritten", g.password_rewrite("browser_type", {"ref": "e17", "text": KNOWN}) is None)

    # 1481's login page, 2026-10-23 22:30:31.
    g.observe_result("browser_navigate", _snap(f"https://{HOST}/jobs/Login?folderId=109191"))
    check("password field on the host expects the recorded one", g.expected_password() == KNOWN)
    check("recorded password allowed on the host",
          g.check_tool_call("browser_type", {"ref": "e17", "text": KNOWN}) is None
          and g.password_rewrite("browser_type", {"ref": "e17", "text": KNOWN}) is None)
    check("standard password in that field becomes the recorded one",
          g.password_rewrite("browser_type", {"ref": "e17", "text": std}) == {"text": KNOWN})
    msg = g.check_tool_call("browser_type", {"ref": "e99", "text": "Alex@Zeno2026!"}) or ""
    check("invented password still blocked, told the recorded one", KNOWN in msg and "BLOCKED" in msg, msg[:60])

    # A LinkedIn page in between does not reset the employer host.
    g.observe_result("browser_snapshot", _snap("https://www.linkedin.com/jobs/view/1"))
    check("LinkedIn snapshot keeps the employer host", g.expected_password() == KNOWN)

    # Another employer: the standard rule is unchanged.
    g.observe_result("browser_navigate", _snap("https://jobs.other.example/register"))
    check("other host expects the standard password", g.expected_password() == std)
    check("recorded password on another host is rewritten to the standard",
          g.password_rewrite("browser_type", {"ref": "e17", "text": KNOWN}) == {"text": std})
    check("recorded password into an unknown field on another host is blocked",
          bool(g.check_tool_call("browser_type", {"ref": "e98", "text": KNOWN})))


def test_driver():
    print("\n[3] driver handoffs")
    std = password_reset.STANDARD_PASSWORD
    listing = form_filler.ListFieldsResult(ok=True, fields=[
        form_filler.FieldInfo(index=1, label="Username", kind="text", sig="a"),
        form_filler.FieldInfo(index=2, label="Password", kind="password", sig="b"),
    ])
    seen = []

    def fake_set(cdp_url, hint, value, index=None):
        seen.append(value)
        return form_filler.SetFieldResult(ok=True, field_name=hint, value=value, matched="Password")

    def tabs(url):
        return lambda cdp_url: [{"url": url, "type": "page", "webSocketDebuggerUrl": "ws://x"}]

    for cdp, url in (("http://site", f"https://{HOST}/jobs/Login"), ("http://other", "https://jobs.other.example/")):
        form_filler.remember_listing(cdp, listing)
        with patch.object(form_filler, "set_field", side_effect=fake_set), \
                patch.object(form_filler, "_page_targets", side_effect=tabs(url)):
            reply = agent_driver._resolve_field(f"NEED_FIELD: #2 = {std}", cdp_url=cdp, job_id=1481, failures={})
            if cdp == "http://site":
                site_reply = reply
    check("password field on the host gets the recorded password", seen[0] == KNOWN)
    check("agent told it is the existing account's password", KNOWN in site_reply and "earlier run" in site_reply)
    check("password field elsewhere keeps the standard password", seen[1] == std)

    with patch.object(password_reset, "find_reset_link") as lookup, \
            patch.object(agent_driver.time, "sleep") as slept:
        msg = agent_driver._resolve_password_reset(f"NEED_PASSWORD_RESET: {HOST}", job_id=1481)
    check("reset for the host answered with sign-in", msg.startswith("PASSWORD_RESET_NOT_NEEDED") and KNOWN in msg,
          msg[:40])
    check("no mailbox read, no wait", not lookup.called and not slept.called)

    with patch.object(password_reset, "find_reset_link",
                      return_value=password_reset.ResetLinkResult(error="no mail")), \
            patch.object(agent_driver.time, "sleep"):
        other = agent_driver._resolve_password_reset("NEED_PASSWORD_RESET: jobs.other.example", job_id=7)
    check("reset elsewhere still goes to the mailbox", other.startswith("PASSWORD_RESET_FAILED"), other[:40])


def test_prompt():
    print("\n[4] user prompt")
    kw = dict(job_id=1481, company="Wingtip", title="Data Engineer", resume_path="/tmp/r.pdf", cover_path=None)
    on_site = prompts.user_prompt(url=f"https://{HOST}/jobs/FolderDetail?folderId=109191", **kw)
    elsewhere = prompts.user_prompt(url="https://jobs.other.example/apply/1", **kw)
    check("prompt for the host names the recorded password", "EXISTING ACCOUNT" in on_site and KNOWN in on_site)
    check("prompt elsewhere does not", "EXISTING ACCOUNT" not in elsewhere and KNOWN not in elsewhere)


def test_audit():
    print("\n[5] audit redaction")
    out = submission_audit._redact_passwords({"Account": f"signed in with {KNOWN}", "Name": "Alex"})
    check("recorded password redacted", KNOWN not in json.dumps(out) and out["Name"] == "Alex", str(out)[:60])


def main() -> int:
    try:
        for t in (test_personal, test_plugin_guard, test_driver, test_prompt, test_audit):
            t()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
