#!/usr/bin/env python3
"""Guard cleanup must never choose a tab already scheduled for asynchronous close."""
from __future__ import annotations
import importlib.util
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('uploader_suite', Path(__file__).with_name('test_uploader.py'))
assert spec is not None and spec.loader is not None
suite = importlib.util.module_from_spec(spec)
spec.loader.exec_module(suite)


def main():
    events = []
    # /json/close acknowledged the previous probe, but /json/list can still
    # include it first. Keeping that stale target and closing the real guard
    # leaves zero tabs when the pending close completes.
    closing = {'id': 'closing-probe'}
    old_guard = {'id': 'old-guard'}
    fresh_guard = {'id': 'fresh-guard'}
    def create(url):
        assert url == 'about:blank'
        events.append(('create', fresh_guard['id']))
        return fresh_guard
    def targets(_):
        return [closing, old_guard] + ([fresh_guard] if events else [])
    def close(tab_id):
        events.append(('close', tab_id))
    with patch.object(suite, 'new_tab', side_effect=create), \
         patch.object(suite, 'close_tab', side_effect=close), \
         patch.object(suite.uploader, 'list_page_targets', side_effect=targets), \
         patch.object(suite.browser_session, 'ensure_guard_tab', return_value=True):
        suite.close_all_but_one()
    assert events and events[0] == ('create', 'fresh-guard'), events
    assert ('close', 'fresh-guard') not in events, events
    assert ('close', 'closing-probe') in events and ('close', 'old-guard') in events, events
    print('1/1 passed: fresh guard created before cleanup; closing probe never chosen as guard')


if __name__ == '__main__':
    main()
