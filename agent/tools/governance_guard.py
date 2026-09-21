"""In-process guardrails the orchestrator wires around every tool result
before it ever becomes part of the message history sent to Claude.

These two functions are the actual enforcement point for this project's
central claim: PHI masking and prompt-injection defense are architectural,
not just documented in a system prompt. See agent/orchestrator.py for how
they're wired into dispatch(), and tests/test_masking_guard.py +
tests/test_prompt_injection_guard.py for the end-to-end proof.
"""

import re

from common.contracts import MASKED_COLUMNS

REDACTED = "***REDACTED***"


def enforce_masking(table: str, rows: list[dict], role: str) -> list[dict]:
    """Pure function: returns a NEW list of rows with every column listed in
    `common.contracts.MASKED_COLUMNS[table]` replaced with a redaction marker,
    unless `role == "phi_unmasked"`. Never mutates the input rows.

    This must run on every row returned by query_gold_table BEFORE it is
    placed into a ToolResult.content that gets appended to the Claude message
    history — the raw value must never reach that history at all, so no
    prompt (however cleverly worded) can make the model repeat it.
    """
    masked_columns = MASKED_COLUMNS.get(table, set())
    if role == "phi_unmasked" or not masked_columns:
        return [dict(row) for row in rows]

    masked_rows = []
    for row in rows:
        masked_row = dict(row)
        for column in masked_columns:
            if column in masked_row:
                masked_row[column] = REDACTED
        masked_rows.append(masked_row)
    return masked_rows


# Heuristic keyword/phrase patterns suggesting an attempt to override
# instructions or exfiltrate sensitive identifiers via free-text content
# (most notably the `notes` field on a patient event). This is intentionally
# simple and is a defense-in-depth LAYER, not a claim of complete coverage —
# the real guarantee against PHI leakage is enforce_masking() running before
# data ever reaches the model, not this scan. This scan exists to flag
# suspicious content so it can be visibly quarantined (wrapped in
# <untrusted_data> tags) even when it isn't a masked column at all, e.g. an
# injection attempt embedded in a nurse's free-text charting note.
_OVERRIDE_PHRASES = (
    "ignore previous instructions",
    "ignore all previous",
    "ignore prior instructions",
    "disregard previous instructions",
    "you are now",
    "system:",
    "new instructions:",
)

_EXFIL_VERBS = ("reveal", "output the", "print the", "tell me the", "show me the")
_SENSITIVE_NOUNS = ("mrn", "full_name", "full name", "ssn", "social security", "name and", "patient's name")


def scan_for_injection(text: str) -> bool:
    """Heuristic check for prompt-injection attempts in untrusted tool/text
    content. Flags text that either (a) contains a direct instruction-override
    phrase, or (b) combines an exfiltration-style verb with a sensitive-data
    noun in reasonable proximity (the same sentence/short span).

    This is a heuristic, best-effort layer — it will miss cleverly-worded or
    obfuscated attempts, and it is not the primary defense against PHI
    leakage (that's enforce_masking, which runs unconditionally regardless of
    what this function returns). Treat a False here as "nothing obviously
    suspicious," not as a guarantee of safety.
    """
    if not text:
        return False
    lowered = text.lower()

    if any(phrase in lowered for phrase in _OVERRIDE_PHRASES):
        return True

    for verb in _EXFIL_VERBS:
        verb_idx = lowered.find(verb)
        if verb_idx == -1:
            continue
        window = lowered[max(0, verb_idx - 60) : verb_idx + 60]
        if any(noun in window for noun in _SENSITIVE_NOUNS):
            return True

    # Also flag a bare "reveal"/"leak" directly adjacent to a sensitive noun
    # even without one of the specific verb phrases above.
    if re.search(r"\b(reveal|leak|expose)\b", lowered) and any(n in lowered for n in _SENSITIVE_NOUNS):
        return True

    return False
