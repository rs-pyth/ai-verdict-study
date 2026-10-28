"""
The analysis CSV: one row per participant, the scored variables computed in one
place so every figure in the write-up comes from the same code.

Pure functions (no Flask, no database) so test_export.py can check the
arithmetic against hand-computed values.

Scoring (7-point items, 1-7):
  * Jian, Bisantz & Drury (2000), items 1-5 are the DISTRUST items and are
    reverse-scored (8 - x) when building the trust composite; items 6-11 are the
    TRUST items. The composite is the mean of items 1-11 after reversing 1-5.
    Item 12 (familiarity) is NOT part of any composite; item 13 is the
    instructed-response attention check, scored separately (pass = 5).
  * distrust_subscore = mean of items 1-5 as answered (NOT reversed): higher =
    more distrust.   trust_subscore = mean of items 6-11: higher = more trust.
  * perceived_bias = mean of the 3 bias items after reversing item 2 (a fairness
    statement), higher = more perceived bias.
A composite is left blank unless every item that goes into it was answered with
a whole number 1-7; nothing is imputed.
"""

import hashlib
from datetime import datetime

import config

LIKERT_MIN, LIKERT_MAX = 1, 7
DISTRUST_ITEMS = (1, 2, 3, 4, 5)
TRUST_SUBSCALE_ITEMS = (6, 7, 8, 9, 10, 11)
FAMILIARITY_ITEM = 12
BIAS_REVERSED = (2,)

COLUMNS = (
    ["participant_id", "is_test", "stance", "condition", "verdict_id", "verdict_source",
     "verdict_attempts", "verdict_word_count"]
    + [f"trust_{i}" for i in range(1, 13)] + ["attention_check_item13"]
    + ["trust_composite", "distrust_subscore", "trust_subscore", "perceived_bias",
       "pre_confidence", "post_confidence", "completion_time_s", "verdict_wait_s", "total_time_s",
       "attention_check_pass", "completed"]
)


def _valid(x):
    return type(x) is int and LIKERT_MIN <= x <= LIKERT_MAX


def _mean(values):
    return round(sum(values) / len(values), 4) if values and all(_valid(v) for v in values) else None


def _rev(x):
    return LIKERT_MIN + LIKERT_MAX - x if _valid(x) else None


def score(p):
    """Scored variables from one participant row (dict)."""
    t = {i: p.get(f"trust_{i}") for i in range(1, 13)}
    composite_items = [_rev(t[i]) for i in DISTRUST_ITEMS] + [t[i] for i in TRUST_SUBSCALE_ITEMS]
    bias = [p.get(f"bias_{i}") for i in (1, 2, 3)]
    bias_items = [_rev(b) if i in BIAS_REVERSED else b for i, b in zip((1, 2, 3), bias)]
    return {
        "trust_composite": _mean(composite_items),
        "distrust_subscore": _mean([t[i] for i in DISTRUST_ITEMS]),
        "trust_subscore": _mean([t[i] for i in TRUST_SUBSCALE_ITEMS]),
        "perceived_bias": _mean(bias_items),
    }


def attention_pass(p):
    v = p.get("attention_check_response")
    return None if v is None else int(v == config.ATTENTION_CHECK_CORRECT_VALUE)


def _ts(x):
    try:
        return datetime.fromisoformat(x) if x else None
    except ValueError:
        return None


def timing(p):
    """total_time_s: consent -> debrief submitted. verdict_wait_s: from asking
    for the verdict to receiving it (the AI call, up to the 90 s budget).
    completion_time_s = total - wait, i.e. the participant's own active time,
    so a slow or retried API call can't make a careful participant look like
    a fast one (or the reverse). Blank when a needed timestamp is missing."""
    start, end = _ts(p.get("consent_timestamp")), _ts(p.get("completed_at"))
    asked, got = _ts(p.get("verdict_requested_at")), _ts(p.get("ai_verdict_generated_at"))
    total = round((end - start).total_seconds(), 1) if start and end else None
    wait = round(max((got - asked).total_seconds(), 0), 1) if asked and got else None
    active = round(total - wait, 1) if total is not None and wait is not None else None
    return {"total_time_s": total, "verdict_wait_s": wait, "completion_time_s": active}


def verdict_identity(p):
    """The verdicts are generated live, so there is no bank id. verdict_id is
    'fallback-<stance>-<condition>' for the four stored fallbacks and
    'live-<first 10 hex of sha256(text)>' otherwise: identical text -> identical id."""
    text = p.get("ai_verdict_text")
    if not text:
        return None, None
    if p.get("verdict_fallback_used"):
        return f"fallback-{p.get('pre_stance')}-{p.get('condition')}", "fallback"
    return "live-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:10], "live"


def analysis_row(p):
    vid, source = verdict_identity(p)
    row = {
        "participant_id": p["id"], "is_test": p.get("is_test"), "stance": p.get("pre_stance"),
        "condition": p.get("condition"), "verdict_id": vid, "verdict_source": source,
        "verdict_attempts": p.get("verdict_attempts"), "verdict_word_count": p.get("ai_verdict_word_count"),
        "attention_check_item13": p.get("attention_check_response"),
        "pre_confidence": p.get("pre_confidence"), "post_confidence": p.get("post_confidence"),
        "attention_check_pass": attention_pass(p), "completed": int(bool(p.get("completed_at"))),
    }
    for i in range(1, 13):
        row[f"trust_{i}"] = p.get(f"trust_{i}")
    row.update(score(p))
    row.update(timing(p))
    return {c: row.get(c) for c in COLUMNS}
