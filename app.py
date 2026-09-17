"""
Flask app — one page (templates/index.html) that drives itself through the
study steps with JS, talking to these JSON API routes. All actual state
lives in SQLite (db.py); nothing here is trusted from the client except
where explicitly noted.
"""

import csv
import io
import os
import secrets
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()  # must run before `import config` — config.py reads env vars at import time

from flask import Flask, jsonify, render_template, request, Response
import openai
from openai import OpenAI

import db
import config
from randomization import assign_condition

app = Flask(__name__)

# No db.init_db() call here at import time — see the comment above
# get_db() in db.py for why. Schema creation happens lazily on first use.

ADMIN_EXPORT_PASSWORD = os.environ.get("ADMIN_EXPORT_PASSWORD")

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
    return render_template(
        "index.html",
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
    db.save_consent(participant_id, recruitment_source, now())
    return jsonify({"ok": True})


# --- Step 1: covariates ------------------------------------------------

@app.route("/api/covariates", methods=["POST"])
def api_covariates():
    data = request.get_json()
    db.save_covariates(
        participant_id=data["participant_id"],
        ai_use_frequency=data["ai_use_frequency"],
        tech_affinity=data["tech_affinity"],          # list of 3
        fin_lit_correct=data["fin_lit_correct"],       # list of 3 booleans
        fin_lit_self_rating=data["fin_lit_self_rating"],
        age=data["age"],
        grade_year=data["grade_year"],
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


# --- Step 4: AI verdict (v2 — honest audit against a condition-selected
# fact-set; see config.py's comment above AI_PROMPT_TEMPLATE) -------------

def scenario_facts_for_condition(condition):
    """The random condition draw (randomization.py, via the DB) selects
    WHICH fact-set the participant's reasoning is audited against — that
    selection is the actual experimental manipulation now, since the
    model is never told what verdict to reach. Never derived from
    anything participant-authored."""
    return config.SCENARIO_FACTS_AGREE if condition == "agree" else config.SCENARIO_FACTS_DISAGREE


def build_verdict_prompt(scenario_facts, participant_reasoning):
    """Pure string-building, kept separate from the API call so it's easy
    to read/test on its own."""
    return config.AI_PROMPT_TEMPLATE.format(
        scenario_facts=scenario_facts.strip(),
        participant_reasoning=participant_reasoning,
    )


@app.route("/api/generate-verdict", methods=["POST"])
def api_generate_verdict():
    data = request.get_json()
    participant_id = data["participant_id"]

    # Read condition/rationale from storage, not from the request, so the
    # AI call always reflects what was actually recorded (and randomized)
    # rather than whatever the client happens to send.
    participant = db.get_participant(participant_id)
    if participant is None or participant["condition"] is None:
        return jsonify({"error": "no condition assigned for this participant"}), 400

    scenario_facts = scenario_facts_for_condition(participant["condition"])
    prompt = build_verdict_prompt(
        scenario_facts=scenario_facts,
        participant_reasoning=participant["pre_rationale"],
    )

    if ai_client is None:
        return jsonify({"error": "GEMINI_API_KEY not configured on the server"}), 500

    # A system-only message array (no "user" turn) worked fine against
    # OpenRouter/Claude, but Gemini's OpenAI-compat layer rejects it with
    # "GenerateContentRequest.contents is not specified" — its system role
    # maps to a separate field, not to `contents`, so `contents` ends up
    # empty with nothing else in the array. Sending everything as a single
    # "user" message works universally across providers, which matters
    # now that we're on our second provider swap.
    global _thinking_config_supported
    create_kwargs = dict(
        model=AI_MODEL,
        max_tokens=config.AI_MAX_TOKENS,
        temperature=config.AI_TEMPERATURE,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        if _thinking_config_supported:
            try:
                response = ai_client.chat.completions.create(
                    **create_kwargs,
                    extra_body={
                        "extra_body": {
                            "google": {
                                "thinking_config": {
                                    "thinking_budget": 1024,
                                    "include_thoughts": False,
                                }
                            }
                        }
                    },
                )
            except openai.BadRequestError:
                # Unverified parameter shape was rejected -- remember that
                # for the rest of this worker's lifetime so we don't pay
                # for this mistake on every subsequent call, then fall
                # back to the known-working request for this one.
                _thinking_config_supported = False
                app.logger.warning(
                    "thinking_config extra_body rejected by the API -- "
                    "falling back to the default request shape for the "
                    "rest of this worker's lifetime."
                )
                response = ai_client.chat.completions.create(**create_kwargs)
        else:
            response = ai_client.chat.completions.create(**create_kwargs)
    except openai.RateLimitError as e:
        # RateLimitError (429) covers two genuinely different Gemini
        # quotas, confirmed live from the raw error's quotaId in each
        # case: GenerateRequestsPerDayPerProjectPerModel-FreeTier
        # (2026-09-08, limit 20 -- resolves next day) vs.
        # GenerateRequestsPerMinutePerProjectPerModel-FreeTier
        # (2026-09-17, limit 5 -- resolves within about a minute). This
        # handler used to unconditionally tell participants "try again
        # tomorrow" for both, which is actively wrong for the per-minute
        # case. Distinguishing on the quotaId string in the raw message
        # rather than guessing from timing.
        error_text = str(e)
        app.logger.warning(f"RateLimitError on /api/generate-verdict: {error_text}")
        if "PerMinute" in error_text:
            return jsonify({
                "error": "rate_limited",
                "message": (
                    "The AI service is handling a lot of requests right now. "
                    "Please wait about a minute and try again."
                ),
            }), 429
        return jsonify({
            "error": "daily_limit_reached",
            "message": (
                "This study has reached its response limit for today. "
                "Please try again tomorrow, or contact the researcher."
            ),
        }), 429
    except openai.InternalServerError:
        # Confirmed live in Render's logs (2026-09-07): Gemini returns a
        # distinct 503 "This model is currently experiencing high demand"
        # under load -- a different exception class from RateLimitError,
        # so it wasn't caught here before and fell through as a raw HTML
        # 500 (unparseable by app.js's postJSON, which only extracts a
        # JSON error body). The retry button already handles this fine
        # once it gets a real message -- this is transient per Google's
        # own wording, unlike the daily quota -- so give it one instead of
        # an opaque server error page.
        return jsonify({
            "error": "model_overloaded",
            "message": (
                "The AI service is temporarily overloaded. Please try "
                "again in a moment."
            ),
        }), 503
    verdict_text = response.choices[0].message.content

    db.save_ai_verdict(participant_id, verdict_text, now())
    return jsonify({"verdict": verdict_text})


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

    rows = db.all_participants()
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
