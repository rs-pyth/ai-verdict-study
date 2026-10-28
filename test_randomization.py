"""
Tests for the stance-first, stance-stratified draw (randomization.py + db.claim_slot).

Runs against a throwaway SQLite file, never data/study.db or Turso.
Run with:  python test_randomization.py
"""

import inspect
import itertools
import os
import random
import tempfile
from collections import Counter

os.environ.pop("TURSO_DATABASE_URL", None)

import db

db.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="testrand-"), "t.db")

import randomization as R

_n = itertools.count(1)


def new_participant(pool="real"):
    """A consenting participant row, inserted directly (this file tests the draw, not the routes)."""
    pid = f"{'test-' if pool == 'test' else ''}rand-{next(_n):05d}"
    with db.get_db() as conn:
        conn.execute("INSERT INTO participants (id, created_at, consent_given, is_test) VALUES (?, 't', 1, ?)",
                     (pid, 1 if pool == "test" else 0))
    return pid


def deal(stance, pool="real"):
    pid = new_participant(pool)
    d = R.draw_condition(stance, lambda s: db.claim_slot(pid, s, pool))
    return pid, d


def test_signature_cannot_carry_free_text():
    params = list(inspect.signature(R.draw_condition).parameters)
    assert params == ["stance", "next_slot"], params
    assert "rationale" not in " ".join(params)
    print("PASS: draw_condition's only participant-derived input is `stance`.")


def test_stance_must_be_exactly_yes_or_no():
    for ok in ("yes", "no"):
        assert R.validate_stance(ok) == ok
    bad = [None, "", "Yes", "YES", " yes", "yes ", "yes\n", "maybe", "y", 1, 0, True, False, ["yes"], {"yes"},
           "yes, because the growth numbers are strong", "I would invest because revenue is up"]
    for b in bad:
        try:
            R.validate_stance(b)
        except ValueError:
            continue
        raise AssertionError(f"accepted a non-stance value: {b!r}")
    pid = new_participant()
    for b in ("Yes", "free text rationale"):
        try:
            R.draw_condition(b, lambda s: db.claim_slot(pid, s, "real"))
        except ValueError:
            pass
        else:
            raise AssertionError("draw ran on an invalid stance")
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM allocation_slots WHERE participant_id = ?", (pid,)).fetchone()[0] == 0
    print(f"PASS: {len(bad)} non-stance values rejected before the draw; no slot was consumed.")


def test_blocks_are_balanced_and_all_orders_occur():
    seen = Counter()
    for _ in range(6000):
        b = R.new_block()
        assert len(b) == 4 and b.count("agree") == 2 and b.count("contradict") == 2, b
        seen[tuple(b)] += 1
    assert len(seen) == 6, f"only {len(seen)} of the 6 possible orders ever occurred"
    assert min(seen.values()) > 800, f"orders far from uniform (expected ~1000 each): {dict(seen)}"
    print(f"PASS: 6000 blocks all 2 agree + 2 contradict; all 6 orders occur (min {min(seen.values())}, max {max(seen.values())}).")


def test_stratified_balance_in_every_block():
    # Interleave stances in a messy order, as real recruitment would.
    rng = random.Random(7)
    order = [rng.choice(["yes", "no"]) for _ in range(400)]
    by_stance = {"yes": [], "no": []}
    for st in order:
        _, d = deal(st)
        assert d["stratum"] == st and d["scheme"] == R.SCHEME and 1 <= d["position"] <= 4
        by_stance[st].append(d)
    for st, ds in by_stance.items():
        diff = 0
        for i, d in enumerate(ds, 1):
            diff += 1 if d["condition"] == "agree" else -1
            assert abs(diff) <= 2, f"{st}: arms drifted by {diff} after {i}"
            if i % 4 == 0:
                assert diff == 0, f"{st}: not exactly balanced after {i} (diff {diff})"
        for k in range(0, len(ds) - len(ds) % 4, 4):
            blk = [d["condition"] for d in ds[k:k + 4]]
            assert blk.count("agree") == 2 and len({d["block"] for d in ds[k:k + 4]}) == 1
    n = {st: len(ds) for st, ds in by_stance.items()}
    print(f"PASS: {n['yes']} yes + {n['no']} no interleaved; every completed block is 2/2 within its stance, "
          f"running imbalance never above 2.")


def test_draw_ignores_everything_but_stance():
    """Same RNG stream + same stance sequence -> identical conditions, however
    different the participants' free text, confidence or ids are."""
    def run(texts):
        db.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="testrand-"), "t.db")
        db._schema_ready = False
        R._rng = random.Random(2024)
        out = []
        for i, text in enumerate(texts):
            pid = new_participant()
            with db.get_db() as conn:
                conn.execute("UPDATE participants SET pre_rationale = ? WHERE id = ?", (text, pid))
            st = "yes" if i % 3 else "no"
            out.append(R.draw_condition(st, lambda s: db.claim_slot(pid, s, "real"))["condition"])
        return out

    saved = (db.DB_PATH, R._rng)
    a = run([f"short {i}" for i in range(60)])
    b = run(["I would definitely invest, the AI should disagree with me " * (i % 7 + 1) for i in range(60)])
    db.DB_PATH, R._rng = saved
    db._schema_ready = False
    assert a == b, "conditions changed with the participant's text"
    print("PASS: 60 draws are identical under two completely different sets of free-text rationales.")


def test_entropy_source_not_seedable():
    assert isinstance(R._rng, random.SystemRandom), type(R._rng)
    random.seed(12345)
    x = [R.new_block() for _ in range(30)]
    random.seed(12345)
    y = [R.new_block() for _ in range(30)]
    assert x != y, "blocks are reproducible from random.seed()"
    print("PASS: blocks come from OS entropy; random.seed() cannot reproduce them.")


def test_test_runs_do_not_consume_real_blocks():
    with db.get_db() as conn:
        before = conn.execute("SELECT COUNT(*) FROM allocation_slots WHERE pool='real' AND participant_id IS NOT NULL").fetchone()[0]
    for _ in range(9):
        deal("yes", pool="test")
    with db.get_db() as conn:
        after = conn.execute("SELECT COUNT(*) FROM allocation_slots WHERE pool='real' AND participant_id IS NOT NULL").fetchone()[0]
        t = conn.execute("SELECT COUNT(*) FROM allocation_slots WHERE pool='test' AND participant_id IS NOT NULL").fetchone()[0]
    assert before == after and t == 9
    print("PASS: 9 test-run draws used only the 'test' pool; the real pool was untouched.")


def test_repeat_claim_returns_same_slot():
    pid = new_participant()
    first = db.claim_slot(pid, "yes", "real")
    again = [db.claim_slot(pid, "yes", "real") for _ in range(5)]
    assert all(a == first for a in again)
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM allocation_slots WHERE participant_id = ?", (pid,)).fetchone()[0] == 1
    print("PASS: claiming again returns the same slot; exactly one slot is ever held.")


if __name__ == "__main__":
    test_signature_cannot_carry_free_text()
    test_stance_must_be_exactly_yes_or_no()
    test_blocks_are_balanced_and_all_orders_occur()
    test_stratified_balance_in_every_block()
    test_draw_ignores_everything_but_stance()
    test_entropy_source_not_seedable()
    test_test_runs_do_not_consume_real_blocks()
    test_repeat_claim_returns_same_slot()
    print("\nAll randomization tests passed.")
