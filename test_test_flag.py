"""
Tests for the immutable test/real flag (participants.is_test).

Runs against a throwaway SQLite file, never data/study.db or Turso.
Run with:  python test_test_flag.py
"""

import os
import re
import tempfile

os.environ.pop("TURSO_DATABASE_URL", None)

import libsql

import db

_tmp = tempfile.mkdtemp(prefix="testflag-")
db.DB_PATH = os.path.join(_tmp, "t.db")

import app as A

A.TEST_MODE_TOKEN = "unit-test-token-not-a-secret"
A.ADMIN_EXPORT_PASSWORD = "unit-test-admin-pw"
TOKEN = A.TEST_MODE_TOKEN

client = A.app.test_client()


def raw(sql, params=()):
    """A separate connection, bypassing the app entirely."""
    conn = libsql.connect(db.DB_PATH)
    try:
        cur = conn.execute(sql, params)
        rows = cur.fetchall() if cur.description else None
        conn.commit()
        return rows
    finally:
        conn.close()


def refuses(sql, params=(), needle=""):
    try:
        raw(sql, params)
    except Exception as e:  # libsql surfaces trigger aborts as a plain exception
        assert needle in str(e), f"refused, but with an unexpected message: {e}"
        return True
    return False


def flag(pid):
    r = raw("SELECT is_test FROM participants WHERE id = ?", (pid,))
    return r[0][0] if r else "NO ROW"


def consent(pid, token=None, src=""):
    body = {"participant_id": pid, "recruitment_source": src}
    if token is not None:
        body["test_token"] = token
    return client.post("/api/consent", json=body)


def test_schema_installs_column_and_triggers():
    db.init_db()
    cols = [r[1] for r in raw("PRAGMA table_info(participants)")]
    assert "is_test" in cols
    names = {r[0] for r in raw("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert {"participants_is_test_on_insert", "participants_is_test_immutable", "participants_id_immutable"} <= names, names
    print("PASS: is_test column and all three triggers are installed.")


def test_real_and_test_consent_are_flagged_correctly():
    r = consent("5b0e3c52-1111-4222-8333-444455556666")
    assert r.status_code == 200 and r.get_json() == {"ok": True, "is_test": False}
    assert flag("5b0e3c52-1111-4222-8333-444455556666") == 0
    r = consent("test-aaa", token=TOKEN)
    assert r.status_code == 200 and r.get_json() == {"ok": True, "is_test": True}
    assert flag("test-aaa") == 1
    print("PASS: real consent -> 0, token + test- id -> 1.")


def test_every_ambiguous_combination_is_refused_and_creates_no_row():
    cases = [
        ("test- id, no token", consent("test-bbb")),
        ("token, non-test id", consent("6c1f4d63-aaaa-4bbb-8ccc-ddddeeeeffff", token=TOKEN)),
        ("wrong token, test- id", consent("test-ccc", token="wrong")),
        ("wrong token, plain id", consent("7d205e74-aaaa-4bbb-8ccc-ddddeeeeffff", token="wrong")),
        ("empty token, test- id", consent("test-ddd", token="")),
    ]
    for label, r in cases:
        assert r.status_code == 400, (label, r.status_code)
    for pid in ("test-bbb", "6c1f4d63-aaaa-4bbb-8ccc-ddddeeeeffff", "test-ccc", "7d205e74-aaaa-4bbb-8ccc-ddddeeeeffff", "test-ddd"):
        assert flag(pid) == "NO ROW", pid
    r = client.post("/api/consent", json={"participant_id": 123})
    assert r.status_code == 400
    print("PASS: 5 ambiguous/invalid combinations all refused (400) with no row created.")


def test_scripted_ids_without_test_mode_are_refused():
    for bad in ("pilot6-abc123", "quota-diag-1", "migration-verify-9", "12345", "5B0E3C52-1111-4222-8333-444455556666", "5b0e3c52-1111-3222-8333-444455556666"):
        r = consent(bad)
        assert r.status_code == 400, (bad, r.status_code)
        assert flag(bad) == "NO ROW", bad
    assert consent("8e316f85-1111-4222-b333-444455556666").status_code == 200  # a genuine v4 UUID is accepted
    print("PASS: 6 non-UUID / non-v4 ids (e.g. 'pilot6-abc123') refused without test mode; a real v4 UUID accepted.")


def test_test_mode_is_disabled_when_no_token_is_configured():
    A.TEST_MODE_TOKEN = None
    try:
        assert consent("test-eee", token="anything").status_code == 400
        assert consent("test-eee").status_code == 400
        assert client.get("/?test=anything").status_code == 403
        assert flag("test-eee") == "NO ROW"
    finally:
        A.TEST_MODE_TOKEN = TOKEN
    print("PASS: with TEST_MODE_TOKEN unset, no test row or test page is possible.")


def test_database_refuses_flag_and_id_changes_even_for_raw_sql():
    assert refuses("UPDATE participants SET is_test = 1 WHERE id = ?", ("5b0e3c52-1111-4222-8333-444455556666",), "immutable")
    assert refuses("UPDATE participants SET is_test = 0 WHERE id = ?", ("test-aaa",), "immutable")
    assert refuses("UPDATE participants SET is_test = NULL WHERE id = ?", ("test-aaa",), "immutable")
    assert refuses("UPDATE participants SET is_test = 1 WHERE id = ?", ("test-aaa",), "immutable"), "even a no-op rewrite is refused"
    assert refuses("UPDATE participants SET id = 'test-hijack' WHERE id = ?", ("5b0e3c52-1111-4222-8333-444455556666",), "immutable")
    assert refuses("UPDATE participants SET id = 'real-hijack' WHERE id = ?", ("test-aaa",), "immutable")
    assert flag("5b0e3c52-1111-4222-8333-444455556666") == 0 and flag("test-aaa") == 1
    print("PASS: 6 attempts to flip is_test or rename an id were refused by the database.")


def test_database_refuses_inconsistent_or_unflagged_inserts():
    base = "INSERT INTO participants (id, created_at, consent_given, is_test) VALUES (?, 't', 1, {})"
    assert refuses(base.format("NULL"), ("raw-null",), "is_test must be")
    assert refuses(base.format("2"), ("raw-two",), "is_test must be")
    assert refuses(base.format("1"), ("not-test-prefixed",), "is_test must be"), "flagged test but real-looking id"
    assert refuses(base.format("0"), ("test-looks-test",), "is_test must be"), "flagged real but test- id"
    assert refuses("INSERT INTO participants (id, created_at, consent_given) VALUES ('raw-omitted', 't', 1)", (), "is_test must be")
    for pid in ("raw-null", "raw-two", "not-test-prefixed", "test-looks-test", "raw-omitted"):
        assert flag(pid) == "NO ROW", pid
    print("PASS: 5 inconsistent / NULL / omitted-flag inserts were refused by the database.")


def test_reconsent_keeps_the_original_flag():
    assert consent("test-aaa", token=TOKEN).status_code == 200
    assert consent("5b0e3c52-1111-4222-8333-444455556666").status_code == 200
    assert flag("test-aaa") == 1 and flag("5b0e3c52-1111-4222-8333-444455556666") == 0
    print("PASS: re-submitting consent never changes an existing row's flag.")


def test_legacy_rows_are_neither_real_nor_promotable():
    legacy_path = os.path.join(_tmp, "legacy.db")
    old_schema = re.sub(r"    is_test INTEGER,.*?\n.*?\n.*?\n", "", db.SCHEMA, count=1)
    assert "is_test" not in old_schema.split("recruitment_source")[1].split("consent_given")[0]
    conn = libsql.connect(legacy_path)
    conn.execute(old_schema)
    conn.execute("INSERT INTO participants (id, created_at, consent_given) VALUES ('pilot6-legacy', 't', 1)")
    conn.commit(); conn.close()

    saved = db.DB_PATH, db._schema_ready
    db.DB_PATH, db._schema_ready = legacy_path, False
    try:
        db.init_db()
        db._schema_ready = False
        db.init_db()  # idempotent second boot
        rows = raw("SELECT id, is_test FROM participants")
        assert rows == [("pilot6-legacy", None)], rows
        assert refuses("UPDATE participants SET is_test = 0 WHERE id = 'pilot6-legacy'", (), "immutable"), "legacy must never become real"
        raw("UPDATE participants SET is_test = 1 WHERE id = 'pilot6-legacy'")
        assert raw("SELECT is_test FROM participants")[0][0] == 1
        assert refuses("UPDATE participants SET is_test = 0 WHERE id = 'pilot6-legacy'", (), "immutable")
    finally:
        db.DB_PATH, db._schema_ready = saved
    print("PASS: migration leaves old rows NULL (not real), is idempotent, and allows only NULL -> 1.")


def test_default_export_contains_only_real_rows():
    raw("DELETE FROM participants")
    consent("5b0e3c52-1111-4222-8333-444455556666"); consent("6c1f4d63-2222-4333-9444-555566667777"); consent("test-x", token=TOKEN)
    raw("DROP TRIGGER participants_is_test_on_insert")  # simulate a legacy NULL row existing in the table
    raw("INSERT INTO participants (id, created_at, consent_given) VALUES ('legacy-1', 't', 1)")
    db._schema_ready = False; db.init_db()  # triggers reinstalled
    ids = lambda text: {line.split(",")[0] for line in text.strip().splitlines()[1:]}
    default = client.get("/admin/export?password=" + A.ADMIN_EXPORT_PASSWORD)
    everything = client.get("/admin/export?password=" + A.ADMIN_EXPORT_PASSWORD + "&include_test=1")
    assert default.status_code == 200
    assert ids(default.get_data(as_text=True)) == {"5b0e3c52-1111-4222-8333-444455556666", "6c1f4d63-2222-4333-9444-555566667777"}, ids(default.get_data(as_text=True))
    assert ids(everything.get_data(as_text=True)) == {"5b0e3c52-1111-4222-8333-444455556666", "6c1f4d63-2222-4333-9444-555566667777", "test-x", "legacy-1"}
    assert "is_test" in default.get_data(as_text=True).splitlines()[0]
    print("PASS: default export = real rows only; include_test=1 adds test and legacy rows; is_test column present.")


def test_test_page_banner_and_refusal():
    page = client.get(f"/?test={TOKEN}").get_data(as_text=True)
    assert "TEST RUN" in page and "testMode: true" in page and f'testToken: "{TOKEN}"' in page
    assert client.get("/?test=nope").status_code == 403
    assert client.get("/?test=").status_code == 403
    normal = client.get("/").get_data(as_text=True)
    assert "TEST RUN" not in normal and "testMode: false" in normal and "testToken: null" in normal and TOKEN not in normal
    print("PASS: valid token -> banner + test mode; wrong/empty token -> 403; normal page carries no test mode and no token.")


if __name__ == "__main__":
    test_schema_installs_column_and_triggers()
    test_real_and_test_consent_are_flagged_correctly()
    test_every_ambiguous_combination_is_refused_and_creates_no_row()
    test_scripted_ids_without_test_mode_are_refused()
    test_test_mode_is_disabled_when_no_token_is_configured()
    test_database_refuses_flag_and_id_changes_even_for_raw_sql()
    test_database_refuses_inconsistent_or_unflagged_inserts()
    test_reconsent_keeps_the_original_flag()
    test_legacy_rows_are_neither_real_nor_promotable()
    test_default_export_contains_only_real_rows()
    test_test_page_banner_and_refusal()
    print("\nAll test-flag tests passed.")
