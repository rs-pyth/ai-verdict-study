"""
Verdict generation + verification loop.

Pure logic: no Flask, no database, no network. app.py supplies the model call
(`call_model`) and persistence (`on_record`), which is what lets the whole
loop be tested with a fake model and a fake clock.

What the loop guarantees:
  * A generation is ACCEPTED only if the model's `verdict` field equals the
    target for the assigned condition AND the prose's mandatory first sentence
    agrees with that field. Anything else is a failed attempt.
  * At most `max_attempts` GENERATIONS are tried. API errors (503, 429,
    timeouts, ...) are recorded but do NOT count as attempts.
  * The whole loop stays inside `budget_s` seconds of wall-clock time.
  * It never raises for model/API trouble: it returns a result that says why
    it stopped (`reason`), and the caller serves the stored fallback.
  * Every call -- generation or API error -- produces a record via `on_record`
    the moment it finishes, so a crash mid-loop still leaves a trace.
"""

import json
import re
import time

SUPPORTED = "supported"
NOT_SUPPORTED = "not_supported"
VALID_VERDICTS = (SUPPORTED, NOT_SUPPORTED)

# The first sentence is mandatory in the prompt, so the prose itself states the
# verdict; comparing it with the JSON field catches a self-contradictory answer
# without spending another API call.
_OPENER_RE = re.compile(r"^The participant's reasoning is (not supported|supported) by the facts\.")
_OPENER_TO_FIELD = {"supported": SUPPORTED, "not supported": NOT_SUPPORTED}
_MARKDOWN_RE = re.compile(r"[*#`]|^\s*[-•]\s", re.MULTILINE)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

# Seconds to wait before the next call after a retryable API error.
BACKOFF_S = {"overloaded": 3, "rate_limited": 20, "timeout": 2, "connection": 3, "other": 3}
# Don't start a call that has less than this long to finish.
MIN_CALL_S = 5


class ModelAPIError(Exception):
    """A failure of the model API itself (not a bad answer).

    kind: overloaded | rate_limited | daily_quota | bad_request | timeout |
          connection | other.  daily_quota and bad_request are fatal: retrying
          cannot help, so the loop stops at once and the caller falls back.
    """

    def __init__(self, kind, detail=""):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = str(detail)[:500]

    @property
    def fatal(self):
        return self.kind in ("daily_quota", "bad_request")


def target_for(condition, mapping):
    try:
        return mapping[condition]
    except KeyError:
        raise ValueError(f"no target verdict for condition {condition!r}")


def parse_generation(raw_text):
    """Parse one model answer.

    Returns dict(outcome, verdict_field, prose, word_count, problem) where
    outcome is 'ok', 'malformed' or 'inconsistent'. 'ok' still has to be
    compared with the target by the caller.
    """
    out = {"outcome": "malformed", "verdict_field": None, "prose": None, "word_count": None, "problem": None}
    text = _FENCE_RE.sub("", (raw_text or "").strip()).strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        out["problem"] = "not valid JSON"
        return out
    if not isinstance(data, dict) or set(data) != {"verdict", "prose"}:
        out["problem"] = "JSON is not exactly {verdict, prose}"
        return out

    field = data["verdict"]
    out["verdict_field"] = field if isinstance(field, str) else None
    if field not in VALID_VERDICTS:
        out["problem"] = f"verdict field is not one of {VALID_VERDICTS}"
        return out
    prose = data["prose"]
    if not isinstance(prose, str) or not prose.strip():
        out["problem"] = "prose is empty or not a string"
        return out
    prose = " ".join(prose.split())  # plain text, no line breaks
    out["prose"] = prose
    out["word_count"] = len(prose.split())
    if _MARKDOWN_RE.search(prose):
        out["problem"] = "prose contains markdown"
        return out

    opener = _OPENER_RE.match(prose)
    if not opener:
        out["outcome"] = "inconsistent"
        out["problem"] = "prose does not start with the mandatory verdict sentence"
        return out
    if _OPENER_TO_FIELD[opener.group(1)] != field:
        out["outcome"] = "inconsistent"
        out["problem"] = "verdict field disagrees with the prose's first sentence"
        return out

    out["outcome"] = "ok"
    return out


def run_loop(*, target, build_prompt, call_model, max_attempts, budget_s, temperature=None,
             clock=time.monotonic, sleep=time.sleep, on_record=None):
    """Generate until a generation matches `target`, or give up.

    call_model(prompt, timeout) -> (raw_text, model_name); raises ModelAPIError.
    Returns dict: accepted, prose, verdict_field, word_count, generations,
    api_errors, fields (verdict field of each generation, in order), reason
    (None if accepted; else verification_failed | time_budget | daily_quota |
    api_error_fatal), records, elapsed_s.
    """
    start = clock()
    prompt = build_prompt()
    records, fields = [], []
    generations = api_errors = seq = 0
    reason = None

    def record(rec):
        records.append(rec)
        if on_record:
            on_record(rec)

    while True:
        if generations >= max_attempts:
            reason = "verification_failed"
            break
        remaining = budget_s - (clock() - start)
        if remaining < MIN_CALL_S:
            reason = "time_budget"
            break

        seq += 1
        t0 = clock()
        try:
            raw, model = call_model(prompt, timeout=remaining)
        except ModelAPIError as e:
            api_errors += 1
            record({
                "seq": seq, "kind": "api_error", "generation_no": None, "outcome": "api_error",
                "verdict_field": None, "prose": None, "word_count": None, "model": None,
                "temperature": temperature, "latency_ms": int((clock() - t0) * 1000),
                "error_kind": e.kind, "error_detail": e.detail,
            })
            if e.fatal:
                reason = "daily_quota" if e.kind == "daily_quota" else "api_error_fatal"
                break
            wait = min(BACKOFF_S.get(e.kind, 3), budget_s - (clock() - start) - MIN_CALL_S)
            if wait > 0:
                sleep(wait)
            continue

        generations += 1
        parsed = parse_generation(raw)
        outcome = parsed["outcome"]
        if outcome == "ok":
            outcome = "match" if parsed["verdict_field"] == target else "mismatch"
        fields.append(parsed["verdict_field"])
        record({
            "seq": seq, "kind": "generation", "generation_no": generations, "outcome": outcome,
            "verdict_field": parsed["verdict_field"], "prose": parsed["prose"],
            "word_count": parsed["word_count"], "model": model, "temperature": temperature,
            "latency_ms": int((clock() - t0) * 1000), "error_kind": None,
            "error_detail": parsed["problem"],
        })
        if outcome == "match":
            return {
                "accepted": True, "prose": parsed["prose"], "verdict_field": parsed["verdict_field"],
                "word_count": parsed["word_count"], "generations": generations, "api_errors": api_errors,
                "fields": fields, "reason": None, "records": records, "elapsed_s": round(clock() - start, 1),
            }

    return {
        "accepted": False, "prose": None, "verdict_field": None, "word_count": None,
        "generations": generations, "api_errors": api_errors, "fields": fields,
        "reason": reason, "records": records, "elapsed_s": round(clock() - start, 1),
    }


def validate_fallbacks(fallbacks, mapping, word_min, word_max):
    """Problems with the stored fallback texts, as a list of strings (empty = OK)."""
    problems = []
    for (stance, condition), text in sorted(fallbacks.items()):
        name = f"{stance}/{condition}"
        if not text:
            problems.append(f"{name}: not written yet")
            continue
        parsed = parse_generation(json.dumps({"verdict": mapping[condition], "prose": text}))
        if parsed["outcome"] != "ok":
            problems.append(f"{name}: {parsed['problem']} (it must start with the exact verdict sentence for '{mapping[condition]}')")
        n = len(text.split())
        if not word_min <= n <= word_max:
            problems.append(f"{name}: {n} words, needs {word_min}-{word_max}")
        if "\n" in text:
            problems.append(f"{name}: contains line breaks")
    if len({t for t in fallbacks.values() if t}) < len([t for t in fallbacks.values() if t]):
        problems.append("two fallbacks are identical")
    return problems
