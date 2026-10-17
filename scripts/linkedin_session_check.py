#!/usr/bin/env python3
"""Is the apply browser signed in to LinkedIn? Read-only.

    .venv/bin/python scripts/linkedin_session_check.py            # cookie check
    .venv/bin/python scripts/linkedin_session_check.py --probe    # + feed probe
    .venv/bin/python scripts/linkedin_session_check.py --cdp http://127.0.0.1:9501

Default CDP endpoint is the apply browser's (module4.browser_session.cdp_url,
JOBO_BROWSER_CDP_PORT or 9333). It ATTACHES only - it never launches a browser.

Cookie check: one Storage.getCookies call; prints LinkedIn cookie NAMES only,
never values. --probe also opens linkedin.com/feed in a throwaway tab and
reports where it landed, then closes that tab.

Exit 0 = signed in, 1 = signed out, 2 = could not tell (browser down).
Run it before relaunching the pipeline after a "LinkedIn signed out" stop.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import browser_session, linkedin_session  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cdp", default=None, help="CDP http endpoint (default: the apply browser's)")
    ap.add_argument("--probe", action="store_true", help="also load linkedin.com/feed in a temp tab")
    args = ap.parse_args(argv)
    cdp = (args.cdp or browser_session.cdp_url()).rstrip("/")

    try:
        names = sorted({str(c["name"]) for c in linkedin_session.linkedin_cookie_names(cdp)})
        print(f"LinkedIn cookie names ({len(names)}): {', '.join(names) or '(none)'}")
    except Exception as e:
        print(f"UNKNOWN: cannot read cookies from {cdp}: {e}")
        return 2

    state = linkedin_session.check_session(cdp)
    print(f"cookie check: {state.state.upper()} - {state.reason}")
    if args.probe and not state.signed_out:
        probed = linkedin_session.probe_feed(cdp)
        print(f"feed probe:   {probed.state.upper()} - {probed.reason}")
        if probed.state != "unknown":
            state = probed
    if state.signed_in:
        return 0
    return 1 if state.signed_out else 2


if __name__ == "__main__":
    raise SystemExit(main())
