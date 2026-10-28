"""
Tests for the analysis CSV (analysis.py + /admin/export): scoring checked
against hand-computed values, then the route end to end.
Run with:  python test_export.py
"""

import csv
import io
import os
import tempfile

os.environ.pop("TURSO_DATABASE_URL", None)

import analysis
import db

db.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="testexport-"), "t.db")

import app as A

A.ADMIN_EXPORT_PASSWORD = "pw"
A.TEST_MODE_TOKEN = "tok"


def base(**kw):
    p = {"id": "x", "is_test": 0, "pre_stance": "yes", "condition": "agree"}
    p.update(kw)
    return p


def test_trust_scoring_by_hand():
    # items 1-5 = 2,3,2,1,4 (distrust); items 6-11 = 6,5,7,6,5,6 (trust); 12 = 3 (familiarity)
    t = [2, 3, 2, 1, 4, 6, 5, 7, 6, 5, 6, 3]
    p = base(**{f"trust_{i + 1}": v for i, v in enumerate(t)})
    s = analysis.score(p)
    # reversed 1-5 = 6,5,6,7,4 -> sum 28; trust 6-11 = 35; composite = 63/11
    assert s["trust_composite"] == round(63 / 11, 4), s
    assert s["distrust_subscore"] == round(12 / 5, 4), s          # 2+3+2+1+4 = 12, NOT reversed
    assert s["trust_subscore"] == round(35 / 6, 4), s
    # the familiarity item must not move any score
    p2 = dict(p, trust_12=7)
    assert analysis.score(p2) == s
    print("PASS: composite 63/11, distrust 12/5 (not reversed), trust 35/6; item 12 changes nothing.")


def test_reverse_scoring_direction():
    allhigh = base(**{f"trust_{i}": 7 for i in range(1, 12)})
    distrusting = base(**{**{f"trust_{i}": 7 for i in range(1, 6)}, **{f"trust_{i}": 1 for i in range(6, 12)}})
    trusting = base(**{**{f"trust_{i}": 1 for i in range(1, 6)}, **{f"trust_{i}": 7 for i in range(6, 12)}})
    assert analysis.score(trusting)["trust_composite"] == 7.0
    assert analysis.score(distrusting)["trust_composite"] == 1.0
    assert analysis.score(allhigh)["trust_composite"] == round((5 * 1 + 6 * 7) / 11, 4)
    assert analysis.score(distrusting)["distrust_subscore"] == 7.0
    print("PASS: all-trusting answers -> 7.0, all-distrusting -> 1.0; distrust subscore reads 7.0 for max distrust.")


def test_bias_reverse_item_2():
    s = analysis.score(base(bias_1=6, bias_2=2, bias_3=5))   # 6, 8-2=6, 5 -> 17/3
    assert s["perceived_bias"] == round(17 / 3, 4), s
    print("PASS: perceived bias reverses item 2 only (6, 6, 5 -> 17/3).")


def test_missing_or_invalid_items_leave_blanks_not_guesses():
    full = {f"trust_{i}": 4 for i in range(1, 13)}
    for bad in (None, 0, 8, "4", 4.5, True):
        s = analysis.score(base(**{**full, "trust_3": bad}))
        assert s["trust_composite"] is None and s["distrust_subscore"] is None, (bad, s)
        assert s["trust_subscore"] == 4.0
    assert analysis.score(base(**{**full, "trust_12": None}))["trust_composite"] == 4.0
    assert analysis.score(base(bias_1=3, bias_2=None, bias_3=3))["perceived_bias"] is None
    print("PASS: a missing/invalid item blanks only the scores it feeds; nothing is imputed.")


def test_attention_check():
    assert analysis.attention_pass(base(attention_check_response=5)) == 1
    assert analysis.attention_pass(base(attention_check_response=4)) == 0
    assert analysis.attention_pass(base(attention_check_response=None)) is None
    print("PASS: attention check passes only on 5; blank when unanswered.")


def test_timing_excludes_the_ai_wait():
    p = base(consent_timestamp="2026-11-02T10:00:00+00:00", completed_at="2026-11-02T10:06:30+00:00",
             verdict_requested_at="2026-11-02T10:02:00+00:00", ai_verdict_generated_at="2026-11-02T10:02:45+00:00")
    t = analysis.timing(p)
    assert t == {"total_time_s": 390.0, "verdict_wait_s": 45.0, "completion_time_s": 345.0}, t
    assert analysis.timing(base(consent_timestamp="2026-11-02T10:00:00+00:00"))["completion_time_s"] is None
    print("PASS: 390 s total - 45 s waiting for the AI = 345 s completion time; blank if unknowable.")


def test_verdict_id():
    a = analysis.verdict_identity(base(ai_verdict_text="same text", verdict_fallback_used=0))
    b = analysis.verdict_identity(base(ai_verdict_text="same text", verdict_fallback_used=0))
    c = analysis.verdict_identity(base(ai_verdict_text="other", verdict_fallback_used=0))
    f = analysis.verdict_identity(base(ai_verdict_text="stored", verdict_fallback_used=1, pre_stance="no", condition="contradict"))
    assert a == b and a != c and a[1] == "live" and a[0].startswith("live-")
    assert f == ("fallback-no-contradict", "fallback")
    assert analysis.verdict_identity(base()) == (None, None)
    print("PASS: verdict_id is stable per text for live verdicts and names the cell for fallbacks.")


def test_route_end_to_end():
    client = A.app.test_client()

    def mk(pid, test, **cols):
        body = {"participant_id": pid}
        if test:
            body["test_token"] = A.TEST_MODE_TOKEN
        assert client.post("/api/consent", json=body).status_code == 200
        sets = ", ".join(f"{k}=?" for k in cols)
        with db.get_db() as conn:
            conn.execute(f"UPDATE participants SET {sets} WHERE id=?", (*cols.values(), pid))

    real = "11111111-1111-4111-8111-111111111111"
    cols = {f"trust_{i}": 4 for i in range(1, 13)}
    cols.update(pre_stance="no", condition="contradict", pre_confidence=6, post_confidence=3, bias_1=4, bias_2=4, bias_3=4,
                attention_check_response=5, ai_verdict_text="v", verdict_fallback_used=0, verdict_attempts=1,
                ai_verdict_word_count=90, completed_at="2099-01-01T00:00:00+00:00")
    mk(real, False, **cols)
    mk("test-ex-1", True, **cols)
    r = client.get("/admin/export?password=wrong")
    assert r.status_code == 401
    r = client.get("/admin/export?password=pw")
    rows = list(csv.DictReader(io.StringIO(r.get_data(as_text=True))))
    assert [x["participant_id"] for x in rows] == [real], rows
    x = rows[0]
    assert list(x) == analysis.COLUMNS
    assert (x["stance"], x["condition"], x["is_test"], x["trust_composite"], x["distrust_subscore"],
            x["perceived_bias"], x["pre_confidence"], x["post_confidence"], x["attention_check_pass"]) == \
           ("no", "contradict", "0", "4.0", "4.0", "4.0", "6", "3", "1")
    both = list(csv.DictReader(io.StringIO(client.get("/admin/export?password=pw&include_test=1").get_data(as_text=True))))
    assert {y["participant_id"]: y["is_test"] for y in both} == {real: "0", "test-ex-1": "1"}
    raw = client.get("/admin/export?password=pw&format=raw").get_data(as_text=True)
    assert "pre_rationale" in raw.splitlines()[0] and "pre_rationale" not in ",".join(analysis.COLUMNS)
    assert client.post("/admin/cleanup?password=pw").status_code in (404, 405), "temporary cleanup route must be gone"
    print("PASS: /admin/export = analysis columns, real rows only by default; include_test adds the flagged row; "
          "format=raw gives the full table; /admin/cleanup no longer exists.")


if __name__ == "__main__":
    test_trust_scoring_by_hand()
    test_reverse_scoring_direction()
    test_bias_reverse_item_2()
    test_missing_or_invalid_items_leave_blanks_not_guesses()
    test_attention_check()
    test_timing_excludes_the_ai_wait()
    test_verdict_id()
    test_route_end_to_end()
    print("\nAll export tests passed.")
