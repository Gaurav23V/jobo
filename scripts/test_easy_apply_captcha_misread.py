"""LinkedIn Easy Apply "captcha" drops that were never captchas (GHO-14).

LinkedIn's Easy Apply modal always carries an invisible reCAPTCHA Enterprise
frame. Five jobs whose Next/Submit did not advance were reported as
captcha_failed (or "bot-detection decoy") because the agent found that frame,
and module4/blockers.py dropped them for good. No challenge was shown in any
of them, and Easy Apply submissions kept going through in the same sessions.
These checks pin, on the verbatim module4_last_status rows:
  * the five are retryable now;
  * every real captcha wall (a challenge that was SHOWN, a Turnstile hang, a
    whole-page bot wall) is still permanent;
  * the apply prompt tells the agent the frame is not a captcha.

Offline; no browser. Run: uv run python scripts/test_easy_apply_captcha_misread.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import blockers, prompts  # noqa: E402

PASS = FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


# Easy Apply attempts dropped as captcha walls with no challenge ever shown.
MISREAD = {
    1053: 'captcha_failed:LinkedIn Easy Apply modal has an invisible reCAPTCHA Enterprise widget; the Next button click registers but the form never advances past step 1/4 (Contact info) despite 6 attempts, consistent with bot-detection blocking automated submission.',
    1282: 'captcha_failed:LinkedIn Easy Apply form for this Jobrelay/Lever posting is gated by an invisible Google reCAPTCHA Enterprise widget; the Next button on page 1/4 is enabled and receives clicks but never advances despite all required fields (name, phone, email) being correctly filled, consistent with a silent anti-bot token check blocking automated submission.',
    1328: 'stuck:LinkedIn Easy Apply modal reproducibly fails to advance past page 1/4 (Contact info) — Next button clicks report success but the page never progresses, and frame_tree still shows only a blank unresolvable oopif, confirming the same bot-detection decoy failure as the prior attempt; no company-website apply route exists.',
    1468: 'captcha_failed:LinkedIn Easy Apply "Next" button on page 1/4 never advances despite multiple genuine clicks (normal and raw CDP mouse events); an invisible reCAPTCHA Enterprise iframe is active on the page, consistent with an anti-bot fingerprint check blocking automated submission.',
    1612: 'captcha_failed:LinkedIn Easy Apply repeatedly fails with "We couldn\'t submit your application. Please try again." alongside an invisible Google reCAPTCHA enterprise frame on the page, indicating a bot-detection block on submission; form was fully filled through the Review step but the Submit click never completes.',
}

# Real walls: must stay permanent.
REAL_WALLS = {
    1158: 'captcha_failed:Larchmont/BambooHR application form fully completed and resume uploaded, but final submission is blocked by a Google reCAPTCHA checkbox/image challenge in a cross-origin iframe that could not be solved or reached via the click helper.',
    1377: 'captcha_failed:hCaptcha presented an image-selection puzzle after repeated submit attempts, compounded by Lever\'s "Current location" Google Places field rejecting programmatically-set values (requires a genuine autocomplete dropdown selection); form was fully filled but could not be submitted.',
    1496: 'captcha_failed:Submission triggered an hCaptcha drag-and-drop visual puzzle ("complete the pattern by dragging the correct shape into the empty cell") on jobs.lever.co/goldcrest; this is an anti-bot challenge widget, not a text captcha, and cannot be reliably solved with available browser tools.',
    1662: 'captcha_failed:Lever/hCaptcha presented an image-puzzle challenge ("click all icons that show multiple times") after clicking Submit Application; all required form fields were filled correctly but the hCaptcha visual challenge could not be reliably solved',
    1739: 'captcha_failed:hCaptcha "click the two ends of the line" visual challenge (rendered inside a cross-origin enclave iframe on jobs.lever.co) blocks final submission; one genuine solve attempt did not clear it and the puzzle image does not render legibly for repeated attempts.',
    1477: 'captcha_failed:Kestrelway\'s "Apply on company website" flow redirects through a DataDome slider CAPTCHA anti-bot challenge before reaching the ATS form; this is a bot-detection widget, not a text captcha, and cannot be reliably solved by this agent.',
    1313: 'captcha_failed:Cutshort ATS application modal for this job hangs indefinitely at "Reading your resume..." because its background Cloudflare Turnstile widget repeatedly fails with error 300030 ("widget seems to have hung") and never issues a token, blocking the form from proceeding past the resume-upload step.',
    1220: 'captcha_failed:Oracle Cloud Recruiting (adatum.fa.oraclecloud.com) authentication step presents an interactive hCaptcha drag-and-drop puzzle ("drag the animal into the correct silhouette") that cannot be solved visually/textually; Skip Challenge button did not bypass it.',
    1341: 'stuck:Cutshort.io apply page (K&L Python Full stack Developer) has an invisible Cloudflare Turnstile challenge that silently blocks all click handlers; both "Apply to this job" and "Candidate login" buttons produce no modal, navigation, or DOM change across repeated verified-on-screen clicks.',
}


def main() -> int:
    print("\n[1] invisible reCAPTCHA on Easy Apply is retryable")
    for jid, status in MISREAD.items():
        got = blockers.permanent_reason_from_last_status(status)
        check(f"job {jid} retryable", got is None, repr(got))

    print("\n[2] real captcha / bot walls stay permanent")
    for jid, status in REAL_WALLS.items():
        got = blockers.permanent_reason_from_last_status(status)
        check(f"job {jid} still permanent", got is not None, repr(got))
    for label, detail in (
        ("visible image grid on LinkedIn", "LinkedIn Easy Apply showed a reCAPTCHA image challenge (select all squares with buses) after Submit; invisible reCAPTCHA escalated"),
        ("checkbox on LinkedIn", "LinkedIn Easy Apply: reCAPTCHA Enterprise checkbox \"I'm not a robot\" appeared and did not clear"),
        ("invisible reCAPTCHA off LinkedIn", "Ashby form flagged as possible spam by an invisible reCAPTCHA after submit"),
    ):
        got = blockers.permanent_reason("captcha_failed", detail)
        check(f"{label}: still permanent", got is not None, repr(got))

    print("\n[3] the prompt says the frame is not a captcha")
    text = prompts.SYSTEM_PROMPT if hasattr(prompts, "SYSTEM_PROMPT") else ""
    if not text:
        text = "\n".join(v for v in vars(prompts).values() if isinstance(v, str))
    check("Easy Apply note present", "invisible reCAPTCHA frame: that is NOT a captcha" in text)
    check("tells it to read inline errors and use NEED_CLICK",
          "inline errors" in text and "NEED_CLICK: <button label>" in text)

    print(f"\n{PASS}/{PASS + FAIL} passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
