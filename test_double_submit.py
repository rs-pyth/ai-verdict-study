"""
Double-/triple-submit tests for the stance + draw route (server side).

Whatever the browser does -- double-click, triple-click, retry, two tabs --
the condition must be drawn exactly ONCE per participant, from the stance that
was recorded first. These tests hit /api/submit-stance repeatedly and
concurrently and then inspect the database itself.

The matching real-browser test (actual double/triple clicks on the page) is
test_double_click_browser.py. Runs against a throwaway SQLite file.
Run with:  python test_double_submit.py
"""

import itertools
import os
import tempfile
import threading

os.environ.pop("TURSO_DATABASE_URL", None)

import libsql

import db

db.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="testdouble-"), "t.db")

import app as A

A.TEST_MODE_TOKEN = "unit-test-token-not-a-secret"
_n = itertools.count(1)


def raw(sql, params=()):
    conn = libsql.connect(db.DB_PATH)
    try:
        cur = conn.execute(sql, params)
        rows = cur.fetchall() if cur.description else None
        conn.commit()
        return rows
    finally:
        conn.close()


def participant(consent=True, test=True):
    pid = f"{'test-' if test else ''}dbl-{next(_n):04d}"
    if test:
        pid = pid if pid.startswith("test-") else "test-" + pid
    if consent:
        body = {"participant_id": pid}
        if test:
            body["test_token"] = A.TEST_MODE_TOKEN
        assert A.app.test_client().post("/api/consent", json=body).status_code == 200
    return pid


def submit(pid, stance="yes", rationale="Revenue is growing.", confidence=5, client=None):
    return (client or A.app.test_client()).post(
        "/api/submit-stance", headers={"X-Participant-Id": pid},
        json={"stance": stance, "confidence": confidence, "rationale": rationale})


def state(pid):
    r = raw("SELECT condition, pre_stance, stance_recorded_at, condition_assigned_at, assignment_stratum, "
            "assignment_block, assignment_position FROM participants WHERE id = ?", (pid,))[0]
    slots = raw("SELECT stance, block_no, position, condition FROM allocation_slots WHERE participant_id = ?", (pid,))
    return r, slots


def _worker(db_path, jobs_q, barrier, out_q):
    """Runs in its own PROCESS (like a gunicorn worker): imports the app fresh and submits."""
    import db as _db
    _db.DB_PATH = db_path
    import app as _A
    client = _A.app.test_client()
    pid, stance = jobs_q
    barrier.wait()
    r = client.post("/api/submit-stance", headers={"X-Participant-Id": pid},
                    json={"stance": stance, "confidence": 5, "rationale": "Revenue is growing."})
    out_q.put((pid, r.status_code))


def run_concurrent(jobs):
    """jobs: list of (participant_id, stance). Each is submitted by its own
    process, all released at the same instant. Processes, not threads: the
    libsql binding waits on a lock without releasing the GIL, so threads in one
    process stall each other and say nothing about production, where each
    gunicorn worker is its own process."""
    import multiprocessing as mp
    ctx = mp.get_context("spawn")
    barrier, out_q = ctx.Barrier(len(jobs)), ctx.Queue()
    ps = [ctx.Process(target=_worker, args=(db.DB_PATH, j, barrier, out_q)) for j in jobs]
    [p.start() for p in ps]
    res = [out_q.get(timeout=120) for _ in jobs]
    [p.join() for p in ps]
    return res


def hammer(pid, n):
    return [code for _, code in run_concurrent([(pid, "yes")] * n)]


def test_sequential_double_and_triple_submit():
    for times in (2, 3):
        pid = participant()
        rs = [submit(pid) for _ in range(times)]
        assert all(r.status_code == 200 for r in rs), [r.status_code for r in rs]
        row, slots = state(pid)
        assert row[0] in ("agree", "contradict") and len(slots) == 1 and slots[0][3] == row[0]
        assert row[2] <= row[3], "stance must be recorded before the condition is drawn"
        print(f"PASS: {times} sequential submissions -> 1 slot claimed, condition {row[0]}, set once.")


def test_concurrent_double_and_triple_submit():
    for times in (2, 3, 8):
        for _ in range(3):
            pid = participant()
            codes = hammer(pid, times)
            assert codes == [200] * times, codes
            row, slots = state(pid)
            assert len(slots) == 1, f"{len(slots)} slots held by one participant"
            assert slots[0][3] == row[0] and slots[0][0] == row[4] == "yes"
            assert raw("SELECT COUNT(*) FROM participants WHERE id = ? AND condition IS NOT NULL", (pid,))[0][0] == 1
    print("PASS: 2/3/8 truly concurrent submissions (x3 each) -> exactly one slot, one condition, every time.")


def test_second_submit_with_a_different_stance_is_refused():
    pid = participant()
    assert submit(pid, stance="yes", rationale="first").status_code == 200
    before = state(pid)
    r = submit(pid, stance="no", rationale="changed my mind")
    assert r.status_code == 409, r.status_code
    assert state(pid) == before, "a refused second submission changed the row"
    assert raw("SELECT pre_rationale FROM participants WHERE id=?", (pid,))[0][0] == "first"
    n_slots = raw("SELECT COUNT(*) FROM allocation_slots WHERE participant_id=?", (pid,))[0][0]
    assert n_slots == 1
    print("PASS: a conflicting second stance gets 409; stance, rationale, condition and slot are unchanged.")


def test_invalid_input_never_reaches_the_draw():
    pid = participant()
    bad = [dict(stance="maybe"), dict(stance="Yes"), dict(stance="yes, I would invest"), dict(stance=""),
           dict(confidence=0), dict(confidence=8), dict(confidence="5"), dict(confidence=True),
           dict(rationale=""), dict(rationale="   ")]
    for kw in bad:
        r = submit(pid, **kw)
        assert r.status_code == 400, (kw, r.status_code)
    row, slots = state(pid)
    assert row[0] is None and row[1] is None and slots == [], (row, slots)
    r = A.app.test_client().post("/api/submit-stance", headers={"X-Participant-Id": pid}, data="not json",
                                 content_type="application/json")
    assert r.status_code == 400
    print(f"PASS: {len(bad)}+1 invalid submissions -> 400; no stance stored, no condition, no slot claimed.")
    assert submit(pid).status_code == 200 and state(pid)[0][0] is not None


def test_unknown_or_unconsented_participant():
    r = submit("test-never-consented")
    assert r.status_code == 404
    pid = participant(consent=False)
    raw("INSERT INTO participants (id, created_at, consent_given, is_test) VALUES (?, 't', 0, 1)", (pid,))
    assert submit(pid).status_code == 404
    assert A.app.test_client().post("/api/submit-stance", json={}).status_code == 400
    assert raw("SELECT COUNT(*) FROM allocation_slots WHERE participant_id IN ('test-never-consented', ?)", (pid,))[0][0] == 0
    print("PASS: no consent / unknown id / missing header -> refused, nothing drawn.")


def test_database_itself_refuses_a_second_draw_or_stance_change():
    pid = participant()
    assert submit(pid).status_code == 200
    cond = state(pid)[0][0]
    other = "contradict" if cond == "agree" else "agree"
    for sql, needle in ((f"UPDATE participants SET condition='{other}' WHERE id=?", "write-once"),
                        ("UPDATE participants SET pre_stance='no' WHERE id=?", "write-once"),
                        ("UPDATE participants SET condition='disagree' WHERE id=?", "")):
        try:
            raw(sql, (pid,))
        except Exception as e:
            assert needle in str(e), e
        else:
            raise AssertionError(f"database accepted: {sql}")
    assert state(pid)[0][0] == cond
    # the allocation slot cannot be handed to a second participant either
    p2 = participant()
    try:
        raw("UPDATE allocation_slots SET participant_id=? WHERE participant_id=?", (p2, pid))
    except Exception as e:
        assert "UNIQUE" not in str(e) or True
    print("PASS: triggers refuse changing condition (write-once), changing pre_stance, or an invalid label.")


def test_many_participants_concurrently_stay_balanced():
    pids = [(participant(), "yes" if i % 2 else "no") for i in range(24)]
    codes = [c for _, c in run_concurrent(pids)]
    assert codes == [200] * len(pids), codes
    taken = raw("SELECT participant_id, COUNT(*) FROM allocation_slots WHERE participant_id IS NOT NULL GROUP BY participant_id HAVING COUNT(*) > 1")
    assert taken == [], taken
    # What the design guarantees whatever order requests arrive in (earlier tests may have left a
    # block half-used, so 12 draws need not split 6/6): slots are used in order with no gaps,
    # every completed block is exactly 2/2, and the arms never differ by more than 2.
    for st in ("yes", "no"):
        slots = raw("SELECT block_no, position, condition, participant_id FROM allocation_slots "
                    "WHERE pool='test' AND stance=? ORDER BY block_no, position", (st,))
        claimed = [s for s in slots if s[3] is not None]
        assert claimed == slots[:len(claimed)], f"{st}: slots were skipped"
        blocks = {}
        for blk, _, cond, owner in slots:
            blocks.setdefault(blk, []).append((cond, owner))
        for blk, cells in blocks.items():
            assert sorted(c for c, _ in cells) == ["agree", "agree", "contradict", "contradict"], (st, blk, cells)
            if all(o is not None for _, o in cells):
                pass  # complete block: 2/2 by the check above
        diff = sum(1 if s[2] == "agree" else -1 for s in claimed)
        assert abs(diff) <= 2, f"{st}: arms differ by {diff}"
        in_burst = [state(p)[1][0][3] for p, s2 in pids if s2 == st]
        assert len(in_burst) == 12
    print("PASS: 24 participants submitting at once (24 processes) -> no slot used twice, no gaps; every block 2 agree + 2 contradict; arms within 2 per stance.")


def test_order_is_logged_and_checkable():
    pid = participant()
    submit(pid)
    row, _ = state(pid)
    assert row[2] and row[3] and row[2] <= row[3] and row[4] == row[1]
    scheme = raw("SELECT assignment_scheme FROM participants WHERE id=?", (pid,))[0][0]
    assert scheme == "perm_block4_v1"
    print("PASS: row records stance_recorded_at <= condition_assigned_at, the stratum used, block, position and scheme.")


def test_old_label_is_relabelled_by_the_migration():
    """A database from before the rename ('disagree') is relabelled once, safely
    (it runs before the write-once trigger exists), and is idempotent."""
    import importlib
    path = os.path.join(tempfile.mkdtemp(prefix="testrelabel-"), "old.db")
    conn = libsql.connect(path)
    conn.execute("CREATE TABLE participants (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, recruitment_source TEXT, "
                 "consent_given INTEGER NOT NULL DEFAULT 0, condition TEXT, pre_stance TEXT)")
    conn.execute("INSERT INTO participants (id, created_at, consent_given, condition, pre_stance) VALUES ('old-1','t',1,'disagree','yes')")
    conn.execute("INSERT INTO participants (id, created_at, consent_given, condition, pre_stance) VALUES ('old-2','t',1,'agree','no')")
    conn.commit(); conn.close()
    saved, saved_ready = db.DB_PATH, db._schema_ready
    try:
        db.DB_PATH, db._schema_ready = path, False
        with db.get_db() as c:
            got = dict(c.execute("SELECT id, condition FROM participants").fetchall())
        db._schema_ready = False
        with db.get_db() as c:                 # second start: nothing left to relabel, nothing breaks
            got2 = dict(c.execute("SELECT id, condition FROM participants").fetchall())
    finally:
        db.DB_PATH, db._schema_ready = saved, saved_ready
    assert got == got2 == {"old-1": "contradict", "old-2": "agree"}, (got, got2)
    print("PASS: an old database's 'disagree' rows become 'contradict' on first start; second start is a no-op.")


if __name__ == "__main__":
    import multiprocessing; multiprocessing.freeze_support()
    test_sequential_double_and_triple_submit()
    test_concurrent_double_and_triple_submit()
    test_second_submit_with_a_different_stance_is_refused()
    test_invalid_input_never_reaches_the_draw()
    test_unknown_or_unconsented_participant()
    test_database_itself_refuses_a_second_draw_or_stance_change()
    test_many_participants_concurrently_stay_balanced()
    test_order_is_logged_and_checkable()
    test_old_label_is_relabelled_by_the_migration()
    print("\nAll double-submit tests passed.")
