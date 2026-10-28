"""
Flask app — one page (templates/index.html) that drives itself through the
study steps with JS, talking to these JSON API routes. All actual state
lives in SQLite (db.py); nothing here is trusted from the client except
where explicitly noted.
"""

import csv
import io
import json
import os
import re
import secrets
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()  # must run before `import config` — config.py reads env vars at import time

from flask import Flask, jsonify, render_template, request, Response
import openai
from openai import OpenAI

import db
import config
import verdicts
from randomization import assign_condition

app = Flask(__name__)

# No db.init_db() call here at import time — see the comment above
# get_db() in db.py for why. Schema creation happens lazily on first use.

ADMIN_EXPORT_PASSWORD = os.environ.get("ADMIN_EXPORT_PASSWORD")

# --- test mode ---------------------------------------------------------------
# A test/soak run is only possible when BOTH hold: the caller presents
# TEST_MODE_TOKEN (an env var you set on the host; unset = test mode is
# disabled entirely) AND its participant id starts with "test-" (an id
# namespace real browsers never produce -- they generate bare UUIDs). The
# resulting is_test flag is written once at consent and is immutable at the
# database level (db.py TRIGGERS). Every mismatch is refused outright rather
# than quietly falling back to "real", so a tester who forgets the token, or
# mistypes it, gets an error instead of polluting the real data.
TEST_MODE_TOKEN = os.environ.get("TEST_MODE_TOKEN")
TEST_ID_PREFIX = "test-"
# What a real browser generates (crypto.randomUUID() is always a v4 UUID).
# Requiring real ids to look like this means an ad hoc script that forgets to
# use test mode (ids like "pilot6-...") is refused instead of being stored as
# real data. It can't stop someone deliberately forging a UUID; it stops the
# accident.
REAL_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def is_valid_test_token(supplied):
    return (
        bool(TEST_MODE_TOKEN)
        and isinstance(supplied, str)
        and secrets.compare_digest(supplied.encode(), TEST_MODE_TOKEN.encode())
    )


def classify_run(participant_id, supplied_token):
    """Returns (is_test, error). is_test is 1 or 0 when error is None."""
    if not isinstance(participant_id, str) or not participant_id:
        return None, "invalid participant id"
    has_prefix = participant_id.startswith(TEST_ID_PREFIX)
    if supplied_token is None:
        if has_prefix:
            return None, "participant ids starting with 'test-' require a valid test token"
        if not REAL_ID_RE.match(participant_id):
            return None, (
                "real participant ids must be UUIDs; scripted or test runs "
                "must use test mode (a 'test-' id plus the test token)"
            )
        return 0, None
    if not is_valid_test_token(supplied_token):
        return None, "invalid test token"
    if not has_prefix:
        return None, "a test token requires a participant id starting with 'test-'"
    return 1, None

# Switched from OpenRouter to Gemini's free tier — OpenRouter has no
# durable free tier (a negative account balance blocked every request
# mid-pilot). Google's Gemini API has an actual free tier, and exposes an
# OpenAI-compatible endpoint, so this still goes through the same openai
# package's client, just pointed at Google's base_url with a Gemini model
# name and a Gemini API key instead of an OpenRouter one.
#
# Model name confirmed live against a real key: "gemini-2.0-flash" (my
# original guess, written with no working web access) came back
# deprecated — "This model models/gemini-2.0-flash is no longer
# available... use models/gemini-3.6-flash" — straight from Google's own
# API error, not guessed. If this drifts again, check
# https://ai.google.dev/gemini-api/docs/models or just read the error;
# Google's 404 messages name the current replacement directly.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
AI_MODEL = os.environ.get("AI_MODEL", "gemini-3.6-flash")

ai_client = (
    OpenAI(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key=GEMINI_API_KEY,
        # The openai SDK retries automatically on 5xx by default
        # (max_retries=2, i.e. up to 3 real requests per .create() call).
        # Confirmed live (2026-09-17) that this was silently multiplying
        # quota usage against Gemini's actual free-tier limits: the raw
        # RateLimitError logged that day named
        # GenerateRequestsPerMinutePerProjectPerModel-FreeTier, quotaValue
        # 5 -- a PER-MINUTE cap, not the daily one we'd assumed since
        # 2026-09-08's error (which really did say PerDay that time, with
        # a different quotaId -- both limits are real, just distinct).
        # Two "logical" calls from a pilot script both hit InternalServerError
        # (503 model-overloaded) and were enough to blow through a 5/minute
        # cap, which only makes sense if the SDK was silently resending
        # each one 2-3x internally before surfacing the error. Disabling
        # that here: our own RateLimitError/InternalServerError handlers
        # already give participants a clean message and a manual retry
        # button, so a human-paced retry is far less likely to rapid-fire
        # multiple real requests within the same 60s window than the
        # SDK's built-in backoff was.
        max_retries=0,
    )
    if GEMINI_API_KEY
    else None
)

# gemini-3.6-flash is a reasoning model that spends part of AI_MAX_TOKENS on
# hidden "thinking" before any visible output -- the likely cause of the
# multi-minute latencies behind several ambiguous client-side timeouts
# during pilot testing (2026-09-07/08). Attempting to cap that budget via
# Google's documented thinking_config passthrough (unverified against this
# specific model as of this deploy -- WebSearch/WebFetch were both down
# when this was written, so this could not be checked live before
# shipping). NOT worth guessing wrong and eating an extra RateLimitError
# on every call against a 20/day budget: if the API rejects this shape,
# api_generate_verdict() falls back once and then stops trying it for the
# rest of this worker's process lifetime, so a wrong guess costs at most
# one wasted call, not one per call. Confirm this actually helped (or
# revert it) after the next real pilot run.
_thinking_config_supported = True


def now():
    return datetime.now(timezone.utc).isoformat()


# --- page -----------------------------------------------------------

@app.route("/")
def index():
    src = request.args.get("src", "")

    # ?test=<token> opens a flagged test session. If the parameter is present
    # but the token is wrong (or test mode isn't configured), refuse -- do NOT
    # fall through to the normal page, which would record the session as
    # real participant data while the tester believes it's a test.
    test_arg = request.args.get("test")
    test_mode = test_arg is not None
    if test_mode and not is_valid_test_token(test_arg):
        return Response(
            "Refused: invalid or unconfigured test token. Opening the study "
            "without it would record this session as REAL participant data, "
            "so the page was not served.",
            status=403,
            mimetype="text/plain",
        )

    return render_template(
        "index.html",
        test_mode=test_mode,
        test_token=test_arg if test_mode else None,
        recruitment_source=src,
        consent_text=config.CONSENT_TEXT,
        consent_checkbox_label=config.CONSENT_CHECKBOX_LABEL,
        minimum_age=config.MINIMUM_AGE,
        scenario_text=config.SCENARIO_TEXT,
        stance_question_pre=config.STANCE_QUESTION_PRE,
        stance_question_post=config.STANCE_QUESTION_POST,
        debrief_text=config.DEBRIEF_TEXT,
        ai_use_question=config.AI_USE_QUESTION,
        ai_use_labels=config.AI_USE_LABELS,
        tech_affinity_items=config.TECH_AFFINITY_ITEMS,
        fin_lit_questions=config.FIN_LIT_QUESTIONS,
        gender_question=config.GENDER_QUESTION,
        gender_options=config.GENDER_OPTIONS,
        trust_instructions=config.TRUST_INSTRUCTIONS,
        trust_items=config.TRUST_ITEMS,
        competence_items=config.COMPETENCE_ITEMS,
        bias_items=config.BIAS_ITEMS,
        intention_items=config.INTENTION_ITEMS,
        attention_check_item=config.ATTENTION_CHECK_ITEM,
    )


# --- Step 0: consent --------------------------------------------------

@app.route("/api/consent", methods=["POST"])
def api_consent():
    data = request.get_json()
    participant_id = data["participant_id"]
    recruitment_source = data.get("recruitment_source", "")
    is_test, error = classify_run(participant_id, data.get("test_token"))
    if error:
        return jsonify({"error": error}), 400
    db.save_consent(participant_id, recruitment_source, now(), is_test)
    # Echoed so scripted clients can assert the flag they got is the one they meant.
    return jsonify({"ok": True, "is_test": bool(is_test)})


# --- Step 1: covariates ------------------------------------------------

@app.route("/api/covariates", methods=["POST"])
def api_covariates():
    data = request.get_json()
    # Closed list: reject anything the form couldn't have produced instead
    # of storing it. (A missing value is rejected too -- the UI requires it.)
    if data.get("gender") not in {code for code, _ in config.GENDER_OPTIONS}:
        return jsonify({"error": "invalid gender"}), 400
    db.save_covariates(
        participant_id=data["participant_id"],
        ai_use_frequency=data["ai_use_frequency"],
        tech_affinity=data["tech_affinity"],          # list of 3
        fin_lit_correct=data["fin_lit_correct"],       # list of 3 booleans
        fin_lit_self_rating=data["fin_lit_self_rating"],
        age=data["age"],
        grade_year=data["grade_year"],
        gender=data["gender"],
    )
    return jsonify({"ok": True})


# --- Step 2 + 3: stance submission and RANDOMIZATION --------------------
#
# This is the causal backbone of the study. Read randomization.py first.
#
# Ordering matters and is deliberate: the participant_id is just an
# identifier (not participant-authored content), so reading it is fine.
# But the random draw and its write to storage happen BEFORE the request
# body's stance/rationale content is parsed and stored — so nothing about
# what the participant wrote can influence, or even be involved in, the
# assignment.

@app.route("/api/submit-stance", methods=["POST"])
def api_submit_stance():
    participant_id = request.headers.get("X-Participant-Id")
    if not participant_id:
        # Without this, a missing/stripped header would silently no-op
        # both the condition write and the stance write below (UPDATE
        # ... WHERE id = NULL matches zero rows but raises nothing), and
        # the participant would only find out one step later, when
        # generate-verdict's "no condition assigned" check finally
        # catches it.
        return jsonify({"error": "missing participant id"}), 400

    # --- RANDOMIZATION: happens first, touches nothing else yet ---
    condition = assign_condition()
    db.log_condition(participant_id, condition, now())
    # --- end randomization ---

    data = request.get_json()  # only now do we read the participant's answer
    stance = data["stance"]
    confidence = data["confidence"]
    rationale = data["rationale"]
    word_count = len(rationale.split())
    db.save_pre_stance(participant_id, stance, confidence, rationale, word_count)

    return jsonify({"ok": True})


# --- Step 4: AI verdict (v3 — the model is told the target verdict, then the
# result is verified; see config.py's comment above VERDICT_PROMPT_TEMPLATE
# and verdicts.py) ---------------------------------------------------------

def scenario_facts_for_condition(condition, pre_stance):
    """Which fact-set the model is given to write the verdict from. The
    verdict's DIRECTION is set by the condition (the model is told the target
    verdict); this chooses the material the prose can cite, so it has real
    facts to draw on whichever way it must go.

    The condition is the randomized part and is already fixed before this
    runs (drawn and stored in /api/submit-stance before any participant-
    authored content is read; nothing here can influence it). The
    participant's own pre-stance is used ONLY to implement that
    already-assigned condition:
      'agree'    -> the set that supports their stance
      'disagree' -> the set that opposes it
    "yes" is backed by the growth-case facts and "no" by the risk-case
    facts. See the comment above SCENARIO_FACTS_GROWTH in config.py for
    why the stance is part of the mapping."""
    if condition not in ("agree", "disagree"):
        raise ValueError(f"unexpected condition: {condition!r}")
    if pre_stance not in ("yes", "no"):
        raise ValueError(f"unexpected pre_stance: {pre_stance!r}")

    supports = config.SCENARIO_FACTS_GROWTH if pre_stance == "yes" else config.SCENARIO_FACTS_RISK
    opposes = config.SCENARIO_FACTS_RISK if pre_stance == "yes" else config.SCENARIO_FACTS_GROWTH
    return supports if condition == "agree" else opposes


def build_verdict_prompt(scenario_facts, participant_reasoning, target_verdict):
    """Pure string-building, kept separate from the API call so it's easy
    to read/test on its own."""
    return config.VERDICT_PROMPT_TEMPLATE.format(
        target_verdict=target_verdict,
        min_words=config.VERDICT_WORD_MIN,
        max_words=config.VERDICT_WORD_MAX,
        scenario_facts=scenario_facts.strip(),
        participant_reasoning=participant_reasoning,
    )


def _classify_api_error(exc):
    """Map an openai SDK exception onto verdicts.ModelAPIError kinds."""
    text = str(exc)
    if isinstance(exc, openai.RateLimitError):
        # Two real, different Gemini quotas (confirmed from raw errors): the
        # per-day cap won't clear today, the per-minute one clears shortly.
        return verdicts.ModelAPIError("daily_quota" if "PerDay" in text else "rate_limited", text)
    if isinstance(exc, openai.InternalServerError):
        return verdicts.ModelAPIError("overloaded", text)
    if isinstance(exc, openai.APITimeoutError):
        return verdicts.ModelAPIError("timeout", text)
    if isinstance(exc, openai.APIConnectionError):
        return verdicts.ModelAPIError("connection", text)
    if isinstance(exc, openai.APIStatusError):  # 400/401/403/404...: retrying can't help
        return verdicts.ModelAPIError("bad_request", f"HTTP {exc.status_code}: {text}")
    return verdicts.ModelAPIError("other", text)


def call_model(prompt, timeout):
    """One model call. Returns (raw_text, model_name); raises verdicts.ModelAPIError.
    No retries here and the SDK's own are off (max_retries=0): the loop in
    verdicts.py decides what to retry, so every real request is counted."""
    global _thinking_config_supported
    client = ai_client.with_options(timeout=timeout)
    create_kwargs = dict(
        model=AI_MODEL,
        max_tokens=config.AI_MAX_TOKENS,
        temperature=config.AI_TEMPERATURE,
        # A system-only message array is rejected by Gemini's OpenAI-compat
        # layer ("contents is not specified"); one user message works everywhere.
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        if _thinking_config_supported:
            try:
                response = client.chat.completions.create(
                    **create_kwargs,
                    extra_body={"extra_body": {"google": {"thinking_config": {
                        "thinking_budget": 1024, "include_thoughts": False}}}},
                )
            except openai.BadRequestError:
                # Unverified parameter shape rejected: stop sending it for the
                # rest of this worker's life and retry once without it.
                _thinking_config_supported = False
                app.logger.warning("thinking_config extra_body rejected; falling back to the default request shape.")
                response = client.chat.completions.create(**create_kwargs)
        else:
            response = client.chat.completions.create(**create_kwargs)
    except openai.OpenAIError as e:
        app.logger.warning(f"model call failed: {type(e).__name__}: {str(e)[:300]}")
        raise _classify_api_error(e)
    content = response.choices[0].message.content if response.choices else None
    return content or "", getattr(response, "model", None)


@app.route("/api/generate-verdict", methods=["POST"])
def api_generate_verdict():
    data = request.get_json()
    participant_id = data["participant_id"]

    # Read condition/stance/rationale from storage, not from the request, so
    # the generation always reflects what was actually recorded (and drawn).
    participant = db.get_participant(participant_id)
    if participant is None or participant["condition"] is None:
        return jsonify({"error": "no condition assigned for this participant"}), 400
    if participant["pre_stance"] not in ("yes", "no"):
        return jsonify({"error": "no stance recorded for this participant"}), 400

    # Idempotent: a verdict that was already served is returned as stored and
    # NEVER regenerated, so a reload, a double-click or a second tab can't
    # re-roll the stimulus (or burn quota).
    if participant["ai_verdict_text"]:
        return jsonify({"verdict": participant["ai_verdict_text"]})

    if ai_client is None:
        # A deployment fault, not a participant-level failure: fail loudly
        # rather than quietly serving every participant the fallback.
        return jsonify({"error": "GEMINI_API_KEY not configured on the server"}), 500

    condition, stance = participant["condition"], participant["pre_stance"]
    target = verdicts.target_for(condition, config.TARGET_VERDICT)
    prompt_builder = lambda: build_verdict_prompt(
        scenario_facts_for_condition(condition, stance), participant["pre_rationale"], target)

    result = verdicts.run_loop(
        target=target,
        build_prompt=prompt_builder,
        call_model=call_model,
        max_attempts=config.MAX_VERDICT_ATTEMPTS,
        budget_s=config.VERDICT_TIME_BUDGET_S,
        temperature=config.AI_TEMPERATURE,
        on_record=lambda rec: db.log_verdict_attempt(participant_id, rec, now()),
    )

    if result["accepted"]:
        text, word_count, fallback_used, fallback_reason = result["prose"], result["word_count"], False, None
    else:
        text = config.FALLBACK_VERDICTS.get((stance, condition))
        if not text:
            app.logger.error(f"fallback needed for {stance}/{condition} ({result['reason']}) but none is configured")
            return jsonify({
                "error": "fallback_unavailable",
                "message": "We couldn't generate a response just now. Please try again in a moment, or contact the researcher.",
            }), 503
        word_count, fallback_used, fallback_reason = len(text.split()), True, result["reason"]

    stored = db.save_verdict_result(
        participant_id, text=text, word_count=word_count, target=target,
        attempts=result["generations"], fallback_used=fallback_used, fallback_reason=fallback_reason,
        fields_json=json.dumps(result["fields"]), api_error_count=result["api_errors"], generated_at=now(),
    )
    if not stored:  # another request finished first; serve what's on record
        text = db.get_participant(participant_id)["ai_verdict_text"]
    return jsonify({"verdict": text})


# --- Step 6: post-verdict measures ---------------------------------------

@app.route("/api/post-verdict", methods=["POST"])
def api_post_verdict():
    data = request.get_json()
    db.save_post_verdict(
        participant_id=data["participant_id"],
        stance=data["stance"],
        confidence=data["confidence"],
        rationale=data["rationale"],
        trust_items=data["trust_items"],
        competence_items=data["competence_items"],
        bias_items=data["bias_items"],
        intention_items=data["intention_items"],
        attention_check_response=data["attention_check_response"],
    )
    return jsonify({"ok": True})


# --- Step 7: debrief feedback (optional) ----------------------------------

@app.route("/api/debrief-feedback", methods=["POST"])
def api_debrief_feedback():
    data = request.get_json()
    db.save_debrief_feedback(data["participant_id"], data.get("feedback", ""), now())
    return jsonify({"ok": True})


# --- Admin export ----------------------------------------------------------

@app.route("/admin/export")
def admin_export():
    supplied = request.args.get("password", "")
    if not ADMIN_EXPORT_PASSWORD or not secrets.compare_digest(supplied, ADMIN_EXPORT_PASSWORD):
        return "Unauthorized", 401

    # Real participants only by default; ?include_test=1 adds test rows and
    # legacy (unflagged) rows, each identifiable from the is_test column.
    rows = db.all_participants(include_test=request.args.get("include_test") == "1")
    columns = db.column_names()

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({col: row[col] for col in columns})

    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=study_export.csv"},
    )


# --- TEMPORARY: one-off pilot-data cleanup ---------------------------------
# Two-step cleanup down to just the legit pilot-audit-batch-3 data:
# 1) deletes every row from any other/no recruitment_source (earlier
#    test/pilot batches, individual verification rows), then
# 2) within what's left, deletes rows that never got a verdict (a
#    client-side timeout, a 429/503 mid-run, an abandoned session) --
#    the "null responses", not real audit samples.
# Remove this route (and delete_participants_except() /
# delete_incomplete_participants() in db.py) once you've run it once —
# scoped to this specific cleanup, not a standing bulk-delete capability
# meant to be left in a live app.
@app.route("/admin/cleanup", methods=["POST"])
def admin_cleanup():
    supplied = request.args.get("password", "")
    if not ADMIN_EXPORT_PASSWORD or not secrets.compare_digest(supplied, ADMIN_EXPORT_PASSWORD):
        return "Unauthorized", 401

    deleted_other_batches = db.delete_participants_except("pilot-audit-batch-3")
    deleted_incomplete = db.delete_incomplete_participants("pilot-audit-batch-3")
    return jsonify({
        "deleted_other_batches": deleted_other_batches,
        "deleted_incomplete_pilot_rows": deleted_incomplete,
        "total_deleted": deleted_other_batches + deleted_incomplete,
    })


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5050)))
