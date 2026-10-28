"""
Data model — one table, one row per participant, columns added as the
participant moves through the steps.

Why one wide table instead of several linked ones: this is a fixed-length
survey (no repeating sections), so a normalized schema would just add joins
without adding correctness. One row per participant also makes the CSV
export in app.py trivial — it's the table, unmodified.

STORAGE: uses libsql (SQLite-compatible) via the `libsql` package, not the
stdlib `sqlite3` module. If TURSO_DATABASE_URL is set, every read/write
goes straight over the network to a remote Turso database — no local file
at all. That's deliberate: Render's free tier wipes local disk on every
redeploy and on spin-down after inactivity, which silently lost real
participant data before this switch. If TURSO_DATABASE_URL is unset (e.g.
local development), it falls back to a local SQLite file so you don't need
a Turso account just to run this on your laptop.

libsql's cursors return plain tuples, not sqlite3's dict-like Row objects
— see _row_to_dict() below, which reconstructs column-name access from
cursor.description so the rest of the codebase can keep doing row["column"].
"""

import os
import libsql
from contextlib import contextmanager

DB_PATH = "data/study.db"  # local fallback, only used when Turso isn't configured
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

if not TURSO_DATABASE_URL:
    # Local-file fallback only: the data/ directory is empty in the repo
    # (its only contents are the git-ignored .db file), and git doesn't
    # track empty directories — so a fresh checkout has no data/ folder
    # at all until this runs.
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
    id TEXT PRIMARY KEY,               -- random UUID, generated client-side at consent
    created_at TEXT NOT NULL,          -- when the row was first created (consent time)
    recruitment_source TEXT,           -- from ?src= URL param, e.g. 'northstar', 'classmates', 'prolific'
    is_test INTEGER,                   -- 1 = test/soak run, 0 = real participant, NULL = legacy row from before
                                        -- this flag existed (never counts as real). Set once at consent and
                                        -- IMMUTABLE -- enforced by the TRIGGERS below, not by convention.

    -- Step 0: consent
    consent_given INTEGER NOT NULL DEFAULT 0,
    consent_timestamp TEXT,

    -- Step 1: covariates
    ai_use_frequency INTEGER,          -- 1-5, "How often do you use AI tools"
    tech_affinity_1 INTEGER,           -- 1-7 Likert
    tech_affinity_2 INTEGER,
    tech_affinity_3 INTEGER,
    fin_lit_q1_correct INTEGER,        -- 0/1, objective quiz item
    fin_lit_q2_correct INTEGER,
    fin_lit_q3_correct INTEGER,
    fin_lit_self_rating INTEGER,       -- 1-7 self-rated investing knowledge
    age INTEGER,
    grade_year TEXT,                   -- free text, e.g. "11th grade"
    gender TEXT,                       -- code from config.GENDER_OPTIONS (woman/man/nonbinary/another/prefer_not_to_say);
                                        -- NULL on rows collected before this question existed

    -- Step 2: scenario + initial stance (participant-authored content)
    pre_stance TEXT,                   -- 'yes' / 'no'
    pre_confidence INTEGER,            -- 1-7, lets you compute a WOA-style
                                        -- measure against post_confidence,
                                        -- not just stance-switch rate
    pre_rationale TEXT,
    pre_rationale_word_count INTEGER,

    -- Step 3: RANDOMIZATION — see randomization.py. Logged the instant it's
    -- drawn, before pre_stance/pre_rationale above are even read by the
    -- handler. condition_assigned_at should always be <= any timestamp
    -- below it in this table.
    condition TEXT,                    -- 'agree' / 'disagree'
    condition_assigned_at TEXT,

    -- Step 4: AI verdict
    ai_verdict_text TEXT,              -- the verdict the participant saw (generated prose, or the stored fallback)
    ai_verdict_generated_at TEXT,
    ai_verdict_word_count INTEGER,     -- whitespace-split word count of ai_verdict_text
    verdict_target TEXT,               -- 'supported' / 'not_supported': what the assigned condition required
    verdict_attempts INTEGER,          -- generations tried (API errors NOT counted), 0-3
    verdict_fallback_used INTEGER,     -- 1 if the stored fallback was served instead of a verified generation
    verdict_fallback_reason TEXT,      -- verification_failed | time_budget | daily_quota | api_error_fatal
    verdict_fields TEXT,               -- JSON array: the `verdict` field returned by each generation, in order
    verdict_api_error_count INTEGER,   -- API errors (503/429/timeout...) noticed along the way
                                        -- Per-call detail lives in the verdict_attempts table.

    -- Step 6: post-verdict measures
    post_stance TEXT,
    post_confidence INTEGER,           -- 1-7
    post_rationale TEXT,

    trust_1 INTEGER, trust_2 INTEGER, trust_3 INTEGER, trust_4 INTEGER,
    trust_5 INTEGER, trust_6 INTEGER, trust_7 INTEGER, trust_8 INTEGER,
    trust_9 INTEGER, trust_10 INTEGER, trust_11 INTEGER, trust_12 INTEGER,

    competence_1 INTEGER, competence_2 INTEGER, competence_3 INTEGER,
    competence_4 INTEGER, competence_5 INTEGER,   -- item 5 optional, leave NULL if unused

    bias_1 INTEGER, bias_2 INTEGER, bias_3 INTEGER,

    intention_1 INTEGER, intention_2 INTEGER, intention_3 INTEGER,  -- item 3 optional

    -- Instructed-response attention check, embedded as item 13 in the
    -- trust grid (after the 11 composite + familiarity items) — NOT a
    -- trust item, not part of that composite. 1-7, same scale as the
    -- trust grid. Correct response is defined by
    -- config.ATTENTION_CHECK_CORRECT_VALUE ("Somewhat agree" = 5 on a
    -- Strongly Disagree(1)...Strongly Agree(7) scale). Excluded before
    -- analysis: any response other than the correct value should drop
    -- that participant's row, applied identically across both conditions.
    attention_check_response INTEGER,

    -- Step 7: debrief
    debrief_feedback TEXT,
    completed_at TEXT
);
"""

# ADD COLUMN migrations for the live Turso table, which already existed
# before this field was added — CREATE TABLE IF NOT EXISTS above is a
# no-op against an existing table, so new columns need an explicit
# migration or they'd only ever apply to a brand-new database. Each is
# run once per process (see the _schema_ready guard in get_db()) and
# tolerates already having been applied (e.g. after a redeploy) by
# catching libsql's "duplicate column name" error specifically — any
# other failure still surfaces normally rather than being swallowed.
MIGRATIONS = [
    "ALTER TABLE participants ADD COLUMN attention_check_response INTEGER",
    "ALTER TABLE participants ADD COLUMN gender TEXT",
    # Deliberately nullable with NO default: every row that exists when this
    # migration runs gets NULL ("legacy, unclassified"), which is neither
    # real nor test. A default of 0 would have silently relabeled all the
    # pre-existing synthetic pilot rows as REAL data.
    "ALTER TABLE participants ADD COLUMN is_test INTEGER",
    "ALTER TABLE participants ADD COLUMN ai_verdict_word_count INTEGER",
    "ALTER TABLE participants ADD COLUMN verdict_target TEXT",
    "ALTER TABLE participants ADD COLUMN verdict_attempts INTEGER",
    "ALTER TABLE participants ADD COLUMN verdict_fallback_used INTEGER",
    "ALTER TABLE participants ADD COLUMN verdict_fallback_reason TEXT",
    "ALTER TABLE participants ADD COLUMN verdict_fields TEXT",
    "ALTER TABLE participants ADD COLUMN verdict_api_error_count INTEGER",
]

# One row per model call made for a participant -- generations AND API errors --
# written the moment each call finishes, so a crash mid-loop still leaves a
# trace. `seq` is the call order; `generation_no` numbers only real generations
# (API errors have NULL). Rows are append-only: the trigger below refuses
# UPDATEs. (DELETE stays possible for the pre-launch test-data cleanup.)
VERDICT_ATTEMPTS_SCHEMAS = [
    """
    CREATE TABLE IF NOT EXISTS verdict_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        participant_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        kind TEXT NOT NULL,                -- 'generation' | 'api_error'
        generation_no INTEGER,
        outcome TEXT NOT NULL,             -- match | mismatch | malformed | inconsistent | api_error
        verdict_field TEXT,                -- the `verdict` the model returned (NULL if unparseable / API error)
        prose TEXT,
        word_count INTEGER,
        model TEXT,                        -- the model name the API reported, e.g. to document the exact version
        temperature REAL,
        latency_ms INTEGER,
        error_kind TEXT,                   -- API errors: overloaded | rate_limited | daily_quota | ...
        error_detail TEXT,                 -- API errors: message; generations: why it was rejected
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS verdict_attempts_participant ON verdict_attempts (participant_id)",
    """
    CREATE TRIGGER IF NOT EXISTS verdict_attempts_append_only
    BEFORE UPDATE ON verdict_attempts
    BEGIN
        SELECT RAISE(ABORT, 'verdict_attempts is append-only');
    END
    """,
]

# Database-level guarantees for is_test. Installed after the migrations
# (they reference the column), each a single CREATE TRIGGER statement and
# idempotent. "Immutable" means the database refuses the write, whoever
# or whatever (a script, a console, a bug) attempts it:
#   1. A new row must carry is_test 0 or 1, and the flag must agree with the
#      id namespace: test rows have ids starting 'test-', real rows never do.
#      So a row can't be both a test run and look like a real participant.
#   2. Once set, is_test can't change. The single permitted write is
#      classifying a legacy NULL row as a test row (NULL -> 1): legacy rows
#      can be flagged after the fact but can never be promoted to "real".
#   3. The id can't change (it would defeat rule 1).
# Deleting rows is not restricted.
TRIGGERS = [
    """
    CREATE TRIGGER IF NOT EXISTS participants_is_test_on_insert
    BEFORE INSERT ON participants
    WHEN NEW.is_test IS NULL
      OR NEW.is_test NOT IN (0, 1)
      OR (NEW.is_test = 1) != (substr(NEW.id, 1, 5) = 'test-')
    BEGIN
        SELECT RAISE(ABORT, 'is_test must be 0 or 1 and agree with the test- id prefix');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS participants_is_test_immutable
    BEFORE UPDATE OF is_test ON participants
    WHEN NOT (OLD.is_test IS NULL AND NEW.is_test = 1)
    BEGIN
        SELECT RAISE(ABORT, 'is_test is immutable (only legacy NULL -> 1 is allowed)');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS participants_id_immutable
    BEFORE UPDATE OF id ON participants
    WHEN NEW.id IS NOT OLD.id
    BEGIN
        SELECT RAISE(ABORT, 'participant id is immutable');
    END
    """,
]


# Set once per worker process, the first time get_db() actually opens a
# connection — NOT at import time. app.py used to call init_db() at
# module load, which meant every gunicorn worker's boot blocked on a
# network round-trip to Turso before the worker was considered "ready".
# On Render's free tier (throttled CPU, cold-starts from scratch after
# every spin-down), that round-trip could run long enough to hit
# gunicorn's worker timeout mid-connection, get SIGKILLed, and crash-loop
# on every subsequent boot. Deferring it to first use means a slow first
# request just pays the connection cost once (now with more timeout
# headroom — see gunicorn.conf.py) instead of taking the whole worker
# down before it can serve anything.
_schema_ready = False


@contextmanager
def get_db():
    global _schema_ready

    if TURSO_DATABASE_URL:
        conn = libsql.connect(TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
    else:
        conn = libsql.connect(DB_PATH)

    if not _schema_ready:
        conn.execute(SCHEMA)
        for migration in MIGRATIONS:
            try:
                conn.execute(migration)
            except ValueError as e:
                if "duplicate column name" not in str(e):
                    raise  # a real failure, not just "already migrated"
        for trigger in TRIGGERS:
            conn.execute(trigger)
        for statement in VERDICT_ATTEMPTS_SCHEMAS:
            conn.execute(statement)
        conn.commit()
        _schema_ready = True

    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _row_to_dict(cursor, row):
    """libsql cursors return plain tuples — this rebuilds dict-style
    column access (row["column"]) from cursor.description, which is what
    the rest of the codebase (app.py in particular) expects."""
    return dict(zip((col[0] for col in cursor.description), row))


def init_db():
    """Kept as a standalone callable for scripts/tests that want to force
    schema creation explicitly; app.py no longer calls this at import
    time (see the comment above get_db())."""
    with get_db() as conn:
        pass  # get_db() itself already ensures the schema exists


def column_names():
    """Used by app.py's /admin/export to build the CSV header from the
    live schema, so the export never drifts out of sync with the table."""
    with get_db() as conn:
        cur = conn.execute("PRAGMA table_info(participants)")
        return [row[1] for row in cur.fetchall()]  # index 1 = column name


# --- one explicit function per step ---------------------------------
# Explicit named parameters rather than a generic dict-based updater:
# a little more typing here, but no risk of a column name coming from
# unvalidated user input and no need to cross-reference a whitelist.

def save_consent(participant_id, recruitment_source, timestamp, is_test):
    """is_test (0 or 1) is written only on first insert. The ON CONFLICT
    branch deliberately never touches it, and the triggers would refuse
    if it tried: a participant who re-submits consent keeps whatever flag
    the row was created with."""
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO participants (id, created_at, recruitment_source,
                                       consent_given, consent_timestamp, is_test)
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                consent_given = 1,
                consent_timestamp = excluded.consent_timestamp
            """,
            (participant_id, timestamp, recruitment_source, timestamp, is_test),
        )


def save_covariates(participant_id, ai_use_frequency, tech_affinity,
                     fin_lit_correct, fin_lit_self_rating, age, grade_year, gender):
    """tech_affinity: list of 3 ints. fin_lit_correct: list of 3 bools."""
    with get_db() as conn:
        conn.execute(
            """
            UPDATE participants SET
                ai_use_frequency = ?,
                tech_affinity_1 = ?, tech_affinity_2 = ?, tech_affinity_3 = ?,
                fin_lit_q1_correct = ?, fin_lit_q2_correct = ?, fin_lit_q3_correct = ?,
                fin_lit_self_rating = ?,
                age = ?,
                grade_year = ?,
                gender = ?
            WHERE id = ?
            """,
            (
                ai_use_frequency,
                *tech_affinity,
                *[int(bool(x)) for x in fin_lit_correct],
                fin_lit_self_rating,
                age,
                grade_year,
                gender,
                participant_id,
            ),
        )


def save_pre_stance(participant_id, stance, confidence, rationale, word_count):
    with get_db() as conn:
        conn.execute(
            """
            UPDATE participants SET
                pre_stance = ?, pre_confidence = ?, pre_rationale = ?, pre_rationale_word_count = ?
            WHERE id = ?
            """,
            (stance, confidence, rationale, word_count, participant_id),
        )


def log_condition(participant_id, condition, timestamp):
    """Called immediately after the random draw, before the participant's
    stance/rationale are read. See app.py's /api/submit-stance route."""
    with get_db() as conn:
        conn.execute(
            "UPDATE participants SET condition = ?, condition_assigned_at = ? WHERE id = ?",
            (condition, timestamp, participant_id),
        )


def get_participant(participant_id):
    with get_db() as conn:
        cur = conn.execute("SELECT * FROM participants WHERE id = ?", (participant_id,))
        row = cur.fetchone()
        return _row_to_dict(cur, row) if row else None


def log_verdict_attempt(participant_id, rec, created_at):
    """Append one model call (generation or API error) to verdict_attempts."""
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO verdict_attempts (participant_id, seq, kind, generation_no, outcome,
                                          verdict_field, prose, word_count, model, temperature,
                                          latency_ms, error_kind, error_detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (participant_id, rec["seq"], rec["kind"], rec["generation_no"], rec["outcome"],
             rec["verdict_field"], rec["prose"], rec["word_count"], rec["model"], rec["temperature"],
             rec["latency_ms"], rec["error_kind"], rec["error_detail"], created_at),
        )


def save_verdict_result(participant_id, *, text, word_count, target, attempts, fallback_used,
                        fallback_reason, fields_json, api_error_count, generated_at):
    """Store the final verdict. First writer wins: the UPDATE only applies while
    ai_verdict_text is still NULL, so a second (concurrent or repeated)
    request can never overwrite the stimulus a participant already saw.
    Returns True if this call stored it, False if a verdict was already there."""
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE participants SET
                ai_verdict_text = ?, ai_verdict_generated_at = ?, ai_verdict_word_count = ?,
                verdict_target = ?, verdict_attempts = ?, verdict_fallback_used = ?,
                verdict_fallback_reason = ?, verdict_fields = ?, verdict_api_error_count = ?
            WHERE id = ? AND ai_verdict_text IS NULL
            """,
            (text, generated_at, word_count, target, attempts, int(bool(fallback_used)),
             fallback_reason, fields_json, api_error_count, participant_id),
        )
        return cur.rowcount == 1


def save_post_verdict(participant_id, stance, confidence, rationale,
                       trust_items, competence_items, bias_items, intention_items,
                       attention_check_response):
    """trust_items: list of up to 12 ints. competence_items: up to 5.
    bias_items: up to 3. intention_items: up to 3. Missing trailing optional
    items should be passed as None. attention_check_response: the raw 1-7
    response to the instructed-response item — stored separately from
    trust_items on purpose, it is not part of that composite."""
    def pad(items, length):
        items = list(items) + [None] * (length - len(items))
        return items[:length]

    trust = pad(trust_items, 12)
    competence = pad(competence_items, 5)
    bias = pad(bias_items, 3)
    intention = pad(intention_items, 3)

    with get_db() as conn:
        conn.execute(
            """
            UPDATE participants SET
                post_stance = ?, post_confidence = ?, post_rationale = ?,
                trust_1=?, trust_2=?, trust_3=?, trust_4=?, trust_5=?, trust_6=?,
                trust_7=?, trust_8=?, trust_9=?, trust_10=?, trust_11=?, trust_12=?,
                competence_1=?, competence_2=?, competence_3=?, competence_4=?, competence_5=?,
                bias_1=?, bias_2=?, bias_3=?,
                intention_1=?, intention_2=?, intention_3=?,
                attention_check_response=?
            WHERE id = ?
            """,
            (stance, confidence, rationale, *trust, *competence, *bias, *intention,
             attention_check_response, participant_id),
        )


def save_debrief_feedback(participant_id, feedback_text, timestamp):
    with get_db() as conn:
        conn.execute(
            "UPDATE participants SET debrief_feedback = ?, completed_at = ? WHERE id = ?",
            (feedback_text, timestamp, participant_id),
        )


def all_participants(include_test=False):
    """Real participants only (is_test = 0) unless include_test is set.
    Legacy NULL rows and test rows both count as "not real", so they can
    never reach an export by accident."""
    where = "" if include_test else "WHERE is_test = 0"
    with get_db() as conn:
        cur = conn.execute(f"SELECT * FROM participants {where} ORDER BY created_at")
        return [_row_to_dict(cur, row) for row in cur.fetchall()]


def delete_participants_except(recruitment_source_to_keep):
    """One-off cleanup helper for clearing out test/pilot data before real
    collection starts — deletes every row whose recruitment_source doesn't
    exactly match the given value. Used via the temporary /admin/cleanup
    route in app.py; both should be removed once that specific cleanup is
    done, not kept as a standing bulk-delete capability."""
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM participants WHERE recruitment_source IS NULL OR recruitment_source != ?",
            (recruitment_source_to_keep,),
        )
        return cur.rowcount


def delete_incomplete_participants(recruitment_source):
    """One-off cleanup helper, paired with delete_participants_except above
    and used the same way via /admin/cleanup: within the pilot rows being
    kept, deletes any row that never got a verdict (ai_verdict_text IS
    NULL) — a client-side timeout, a 429/503 mid-run, or an abandoned
    session that never reached /api/generate-verdict. Confirmed live
    (2026-09-07) that a slow Gemini response can still complete
    server-side after the pilot script's client gave up and moved on, so
    NULL here reliably means "no verdict was ever generated for this
    participant", not "one might still be in flight". A row with a
    verdict but no post-verdict/debrief data is still kept — it's still a
    real audit sample for the AI prompt, just an incomplete survey."""
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM participants WHERE recruitment_source = ? AND ai_verdict_text IS NULL",
            (recruitment_source,),
        )
        return cur.rowcount
