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
import time
import libsql
from contextlib import contextmanager

import randomization

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
    stance_recorded_at TEXT,           -- when pre_stance was stored. WRITE-ONCE (trigger). The draw below
                                        -- happens strictly after it: stance_recorded_at <= condition_assigned_at.

    -- Step 3: RANDOMIZATION — see randomization.py. The condition is drawn
    -- AFTER the stance is recorded, stratified on that stance (permuted blocks
    -- of 4), and never depends on free text. WRITE-ONCE: a trigger refuses to
    -- change it after it is set.
    condition TEXT,                    -- 'agree' / 'contradict'
    condition_assigned_at TEXT,
    assignment_scheme TEXT,            -- e.g. 'perm_block4_v1'
    assignment_stratum TEXT,           -- the stance the draw was stratified on ('yes' / 'no')
    assignment_block INTEGER,          -- which block of 4 within that stratum
    assignment_position INTEGER,       -- 1-4, position within that block

    -- Step 4: AI verdict
    verdict_requested_at TEXT,         -- when the browser first asked for the verdict (start of the wait). Write-once.
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
    "ALTER TABLE participants ADD COLUMN stance_recorded_at TEXT",
    "ALTER TABLE participants ADD COLUMN assignment_scheme TEXT",
    "ALTER TABLE participants ADD COLUMN assignment_stratum TEXT",
    "ALTER TABLE participants ADD COLUMN assignment_block INTEGER",
    "ALTER TABLE participants ADD COLUMN assignment_position INTEGER",
    "ALTER TABLE participants ADD COLUMN verdict_requested_at TEXT",
]

# The condition label 'disagree' was renamed 'contradict' (the pre-registered
# wording). Runs after the column migrations and BEFORE the write-once
# triggers exist, so it can relabel old rows; it is a no-op once none are left.
RELABEL = "UPDATE participants SET condition = 'contradict' WHERE condition = 'disagree'"

# The pre-generated allocation sequence. One row per slot of a permuted block;
# a slot is handed to exactly one participant, claimed by a single UPDATE (see
# claim_slot). `pool` keeps test runs (is_test = 1) from consuming real
# participants' blocks: the two pools are balanced independently. The table is
# also the audit trail of the whole sequence that was dealt.
ALLOCATION_SCHEMAS = [
    """
    CREATE TABLE IF NOT EXISTS allocation_slots (
        pool TEXT NOT NULL,                -- 'real' | 'test'
        stance TEXT NOT NULL,              -- stratum: 'yes' | 'no'
        block_no INTEGER NOT NULL,
        position INTEGER NOT NULL,         -- 1..4
        condition TEXT NOT NULL,           -- 'agree' | 'contradict'
        participant_id TEXT UNIQUE,        -- NULL until claimed; one slot per participant, ever
        claimed_at TEXT,
        PRIMARY KEY (pool, stance, block_no, position)
    )
    """,
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
    # Write-once fields: the stance the draw is stratified on, and the draw itself.
    """
    CREATE TRIGGER IF NOT EXISTS participants_condition_write_once
    BEFORE UPDATE OF condition ON participants
    WHEN OLD.condition IS NOT NULL AND NEW.condition IS NOT OLD.condition
    BEGIN
        SELECT RAISE(ABORT, 'condition is write-once: it was already drawn for this participant');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS participants_condition_valid
    BEFORE UPDATE OF condition ON participants
    WHEN NEW.condition IS NOT NULL AND NEW.condition NOT IN ('agree', 'contradict')
    BEGIN
        SELECT RAISE(ABORT, 'condition must be agree or contradict');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS participants_stance_write_once
    BEFORE UPDATE OF pre_stance ON participants
    WHEN OLD.pre_stance IS NOT NULL AND NEW.pre_stance IS NOT OLD.pre_stance
    BEGIN
        SELECT RAISE(ABORT, 'pre_stance is write-once');
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
        # No PRAGMA busy_timeout on purpose: libsql waits without releasing the
        # GIL, which stalls the very thread that holds the lock (measured). A
        # prompt "database is locked" plus retry_on_contention() works instead.

    if not _schema_ready:
        conn.execute(SCHEMA)
        for migration in MIGRATIONS:
            try:
                conn.execute(migration)
            except ValueError as e:
                if "duplicate column name" not in str(e):
                    raise  # a real failure, not just "already migrated"
        conn.execute(RELABEL)
        for trigger in TRIGGERS:
            conn.execute(trigger)
        for statement in VERDICT_ATTEMPTS_SCHEMAS + ALLOCATION_SCHEMAS:
            conn.execute(statement)
        conn.commit()
        _schema_ready = True

    try:
        yield conn
        conn.commit()
    except BaseException:
        # Release the write lock NOW. A failed connection otherwise lingers (the
        # exception's traceback keeps it alive) still holding its open
        # transaction, and every other request then sees "database is locked".
        try:
            conn.rollback()
        except Exception:
            pass
        raise
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


def _is_retryable(exc):
    """Contention, not a bug: a locked file, a stale read snapshot, or losing a
    race for the same participant's slot (UNIQUE on participant_id). A fresh
    attempt on a new connection sees the winner's slot and returns it."""
    text = str(exc).lower()
    return any(w in text for w in ("locked", "busy", "snapshot", "unique constraint"))


def retry_on_contention(fn):
    """Re-run a write that failed only because another request was writing at
    the same moment (locked file / stale snapshot). Every function it wraps is
    idempotent (write-once WHERE clauses), so re-running cannot double-apply."""
    def wrapper(*args, **kwargs):
        deadline = time.monotonic() + 10
        while True:
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                if not _is_retryable(e) or time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
    wrapper.__name__, wrapper.__doc__, wrapper.__wrapped__ = fn.__name__, fn.__doc__, fn
    return wrapper


@retry_on_contention
def record_stance(participant_id, stance, confidence, rationale, word_count, timestamp):
    """STEP 1 of the randomization order: store the participant's stance.

    Write-once and consent-gated: the UPDATE applies only while pre_stance is
    still NULL for a row that gave consent, so a repeated or concurrent call
    cannot overwrite what was first recorded (the trigger forbids it too).
    Returns True if this call recorded it, False if it was already there (or
    the participant doesn't exist / didn't consent)."""
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE participants SET
                pre_stance = ?, pre_confidence = ?, pre_rationale = ?,
                pre_rationale_word_count = ?, stance_recorded_at = ?
            WHERE id = ? AND consent_given = 1 AND pre_stance IS NULL
            """,
            (stance, confidence, rationale, word_count, timestamp, participant_id),
        )
        return cur.rowcount == 1


def claim_slot(participant_id, stance, pool):
    """Hand `participant_id` the next unclaimed slot of `stance`'s stratum.

    Returns (block_no, position, condition). Idempotent: a participant who
    already holds a slot gets that same slot back, never a second one. The
    claim is ONE UPDATE statement, so two requests can never take the same
    slot; a new block of 4 (2 agree + 2 contradict, shuffled) is inserted by
    ONE multi-row INSERT when the stratum has no free slot left, so a block is
    never half-written. Retries briefly on contention (see _is_retryable)."""
    deadline = time.monotonic() + 10
    while True:
        try:
            return _claim_slot_once(participant_id, stance, pool)
        except Exception as e:  # libsql surfaces SQLite errors as plain exceptions
            if not _is_retryable(e) or time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def _claim_slot_once(participant_id, stance, pool):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    with get_db() as conn:
        def mine():
            cur = conn.execute(
                "SELECT block_no, position, condition FROM allocation_slots WHERE participant_id = ?",
                (participant_id,))
            rows = cur.fetchall()  # fetchall, not fetchone: an unfinished SELECT keeps a read lock
            return tuple(rows[0]) if rows else None

        held = mine()
        if held:
            return held
        for _ in range(5):
            conn.execute(
                """
                UPDATE allocation_slots SET participant_id = ?, claimed_at = ?
                WHERE pool = ? AND stance = ? AND participant_id IS NULL
                  AND rowid = (SELECT MIN(rowid) FROM allocation_slots
                               WHERE pool = ? AND stance = ? AND participant_id IS NULL)
                """,
                (participant_id, stamp, pool, stance, pool, stance))
            held = mine()
            if held:
                return held
            # No free slot in this stratum: deal a new block. INSERT OR IGNORE
            # on the primary key makes a racing second writer a harmless no-op.
            cur = conn.execute(
                "SELECT COALESCE(MAX(block_no), 0) + 1 FROM allocation_slots WHERE pool = ? AND stance = ?",
                (pool, stance))
            block_no = cur.fetchall()[0][0]
            block = randomization.new_block()
            conn.execute(
                "INSERT OR IGNORE INTO allocation_slots (pool, stance, block_no, position, condition) VALUES "
                + ", ".join(["(?, ?, ?, ?, ?)"] * len(block)),
                [v for pos, c in enumerate(block, 1) for v in (pool, stance, block_no, pos, c)])
        raise RuntimeError("could not claim an allocation slot")


@retry_on_contention
def log_condition(participant_id, draw, timestamp):
    """STEP 2 (after record_stance): store the drawn condition and how it was
    drawn. Write-once: applies only while condition is NULL, and only if the
    stratum used is the stance on record. Returns True if stored."""
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE participants SET
                condition = ?, condition_assigned_at = ?, assignment_scheme = ?,
                assignment_stratum = ?, assignment_block = ?, assignment_position = ?
            WHERE id = ? AND condition IS NULL AND pre_stance = ?
            """,
            (draw["condition"], timestamp, draw["scheme"], draw["stratum"],
             draw["block"], draw["position"], participant_id, draw["stratum"]),
        )
        return cur.rowcount == 1


@retry_on_contention
def mark_verdict_requested(participant_id, timestamp):
    """Start of the wait for the verdict (write-once); used for active time."""
    with get_db() as conn:
        conn.execute(
            "UPDATE participants SET verdict_requested_at = ? WHERE id = ? AND verdict_requested_at IS NULL",
            (timestamp, participant_id))


def get_participant(participant_id):
    with get_db() as conn:
        cur = conn.execute("SELECT * FROM participants WHERE id = ?", (participant_id,))
        rows = cur.fetchall()  # fetchall, not fetchone: an unfinished SELECT keeps a read lock
        return _row_to_dict(cur, rows[0]) if rows else None


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


@retry_on_contention
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
