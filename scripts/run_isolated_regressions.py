#!/usr/bin/env python3
"""Safe default regression runner (Linux, Chromium, repository Python env).

Usage: bash scripts/run_regression_tests.sh [suite ...] [--timeout 600]
       bash scripts/run_regression_tests.sh --list
       bash scripts/run_regression_tests.sh --self-test

Each suite gets a fresh process, allowlisted environment, empty HOME/profile,
random CDP port and local headless launcher. Legacy modules are imported, not
rewritten: only their CDP globals, generated fixture path, recovery kill helper,
and profile argument to the REAL browser lifecycle functions are substituted.
Explicit self-contained suites retain their own browser/fixture lifecycle.
Unittest modules are loaded by module object, never unittest.main(__main__).

Guards reject production port 9333, remote/model network, unapproved subprocesses,
credentials, and writes outside the disposable directory. Existing disk SQLite
DBs outside it are opened read-only, with an authorizer that refuses writes.
A caught guard violation still fails the suite. These are regression accident
barriers, not an OS sandbox for adversarial code (native Chromium/Node have their
own processes). No live-agent E2E scripts belong in the default manifest.

Logs and summary.json survive in a unique /tmp/jobo-regressions-* directory;
profiles are removed in finally, including timeouts. SIGKILL of the orchestrator
itself cannot run finally. Cleanup uses explicit PID identities, never pkill.
"""
from __future__ import annotations

import argparse
import ctypes
import functools
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import signal
import socket
import sqlite3
import sqlite3.dbapi2
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
import urllib.parse
import urllib.request

REPO = Path(__file__).resolve().parents[1]
SUITES = (
    'test_answer_book', 'test_budget_escalation', 'test_run_bugfixes',
    'test_upload_stale_node', 'test_upload_tab_targeting', 'test_form_filler',
    'test_live_failure_replay', 'test_react_widgets', 'test_clicker',
    'test_field_handoff', 'test_employer_blocklist', 'test_password_reset',
    'test_run_verdict', 'test_preflight', 'test_otp_extraction',
    'test_uploader_guard', 'test_uploader', 'test_blocklist_runner_gate',
    'test_browser_recovery', 'test_upload_stability', 'test_form_widget_types',
    'test_field_clear', 'test_closed_not_applied',
    'test_provider_outage_accounting', 'test_module3_triage',
    'test_apply_token_controls', 'test_otp_handoff', 'test_dropdown_shapes',
    'test_avature_school', 'test_google_forms', 'test_otp_sender_guard',
    'test_otp_employer_brand', 'test_otp_workday_activation',
    'test_linkedin_session', 'test_avature_budget', 'test_upload_twin_inputs', 'test_upload_chooser_iframe', 'test_upload_company_sites',
    'test_captcha_walls', 'test_skip_walls', 'test_easy_apply_resume',
    'test_easy_apply_captcha_misread',
    'test_outcome_taxonomy', 'test_model_guard', 'test_budget_guard',
    'test_budget_gate', 'test_avature_work_history', 'test_parallel_apply',
    'test_reset_requeue', 'test_workday_click_filter',
    'test_workday_create_account',
    'test_hermes_pin', 'test_enrich_recovery',
    'test_attach_canary', 'test_session_limit',
    'test_apply_speed', 'test_avature_stale_numbers',
    'test_setup_wizard', 'test_source_dedup', 'test_jobfound_source',
    'test_jobfound_stages',
)
LEGACY_BROWSER = {
    'test_upload_stale_node', 'test_form_filler', 'test_live_failure_replay',
    'test_react_widgets', 'test_uploader', 'test_browser_recovery',
    'test_linkedin_session', 'test_parallel_apply',
}
SELF_ISOLATED = {
    'test_clicker', 'test_upload_stability', 'test_form_widget_types',
    'test_upload_tab_targeting', 'test_field_clear', 'test_dropdown_shapes',
    'test_avature_school', 'test_avature_budget', 'test_upload_twin_inputs', 'test_upload_chooser_iframe', 'test_upload_company_sites',
    'test_captcha_walls', 'test_easy_apply_resume', 'test_avature_work_history',
    'test_workday_click_filter', 'test_workday_create_account', 'test_avature_stale_numbers',
}
UNITTEST_SUITES = {'test_clicker', 'test_upload_stability', 'test_field_clear',
                   'test_workday_click_filter', 'test_workday_create_account'}


class IsolationError(RuntimeError):
    pass


def beneath(path, root: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(root.resolve())
    except (TypeError, ValueError, OSError):
        return False


def processes() -> dict:
    """PID + start time prevent PID-reuse mistakes; argv is never shell text."""
    result = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            args = (entry / 'cmdline').read_bytes().decode(errors='replace').split('\0')[:-1]
            result[int(entry.name)] = (stat[19], int(stat[1]), args, stat[0])
        except (OSError, ValueError, IndexError):
            continue
    return result


def flag(args: list[str], name: str) -> str | None:
    # Chromium can rewrite /proc/cmdline as a single, space-separated title.
    # Decode only known browser titles; flag matching remains exact-token.
    if len(args) == 1 and Path(args[0].split(' ', 1)[0]).name in {'chromium', 'chrome'}:
        try:
            args = shlex.split(args[0])
        except ValueError:
            return None
    for i, arg in enumerate(args):
        if arg.startswith(name + '='):
            return arg.split('=', 1)[1]
        if arg == name and i + 1 < len(args):
            return args[i + 1]
    return None


def signal_pid(pid: int, identity: str, sig: int) -> None:
    """Signal an exact process through pidfd after rechecking its start time.

    uv's standalone Python may omit os.pidfd_open even on a supporting kernel;
    glibc exposes the same API without falling back to race-prone PID signals.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.pidfd_open(ctypes.c_int(pid), ctypes.c_uint(0))
    if fd < 0:
        error = ctypes.get_errno()
        if error == 3:  # ESRCH: process exited before pidfd_open
            return
        raise OSError(error, os.strerror(error))
    try:
        current = processes().get(pid)
        if current and current[0] == identity and current[3] != 'Z':
            result = libc.pidfd_send_signal(ctypes.c_int(fd), ctypes.c_int(sig),
                                           ctypes.c_void_p(), ctypes.c_uint(0))
            if result < 0 and ctypes.get_errno() != 3:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
    finally:
        os.close(fd)


def kill_owned_browsers(root: Path, port: int, profile: Path | None = None) -> list[int]:
    """Recovery requires BOTH exact port and profile. Cleanup also accepts the
    explicit self-isolated suites' port=0 browsers, only below this suite root.
    Descendants are included by actual ancestry, not a command-line substring.
    """
    table = processes()
    owned = set()
    for pid, (_, _, args, state) in table.items():
        p = flag(args, '--user-data-dir')
        argport = flag(args, '--remote-debugging-port')
        if not p or state == 'Z' or not beneath(p, root):
            continue
        if profile is not None:
            matches = Path(p).resolve() == profile.resolve() and argport == str(port)
        else:
            matches = argport in {str(port), '0'}
        if matches:
            owned.add(pid)
    # Freeze browser roots before killing children: otherwise Chromium can
    # respawn a GPU/renderer between the ancestry snapshot and the final kill.
    for pid in owned:
        signal_pid(pid, table[pid][0], signal.SIGSTOP)
    if owned:
        table = {**processes(), **table}  # Never replace captured identities.
    while True:
        more = {pid for pid, info in table.items() if info[1] in owned}
        if more <= owned:
            break
        owned |= more
    for pid in sorted(owned, reverse=True):
        signal_pid(pid, table[pid][0], signal.SIGKILL)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        now = processes()
        if all(pid not in now or now[pid][0] != table[pid][0] or now[pid][3] == 'Z' for pid in owned):
            return sorted(owned)
        time.sleep(.05)
    raise IsolationError(f'Owned browser PIDs did not exit: {sorted(owned)}')


def production_fingerprint() -> dict:
    """Read /proc only: do not even connect to the user's CDP endpoint."""
    # Renderer processes inherit the debugging-port flag and legitimately
    # change during navigation. Only the browser root owns the CDP listener.
    procs = {str(pid): info[0] for pid, info in processes().items()
             if flag(info[2], '--remote-debugging-port') == '9333'
             and flag(info[2], '--type') in {None, 'browser'} and info[3] != 'Z'}
    listeners = []
    for filename in ('/proc/net/tcp', '/proc/net/tcp6'):
        for line in Path(filename).read_text().splitlines()[1:]:
            fields = line.split()
            if int(fields[1].rsplit(':', 1)[1], 16) == 9333 and fields[3] == '0A':
                listeners.append((filename, fields[1], fields[9]))
    return {'pids_starttime': procs, 'listening_sockets': sorted(listeners)}


class Guards:
    def __init__(self, root: Path, port: int, launcher: Path):
        self.root, self.port, self.launcher = root, port, launcher
        self.violations: list[str] = []

    def refuse(self, message):
        self.violations.append(message)
        raise IsolationError(message)

    def network(self, host, port):
        if int(port or 0) == 9333:
            self.refuse('Forbidden production CDP port 9333')
        try:
            local = host == 'localhost' or ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        # Local ephemeral fixture servers and random CDP ports only. Known
        # production model/service ports (11434, 8000, etc.) are not eligible.
        if not local or int(port or 0) < 32768:
            self.refuse(f'Non-fixture network refused: {host}:{port}')

    def url(self, value):
        raw = getattr(value, 'full_url', value)
        parsed = urllib.parse.urlsplit(str(raw))
        if parsed.scheme not in {'http', 'https', 'ws', 'wss'}:
            self.refuse(f'Non-fixture URL scheme refused: {parsed.scheme}')
        self.network(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80))

    def write(self, path):
        if isinstance(path, int) or os.fspath(path) == '/dev/null':
            return
        if not beneath(path, self.root):
            self.refuse(f'Write outside suite directory refused: {path}')

    def command(self, executable, args):
        if not isinstance(args, (list, tuple)):
            self.refuse('Shell subprocess refused')
        argv = [os.fsdecode(a) for a in args]
        exe = Path(os.fsdecode(executable)).resolve()
        # Playwright's private Node driver, not arbitrary node commands.
        if exe.name == 'node' and len(argv) == 3 and argv[-1] == 'run-driver':
            driver = Path(argv[1]).resolve()
            if driver.name == 'cli.js' and 'playwright/driver/package' in str(driver):
                return
        browser = exe == self.launcher.resolve() or exe.name in {
            'chromium', 'chromium-browser', 'chrome', 'chrome-headless-shell',
        }
        if not browser:
            self.refuse(f'Subprocess refused (no agents/models/shell commands): {exe}')
        p = flag(argv, '--user-data-dir')
        cdp = flag(argv, '--remote-debugging-port')
        if not p or not beneath(p, self.root) or cdp not in {str(self.port), '0'}:
            self.refuse('Browser launch without owned profile and isolated port refused')
        if exe != self.launcher.resolve() and not any(a.startswith('--headless') for a in argv):
            self.refuse('Headed browser launch refused')

    def audit(self, event, args):
        if event == 'socket.connect':
            address = args[1]
            if isinstance(address, tuple):
                self.network(address[0], address[1])
        elif event == 'urllib.Request':
            self.url(args[0])
        elif event == 'subprocess.Popen':
            self.command(args[0], args[1])
        elif event in {'os.system', 'os.posix_spawn', 'os.exec'}:
            self.refuse(f'Unapproved process execution: {event}')
        elif event == 'open':
            path, mode, flags = args
            if isinstance(path, (str, bytes, os.PathLike)):
                resolved = Path(os.fsdecode(path)).resolve()
                if not beneath(resolved, self.root) and (
                    'credentials' in resolved.parts or resolved.name == '.env'
                    or 'jobo-linkedin-profile' in resolved.parts
                ):
                    self.refuse(f'Credential/profile access refused: {resolved}')
            if (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
            ):
                self.write(path)
        elif event in {'os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.truncate'}:
            # dir_fd-relative deletion inside TemporaryDirectory is already
            # constrained by its root. Resolve the fd rather than cwd.
            path = args[0]
            fd = args[-1] if event in {'os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod'} else None
            if isinstance(fd, int) and fd >= 0 and not Path(path).is_absolute():
                path = Path(os.readlink(f'/proc/self/fd/{fd}')) / path
            self.write(path)
        elif event in {'os.rename', 'os.link', 'os.symlink'}:
            self.write(args[1])
            if event == 'os.rename':
                self.write(args[0])

    def install(self):
        # Guard both urlopen and custom openers; audit also catches direct
        # HTTPConnection/WebSocket sockets and missed literal CDP addresses.
        original_open = urllib.request.OpenerDirector.open
        @functools.wraps(original_open)
        def guarded_open(opener, fullurl, *args, **kwargs):
            self.url(fullurl)
            return original_open(opener, fullurl, *args, **kwargs)
        setattr(urllib.request.OpenerDirector, 'open', guarded_open)
        original_connect = sqlite3.connect
        def connect(database, *args, **kwargs):
            raw = os.fsdecode(database)
            if raw == ':memory:' or (not raw.startswith('file:') and beneath(raw, self.root)):
                return original_connect(database, *args, **kwargs)
            parsed = urllib.parse.urlsplit(raw)
            path = Path(urllib.parse.unquote(parsed.path) if raw.startswith('file:') else raw).resolve()
            kwargs['uri'] = True
            conn = original_connect(path.as_uri() + '?mode=ro', *args, **kwargs)
            conn.execute('PRAGMA query_only=ON')
            allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                       sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT, sqlite3.SQLITE_RECURSIVE}
            def authorize(action, first, second, db, trigger):
                if action in allowed:
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_PRAGMA and (
                    second is None or first in {'table_info', 'table_xinfo', 'index_info', 'index_list', 'foreign_key_list'}
                ):
                    return sqlite3.SQLITE_OK
                self.violations.append(f'SQLite write/unsafe operation refused: {action} {first}')
                return sqlite3.SQLITE_DENY
            conn.set_authorizer(authorize)
            return conn
        sqlite3.connect = sqlite3.dbapi2.connect = connect
        sys.addaudithook(self.audit)


def child(name: str, root: Path, port: int) -> int:
    profile = root / 'profile'
    guards = Guards(root, port, Path(os.environ['JOBO_CHROME_BIN']))
    guards.install()
    sys.path.insert(0, str(REPO))
    sys.argv = [str(REPO / 'scripts' / (name + '.py'))]
    code = 1
    try:
        if name == '__guards__':
            code = guard_tests(guards)
        else:
            from module4 import browser_session
            if name not in SELF_ISOLATED:
                # No fake lifecycle responses: substitute the argument, then invoke
                # the actual implementation including real recovery assertions.
                for attr in ('ensure_browser_running', 'ensure_healthy'):
                    real = getattr(browser_session, attr)
                    def wrap(fn):
                        @functools.wraps(fn)
                        def isolated(_profile_dir=None, *args, **kwargs):
                            kwargs.pop('profile_dir', None)
                            return fn(profile, *args, **kwargs)
                        return isolated
                    setattr(browser_session, attr, wrap(real))
            spec = importlib.util.spec_from_file_location(f'isolated_{name}', REPO / 'scripts' / (name + '.py'))
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            if name in LEGACY_BROWSER:
                for attr in ('CDP', 'CDP_URL'):
                    if hasattr(module, attr):
                        setattr(module, attr, f'http://127.0.0.1:{port}')
                if hasattr(module, 'CDP_PORT'):
                    setattr(module, 'CDP_PORT', port)
                if name == 'test_uploader':
                    setattr(module, 'TWO_SLOT', root / 'cdp_upload_probe_two_slot.html')
                if name == 'test_browser_recovery':
                    def kill_browser():
                        pids = kill_owned_browsers(root, port, profile)
                        if not pids:
                            raise IsolationError('Recovery kill found no exact owned browser PID')
                        print(f'Isolated recovery killed exact owned PIDs: {pids}', flush=True)
                    setattr(module, 'kill_browser', kill_browser)
                # Recovery must exercise its OWN cold-start branch, not a prestart.
                elif not browser_session.ensure_healthy(profile):
                    raise IsolationError('Isolated Chromium did not become healthy')
            if name in UNITTEST_SUITES:
                suite = unittest.defaultTestLoader.loadTestsFromModule(module)
                if suite.countTestCases() == 0:
                    raise IsolationError('Unittest discovery found zero tests')
                result = unittest.TextTestRunner(verbosity=2).run(suite)
                print(f'HARNESS_TEST_COUNTS run={result.testsRun} failures={len(result.failures)} '
                      f'errors={len(result.errors)} skipped={len(result.skipped)}', flush=True)
                code = 0 if result.wasSuccessful() and not result.skipped else 1
            else:
                code = int(module.main() or 0)
    except BaseException:
        traceback.print_exc()
        code = 1
    finally:
        try:
            killed = kill_owned_browsers(root, port)
            print(f'HARNESS_CLEANUP owned_pids={killed}', flush=True)
        except Exception:
            traceback.print_exc()
            code = 1
        if guards.violations:
            print('HARNESS_GUARD_FAILURES ' + json.dumps(guards.violations), flush=True)
            code = 1
    return code


def guard_tests(guards: Guards) -> int:
    """Deliberate forbidden actions; every one must throw before doing I/O."""
    before = production_fingerprint()
    probes = [
        ('urlopen production', lambda: urllib.request.urlopen('http://127.0.0.1:9333/json/version')),
        ('custom opener production', lambda: urllib.request.build_opener().open('http://localhost:9333/json/new?about:blank')),
        ('raw socket production', lambda: socket.create_connection(('127.0.0.1', 9333))),
        ('remote model', lambda: urllib.request.urlopen('https://api.openai.com/v1/models')),
        ('local model', lambda: urllib.request.urlopen('http://127.0.0.1:11434/api/tags')),
        ('agent subprocess', lambda: subprocess.run(['hermes', 'chat', '-q', 'FORBIDDEN'])),
        ('pkill subprocess', lambda: subprocess.run(['pkill', '-f', 'remote-debugging-port=9333'])),
        ('production file write', lambda: (REPO / 'data' / 'forbidden-regression-write').write_text('forbidden')),
    ]
    for label, fn in probes:
        start = len(guards.violations)
        try:
            fn()
        except IsolationError:
            assert len(guards.violations) > start, label
            del guards.violations[start:]  # expected ONLY in this explicit self-test
            print(f'PASS guard: {label}', flush=True)
        else:
            raise AssertionError(f'Guard did not refuse: {label}')
    with sqlite3.connect(REPO / 'data' / 'jobs.db') as conn:
        count = conn.execute('SELECT count(*) FROM jobs').fetchone()[0]
        start = len(guards.violations)
        try:
            conn.execute('DELETE FROM jobs WHERE 0')
        except sqlite3.DatabaseError:
            assert len(guards.violations) > start
            del guards.violations[start:]
        else:
            raise AssertionError('Database write was not refused')
        assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0] == count
    print('PASS guard: live SQLite reads allowed, writes refused', flush=True)
    assert production_fingerprint() == before, 'Production process/listener changed during guard tests'
    print('PASS guard: production PID identity and listener unchanged; zero CDP requests', flush=True)
    print(f'{len(probes) + 2}/{len(probes) + 2} passed', flush=True)
    return 0


def environment(root: Path, port: int, chrome: str) -> dict[str, str]:
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL', 'TZ') if k in os.environ}
    for name in ('home', 'tmp', 'cache', 'config', 'runtime', 'profile'):
        (root / name).mkdir(mode=0o700)
    launcher = root / 'headless-chromium'
    # No inherited JOBO_CHROME_BIN; discover the system executable ourselves.
    launcher.write_text('#!/bin/sh\nexec ' + shlex.quote(chrome) +
                        ' --headless=new --disable-gpu --disable-background-networking'
                        ' --disable-component-update --no-first-run --no-default-browser-check'
                        ' --disable-sync --no-proxy-server'
                        " --host-resolver-rules='MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1'"
                        ' "$@"\n')
    launcher.chmod(0o700)
    env.update({
        'HOME': str(root / 'home'), 'TMPDIR': str(root / 'tmp'),
        'XDG_CACHE_HOME': str(root / 'cache'), 'XDG_CONFIG_HOME': str(root / 'config'),
        'XDG_RUNTIME_DIR': str(root / 'runtime'), 'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONUNBUFFERED': '1', 'PYTHONNOUSERSITE': '1',
        'JOBO_BROWSER_CDP_PORT': str(port), 'JOBO_BROWSER_PROFILE': str(root / 'profile'),
        'JOBO_CHROME_BIN': str(launcher), 'BROWSER_CDP_URL': f'http://127.0.0.1:{port}',
        # Suites stub the agent and never run Hermes. A path that does not
        # exist keeps the runner's Hermes preflight off the real install.
        'JOBO_HERMES_BIN': str(root / 'no-hermes'),
        # The attach canary (GHO-17) is a real agent turn, which these suites
        # refuse; suites that test it inject a fake attach_check instead.
        'JOBO_ATTACH_CANARY': '0',
        # Suites always run as the fictional example person, never the real
        # personal/ config (policy/personal.py).
        'JOBO_PERSONAL_DIR': str(Path(__file__).resolve().parents[1] / 'personal.example'),
        # Read-only passthrough of the REAL home directory, distinct from the
        # sandboxed HOME above. test_model_guard needs this to find the real
        # ~/.hermes/profiles/jobo/config.yaml on disk - the model guard is a
        # safety check on the actual deployed profile, not a fixture. Looked
        # up via the passwd DB (not $HOME/Path.home()): this orchestrator
        # process may itself be running under an already-redirected HOME
        # (e.g. inside an agent sandbox), and Path.home() would just repeat
        # that redirection instead of finding the real one.
        'JOBO_HOST_HOME': pwd.getpwuid(os.getuid()).pw_dir,
        # Production lifecycle checks this string; headless Chromium never uses
        # the user's DISPLAY/XAUTHORITY/Wayland session (none is inherited).
        'DISPLAY': ':65535', 'NO_PROXY': 'localhost,127.0.0.1,::1',
        # Playwright may need its installed browser cache, read-only, not HOME.
        'PLAYWRIGHT_BROWSERS_PATH': os.environ.get('PLAYWRIGHT_BROWSERS_PATH', str(Path.home() / '.cache/ms-playwright')),
    })
    return env


def cleanup_suite(proc, root: Path, port: int) -> list[str]:
    """Attempt every independent cleanup even when child termination fails."""
    errors = []
    try:
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception as exc:
                if not isinstance(exc, subprocess.TimeoutExpired):
                    errors.append(repr(exc))
                try:
                    proc.kill()
                except Exception as exc:
                    errors.append(repr(exc))
                try:
                    proc.wait(timeout=5)
                except Exception as exc:
                    errors.append(repr(exc))
    except Exception as exc:
        errors.append(repr(exc))
    finally:
        try:
            kill_owned_browsers(root, port)
        except Exception as exc:
            errors.append(repr(exc))
        finally:
            try:
                shutil.rmtree(root)
            except Exception as exc:
                errors.append(repr(exc))
    return errors


def check_count(text: str) -> int:
    """Use emitted counts or actual PASS lines, never invent a suite count."""
    discovered = re.findall(r'HARNESS_TEST_COUNTS run=(\d+)', text)
    if discovered:
        return int(discovered[-1])
    summaries = re.findall(r'(\d+)/(\d+) (?:widget checks )?passed', text)
    if summaries:
        return int(summaries[-1][1])
    return len(re.findall(r'(?m)^\s*PASS(?:\s|:)', text))


class CleanupTests(unittest.TestCase):
    """Fault injection uses only temporary resources and mock processes."""
    def test_production_fingerprint_ignores_renderer_churn_not_root_restart(self):
        from unittest.mock import patch
        root = ('root-start', 1, ['chromium', '--remote-debugging-port=9333'], 'S')
        renderer = ('renderer-start', 101, ['chromium', '--type=renderer', '--remote-debugging-port=9333'], 'S')
        with patch.object(Path, 'read_text', return_value='header\n'):
            with patch(__name__ + '.processes', return_value={101: root, 102: renderer}):
                before = production_fingerprint()
            with patch(__name__ + '.processes', return_value={101: root, 103: renderer}):
                after = production_fingerprint()
            self.assertEqual(before, after)
            self.assertEqual(before['pids_starttime'], {'101': 'root-start'})
            changed = ('new-start', 1, root[2], 'S')
            with patch(__name__ + '.processes', return_value={101: changed}):
                self.assertNotEqual(before, production_fingerprint())

    def test_cleanup_errors_do_not_skip_other_resources(self):
        from unittest.mock import Mock, patch
        for stage in ('poll', 'terminate', 'wait', 'kill', 'browser'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as parent:
                root = Path(parent) / 'suite'
                root.mkdir()
                (root / 'profile').mkdir()
                proc = Mock()
                proc.poll.return_value = None
                if stage == 'kill':
                    proc.wait.side_effect = subprocess.TimeoutExpired('fake', 1)
                if stage != 'browser':
                    getattr(proc, stage).side_effect = RuntimeError(stage)
                with patch(__name__ + '.kill_owned_browsers',
                           side_effect=RuntimeError('browser') if stage == 'browser' else None) as kill:
                    errors = cleanup_suite(proc, root, 45678)
                self.assertTrue(errors)
                kill.assert_called_once_with(root, 45678)
                self.assertFalse(root.exists())

    def test_setup_failure_removes_partial_profile(self):
        from unittest.mock import patch
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as parent:
            logs = Path(parent) / 'logs'
            logs.mkdir()
            root = logs / 'suite'
            root.mkdir()
            def broken_setup(root, port, chrome):
                (root / 'profile').mkdir()
                raise OSError('injected setup failure')
            with patch.object(sys, 'argv', ['runner', 'test_answer_book']), \
                 patch(__name__ + '.tempfile.mkdtemp', side_effect=[str(logs), str(root)]), \
                 patch(__name__ + '.environment', side_effect=broken_setup), \
                 patch(__name__ + '.production_fingerprint', return_value={}), \
                 patch(__name__ + '.shutil.which', return_value='/fake/chromium'), \
                 patch(__name__ + '.signal.signal'), \
                 patch(__name__ + '.kill_owned_browsers', return_value=[]) as kill, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 1)
            kill.assert_called_once()
            self.assertFalse(root.exists())
            record = json.loads((logs / 'summary.json').read_text())['suites'][0]
            self.assertTrue(record['failed'])
            self.assertTrue(record['profile_removed'])
            self.assertIn('injected setup failure', Path(record['log']).read_text())

    def test_stability_and_targeting_teardown_order(self):
        from unittest.mock import Mock, patch
        from types import SimpleNamespace
        from scripts.test_upload_stability import Stability
        from scripts.test_upload_tab_targeting import Stability as Targeting
        for base in (Stability, Targeting):
            with tempfile.TemporaryDirectory() as parent:
                fake = SimpleNamespace(tmp=SimpleNamespace(name=parent, cleanup=Mock()),
                                       proc=Mock(), log=Mock())
                events = []
                fake.proc.wait.side_effect = lambda **kw: events.append('wait')
                def owned(root, port, profile):
                    self.assertEqual((root, port, profile), (Path(parent), 0, Path(parent) / 'profile'))
                    events.append('owned')
                with patch('scripts.run_isolated_regressions.kill_owned_browsers', side_effect=owned), \
                     patch('os.killpg', side_effect=AssertionError('unsafe group kill')):
                    base.tearDownClass.__func__(fake)
                self.assertEqual(events, ['owned', 'wait'])
                fake.log.close.assert_called_once()
                fake.tmp.cleanup.assert_called_once()

    def test_owned_browser_title_and_identity_cleanup(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            table = {
                101: ('owned', 1, [f'/usr/lib/chromium/chromium --remote-debugging-port=0 --user-data-dir={root}/profile'], 'R'),
                102: ('child', 101, ['renderer'], 'R'),
                103: ('other', 1, [f'/usr/lib/chromium/chromium --remote-debugging-port=0 --user-data-dir={root}/profile-other'], 'R'),
            }
            with patch(__name__ + '.processes', side_effect=[table, table, {}]), \
                 patch(__name__ + '.signal_pid') as send:
                self.assertEqual(kill_owned_browsers(root, 0, root / 'profile'), [101, 102])
            self.assertEqual(send.call_args_list[0].args, (101, 'owned', signal.SIGSTOP))
            self.assertEqual({c.args[:2] for c in send.call_args_list}, {(101, 'owned'), (102, 'child')})

    def test_nonzero_emitted_checks(self):
        for text in ('', '0/0 passed', 'ALL CHECKS PASSED', 'HARNESS_TEST_COUNTS run=0'):
            self.assertEqual(check_count(text), 0, text)
        self.assertEqual(check_count('  PASS  actual preflight check\nALL CHECKS PASSED'), 1)
        self.assertEqual(check_count('PASS: runner refuses a blocked employer'), 1)
        self.assertEqual(check_count('2/2 passed'), 2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('suites', nargs='*')
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--timeout', type=int, default=600, help='seconds per suite (1..600)')
    parser.add_argument('--child', help=argparse.SUPPRESS)
    parser.add_argument('--root', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--port', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        if args.child not in (*SUITES, '__guards__') or not args.root or not args.port or args.port == 9333:
            parser.error('Invalid internal child arguments')
        return child(args.child, args.root.resolve(), args.port)
    if args.list:
        for name in SUITES:
            mode = 'self-isolated' if name in SELF_ISOLATED else 'legacy-browser' if name in LEGACY_BROWSER else 'offline/read-only'
            print(f'{name:32} {mode}')
        print(f'{len(SUITES)} default suites; live-agent suites excluded')
        return 0
    if not 1 <= args.timeout <= 600:
        parser.error('--timeout must be between 1 and 600 seconds')
    names = ['__guards__'] if args.self_test else (args.suites or list(SUITES))
    if args.self_test and args.suites:
        parser.error('--self-test takes no suite names')
    if len(set(names)) != len(names) or any(n not in (*SUITES, '__guards__') for n in names):
        parser.error('Suites must be unique names from --list (no live-agent tests)')
    if args.self_test:
        sys.path.insert(0, str(REPO))
        result = unittest.TextTestRunner(verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(CleanupTests))
        if not result.wasSuccessful():
            return 1
    chrome = shutil.which('chromium') or shutil.which('chromium-browser')
    if not chrome:
        parser.error('System Chromium required; install it before running regressions')
    logs = Path(tempfile.mkdtemp(prefix='jobo-regressions-', dir='/tmp'))
    before = production_fingerprint()
    records = []
    print(f'Logs: {logs}', flush=True)
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f'signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        for name in names:
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                port = reservation.getsockname()[1]
            if port == 9333 or port < 32768:
                raise IsolationError('OS assigned an unsafe ephemeral port')
            root = Path(tempfile.mkdtemp(prefix='suite-', dir=logs))
            log = logs / (name + '.log')
            started = time.monotonic()
            proc = None
            exitcode = 1
            cleanup_error = None
            try:
                with log.open('w') as output:
                    print(f'{name}: isolated port={port} profile={root / "profile"}', file=output, flush=True)
                    env = environment(root, port, chrome)
                    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--child', name,
                                             '--root', str(root), '--port', str(port)],
                                            cwd=REPO, env=env, stdout=output, stderr=subprocess.STDOUT,
                                            start_new_session=True)
                    try:
                        exitcode = proc.wait(timeout=args.timeout)
                    except subprocess.TimeoutExpired:
                        print(f'HARNESS_TIMEOUT after {args.timeout}s', file=output, flush=True)
                        exitcode = 124
            except Exception:
                exitcode = 1
                try:
                    with log.open('a') as output:
                        traceback.print_exc(file=output)
                except OSError:
                    traceback.print_exc()
            finally:
                errors = cleanup_suite(proc, root, port)
                if errors:
                    cleanup_error = '; '.join(errors)
                    exitcode = exitcode or 1
            text = log.read_text() if log.exists() else ''
            summaries = re.findall(r'\d+/\d+ (?:widget checks )?passed|HARNESS_TEST_COUNTS[^\n]*|ALL CHECKS PASSED|PASS:[^\n]*', text)
            # A legacy zero-exit SKIP is not silently counted as a pass.
            skipped = bool(re.search(r'(?m)^\s*SKIP\b', text))
            checks = check_count(text)
            failed = exitcode != 0 or skipped or cleanup_error is not None or checks == 0
            record = {'suite': name, 'exit_code': exitcode, 'failed': failed,
                      'check_count': checks,
                      'skipped_output': skipped, 'seconds': round(time.monotonic() - started, 2),
                      'summary': summaries[-1] if summaries else '', 'log': str(log),
                      'port': port, 'profile_removed': not root.exists(), 'cleanup_error': cleanup_error}
            records.append(record)
            print(f'{name:32} {"FAIL" if failed else "PASS"} exit={exitcode} '
                  f'{record["summary"]} ({record["seconds"]}s)', flush=True)
            if failed:
                print(f'  See {log}', flush=True)
    except KeyboardInterrupt:
        print('Interrupted; owned resources cleaned up.', flush=True)
    after = production_fingerprint()
    unchanged = before == after
    failed = sum(r['failed'] for r in records)
    complete = len(records) == len(names)
    result = {'requested': len(names), 'completed': len(records), 'passed': len(records) - failed,
              'failed': failed, 'production_process_listener_unchanged': unchanged,
              'production_before': before, 'production_after': after, 'suites': records}
    (logs / 'summary.json').write_text(json.dumps(result, indent=2) + '\n')
    print(f'{result["passed"]}/{len(names)} suites passed; {failed} failed; '
          f'{len(names) - len(records)} not completed', flush=True)
    print(f'Production PID/listener unchanged: {unchanged} (no CDP request sent)', flush=True)
    print(f'Summary: {logs / "summary.json"}', flush=True)
    return 0 if not failed and complete and unchanged else 1


if __name__ == '__main__':
    raise SystemExit(main())
