"""Personal values the apply agent must never make up: postal codes, street numbers.

The prompt has said since GHO-92 to never type personal data the candidate
facts do not give. Larkspur 3015 (Oracle Recruiting CX, 2026-10-30) still asked
to set its required "Pin Code" to a city's generic PIN, then to the first three
digits of it, then to the city name. The facts hold no PIN code. It only failed
because the Oracle dropdown setter was broken at the time.

So this is checked in code, on both ways a value reaches a field:
  * NEED_FIELD, in module4/agent_driver.py (_resolve_field)
  * browser_type, in hermes_plugin/jobo-browser/fact_guard.py

Stdlib only: the Hermes plugin loads this file by path (it cannot import the
jobo package).
"""

from __future__ import annotations

import re

# "Pin Code *", "Pincode", "Postal Code", "Post Code", "ZIP", "Zip/Postal Code".
# Not "Postal Address" (a whole address) and not a bare "PIN", which is also
# what sites call an emailed or texted one-time code.
POSTAL_LABEL = re.compile(
    r"\b(?:pin\s*-?\s*code|pincode|postal(?!\s*address)(?:\s*code)?|post\s*code|postcode|zip(?:\s*code)?)\b",
    re.I,
)
# A facts line that names a postal code at all ("PIN code: ...", "ZIP ...").
_POSTAL_FACT = re.compile(r"\b(?:pin\s*-?\s*code|pincode|postal\s*code|post\s*code|postcode|zip)\b", re.I)
ADDRESS_LABEL = re.compile(r"\b(?:address|street)\b", re.I)
_NOT_STREET = re.compile(r"\b(?:e-?mail|web|website|ip|url|mac|wallet)\s*address\b", re.I)
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)


def _known(facts: str) -> str:
    return _URL.sub(" ", str(facts or "")).lower()


def _in_facts(token: str, known: str) -> bool:
    token = re.sub(r"\s+", " ", token.strip().lower())
    return bool(token) and re.search(
        r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", known
    ) is not None


def refusal(label: str, value: str, facts: str) -> str:
    """Why `value` must not go into the field labelled `label`, or "".

    "postal": the field asks for a postal/PIN/ZIP code and `value` is not one
    the facts give (the facts must name a postal code and contain this value).
    "address": an address/street field and `value` carries a number (house,
    street or postal number) that is not in the facts.
    """
    value = str(value or "").strip()
    label = str(label or "")
    if not value or not label:
        return ""
    known = _known(facts)
    if POSTAL_LABEL.search(label):
        if _POSTAL_FACT.search(known) and _in_facts(value, known):
            return ""
        return "postal"
    if ADDRESS_LABEL.search(label) and not _NOT_STREET.search(label):
        if any(not _in_facts(n, known) for n in re.findall(r"\d+", value)):
            return "address"
    return ""


def agent_message(kind: str, label: str, value: str) -> str:
    """What the agent is told instead of the value being typed."""
    what = "a postal/PIN/ZIP code" if kind == "postal" else "a street address with numbers"
    return (
        f"{label.strip()!r} asks for {what}, and {value.strip()!r} is not in the "
        "candidate facts, so it was NOT typed. Never make one up: not a city's "
        "generic code, not part of one, not the city name. Leave this field "
        "empty and carry on with the rest of the form. If the form will not go "
        "on without it, stop and report:\n"
        "    STATUS: stuck\n"
        f"    DETAIL: <site> requires {label.strip()}, which is not in the candidate facts"
    )
