"""
Tests for the verdict verification loop (verdicts.py) and the
/api/generate-verdict route that uses it.

No network and no API quota: the model is a fake, the clock is fake, and the
database is a throwaway file. Run with:  python test_verdicts.py
"""

import json
import os
import tempfile

os.environ.pop("TURSO_DATABASE_URL", None)

import libsql

import db

_tmp = tempfile.mkdtemp(prefix="verdicts-")
db.DB_PATH = os.path.join(_tmp, "t.db")

import app as A
import config
import verdicts
from verdicts import ModelAPIError

S, N = verdicts.SUPPORTED, verdicts.NOT_SUPPORTED


# ---------------------------------------------------------------- helpers
def prose(field, words=90):
    opener = {"supported": "The participant's reasoning is supported by the facts.",
              "not_supported": "The participant's reasoning is not supported by the facts."}[field]
    body = " ".join(f"word{i}" for i in range(words - len(opener.split())))
    return f"{opener} {body}."  # trailing '.' glued to the last filler word


def answer(field, opener_field=None, words=90):
    """What the model returns: JSON with `verdict` and prose whose opener may disagree."""
    return json.dumps({"verdict": field, "prose": prose(opener_field or field, words)})


class FakeWorld:
    """A fake clock plus a scripted model. Each script item is a raw string
    (the answer), or a ModelAPIError (raised), or ('slow', seconds, item)."""

    def __init__(self, script, latency=1.0):
        self.t, self.script, self.latency, self.calls, self.timeouts = 0.0, list(script), latency, 0, []

    def clock(self): return self.t
    def sleep(self, s): self.t += s

    def call_model(self, prompt, timeout):
        self.calls += 1
        self.timeouts.append(timeout)
        item = self.script.pop(0) if self.script else ModelAPIError("overloaded", "script exhausted")
        self.t += self.latency
        if isinstance(item, ModelAPIError):
            raise item
        return item, "fake-model-1"

    def run(self, target=S, max_attempts=3, budget_s=90, on_record=None):
        return verdicts.run_loop(target=target, build_prompt=lambda: "PROMPT", call_model=self.call_model,
                                 max_attempts=max_attempts, budget_s=budget_s, temperature=0,
                                 clock=self.clock, sleep=self.sleep, on_record=on_record)


# ---------------------------------------------------------------- loop tests
def test_match_on_first_attempt():
    w = FakeWorld([answer(S)])
    r = w.run(S)
    assert r["accepted"] and r["generations"] == 1 and r["api_errors"] == 0 and r["reason"] is None
    assert r["fields"] == [S] and r["word_count"] == 90 and w.calls == 1
    print("PASS: a matching first generation is accepted after 1 attempt.")


def test_mismatch_then_match_regenerates():
    w = FakeWorld([answer(N), answer(S)])
    r = w.run(S)
    assert r["accepted"] and r["generations"] == 2 and r["fields"] == [N, S]
    assert [x["outcome"] for x in r["records"]] == ["mismatch", "match"]
    print("PASS: a mismatch is regenerated; the field returned by each attempt is kept in order.")


def test_three_mismatches_give_up_with_exactly_three_calls():
    w = FakeWorld([answer(N)] * 5)
    r = w.run(S)
    assert not r["accepted"] and r["reason"] == "verification_failed" and r["generations"] == 3 and w.calls == 3
    assert r["fields"] == [N, N, N]
    print("PASS: 3 mismatches -> give up after exactly 3 calls (verification_failed).")


def test_bad_answers_count_as_attempts_and_are_never_accepted():
    cases = {
        "not json": "Sure! The participant's reasoning is supported by the facts.",
        "wrong keys": json.dumps({"verdict": S, "text": "x"}),
        "bad enum": json.dumps({"verdict": "agree", "prose": prose(S)}),
        "empty prose": json.dumps({"verdict": S, "prose": "  "}),
        "markdown": json.dumps({"verdict": S, "prose": "**" + prose(S) + "**"}),
        "no opener": json.dumps({"verdict": S, "prose": "Meridian facts here. " * 20}),
        "field vs opener": answer(S, opener_field=N),
        "none content": "",
    }
    for name, raw in cases.items():
        w = FakeWorld([raw] * 3)
        r = w.run(S)
        assert not r["accepted"] and r["generations"] == 3 and w.calls == 3, name
    assert verdicts.parse_generation(cases["bad enum"])["verdict_field"] == "agree"
    assert verdicts.parse_generation(json.dumps({"verdict": 7, "prose": "x"}))["verdict_field"] is None
    print(f"PASS: {len(cases)} kinds of bad answer each count as an attempt and are never accepted.")


def test_code_fences_are_tolerated():
    w = FakeWorld(["```json\n" + answer(S) + "\n```"])
    assert w.run(S)["accepted"]
    print("PASS: a ```json fence around the object is tolerated.")


def test_api_errors_do_not_count_as_attempts():
    w = FakeWorld([ModelAPIError("overloaded", "503")] * 4 + [answer(S)])
    r = w.run(S)
    assert r["accepted"] and r["generations"] == 1 and r["api_errors"] == 4 and w.calls == 5
    kinds = [x["outcome"] for x in r["records"]]
    assert kinds == ["api_error"] * 4 + ["match"]
    w = FakeWorld([answer(N), ModelAPIError("overloaded", "503"), answer(N), ModelAPIError("rate_limited", "429"), answer(N)])
    r = w.run(S)
    assert not r["accepted"] and r["generations"] == 3 and r["api_errors"] == 2 and r["reason"] == "verification_failed"
    print("PASS: API errors are logged but never consume one of the 3 attempts.")


def test_fatal_api_errors_stop_immediately():
    for kind, reason in (("daily_quota", "daily_quota"), ("bad_request", "api_error_fatal")):
        w = FakeWorld([ModelAPIError(kind, "x"), answer(S)])
        r = w.run(S)
        assert not r["accepted"] and r["reason"] == reason and w.calls == 1 and r["generations"] == 0, kind
    print("PASS: daily-quota and bad-request errors stop the loop after one call.")


def test_time_budget_is_respected():
    w = FakeWorld([ModelAPIError("overloaded", "503")] * 100, latency=10.0)
    r = w.run(S, budget_s=90)
    assert not r["accepted"] and r["reason"] == "time_budget" and r["generations"] == 0 and r["api_errors"] >= 5
    assert w.t <= 90 + 1e-9, f"clock ran to {w.t}"
    assert all(t >= verdicts.MIN_CALL_S for t in w.timeouts), w.timeouts
    assert all(b <= a for a, b in zip(w.timeouts, w.timeouts[1:])), "per-call timeout must shrink with the remaining budget"
    print(f"PASS: with endless 503s the loop stops at the 90s budget (clock {w.t:.0f}s, {w.calls} calls, last timeout {w.timeouts[-1]:.0f}s).")


def test_every_call_is_recorded_immediately_even_if_the_loop_crashes():
    seen = []
    def boom(prompt, timeout):
        if len(seen) >= 2:
            raise RuntimeError("worker died")
        return answer(N), "m"
    try:
        verdicts.run_loop(target=S, build_prompt=lambda: "P", call_model=boom, max_attempts=3, budget_s=90,
                          clock=lambda: 0.0, sleep=lambda s: None, on_record=seen.append)
    except RuntimeError:
        pass
    assert [r["generation_no"] for r in seen] == [1, 2], seen
    print("PASS: records were handed to the logger as each call finished, before the crash.")


def test_word_count_and_model_are_recorded():
    r = FakeWorld([answer(S, words=97)]).run(S)
    rec = r["records"][0]
    assert rec["word_count"] == 97 == len(r["prose"].split()) and rec["model"] == "fake-model-1" and rec["temperature"] == 0
    print("PASS: actual word count, model name and temperature are recorded per attempt.")


def test_fallback_validation():
    good = {("yes", "agree"): prose(S, 90), ("yes", "contradict"): prose(N, 85),
            ("no", "agree"): prose(S, 95), ("no", "contradict"): prose(N, 88)}
    # make them distinct from each other
    good[("no", "agree")] = good[("no", "agree")].replace("word1 ", "other ")
    assert verdicts.validate_fallbacks(good, config.TARGET_VERDICT, 80, 100) == []
    bad = dict(good)
    bad[("yes", "agree")] = None
    bad[("yes", "contradict")] = prose(N, 60)                       # too short
    bad[("no", "agree")] = prose(N, 90)                           # wrong direction for 'agree'
    problems = verdicts.validate_fallbacks(bad, config.TARGET_VERDICT, 80, 100)
    assert any("yes/agree: not written yet" in p for p in problems)
    assert any("yes/contradict: 60 words" in p for p in problems)
    assert any("no/agree" in p and "verdict sentence" in p or "disagrees" in p for p in problems), problems
    print("PASS: fallback validation flags missing, wrong-length and wrong-direction texts.")


# ---------------------------------------------------------------- route tests
A.TEST_MODE_TOKEN = "route-test-token"
A.ai_client = object()  # non-None so the route proceeds; call_model is replaced per test
client = A.app.test_client()
_n = [0]
FALLBACKS = {("yes", "agree"): prose(S, 90).replace("word1 ", "fbA "), ("yes", "contradict"): prose(N, 90).replace("word1 ", "fbB "),
             ("no", "agree"): prose(S, 90).replace("word1 ", "fbC "), ("no", "contradict"): prose(N, 90).replace("word1 ", "fbD ")}


def raw(sql, params=()):
    conn = libsql.connect(db.DB_PATH)
    try:
        cur = conn.execute(sql, params)
        rows = cur.fetchall() if cur.description else None
        conn.commit()
        return rows
    finally:
        conn.close()


def make(condition="agree", stance="yes", rationale="Because the stock is up."):
    """A participant with a FORCED condition. The condition is written straight
    to the row (the first write is allowed; it is write-once afterwards),
    because these tests are about verdict generation, not the draw."""
    _n[0] += 1
    pid = f"test-route-{_n[0]}"
    assert client.post("/api/consent", json={"participant_id": pid, "test_token": A.TEST_MODE_TOKEN}).status_code == 200
    raw("UPDATE participants SET pre_stance=?, pre_confidence=5, pre_rationale=?, condition=? WHERE id=?",
        (stance, rationale, condition, pid))
    return pid


def generate(pid, model, fallbacks=FALLBACKS):
    A.call_model = model
    config.FALLBACK_VERDICTS = fallbacks
    return client.post("/api/generate-verdict", json={"participant_id": pid})


def row(pid):
    return db.get_participant(pid)


def attempts(pid):
    return raw("SELECT seq, kind, generation_no, outcome, verdict_field, word_count, model, error_kind FROM verdict_attempts WHERE participant_id=? ORDER BY seq", (pid,))


def test_route_verified_generation_is_stored_and_logged():
    pid = make("agree", "yes")
    seen = []
    def model(prompt, timeout):
        seen.append(prompt)
        return answer(S, words=92), "gemini-x"
    r = generate(pid, model)
    x = row(pid)
    assert r.status_code == 200 and r.get_json() == {"verdict": x["ai_verdict_text"]}
    assert (x["verdict_target"], x["verdict_attempts"], x["verdict_fallback_used"], x["verdict_fallback_reason"]) == (S, 1, 0, None)
    assert x["ai_verdict_word_count"] == 92 and json.loads(x["verdict_fields"]) == [S] and x["verdict_api_error_count"] == 0
    assert attempts(pid) == [(1, "generation", 1, "match", S, 92, "gemini-x", None)]
    assert "VERDICT TO REPORT: supported" in seen[0] and config.SCENARIO_FACTS_GROWTH.strip() in seen[0]
    print("PASS: route stores the verified prose + attempt count, fields, word count, target, and one attempt row.")


def test_route_tells_the_model_the_right_target_and_facts_for_every_cell():
    expect = {("yes", "agree"): ("supported", config.SCENARIO_FACTS_GROWTH), ("yes", "contradict"): ("not_supported", config.SCENARIO_FACTS_RISK),
              ("no", "agree"): ("supported", config.SCENARIO_FACTS_RISK), ("no", "contradict"): ("not_supported", config.SCENARIO_FACTS_GROWTH)}
    for (stance, cond), (target, facts) in expect.items():
        pid = make(cond, stance, rationale=f"MY-REASON-{stance}-{cond}")
        seen = []
        generate(pid, lambda p, timeout: (seen.append(p), (answer(target), "m"))[1])
        prompt = seen[0]
        assert f"VERDICT TO REPORT: {target}\n\nThis verdict is fixed" in prompt, (stance, cond)
        assert facts.strip() in prompt and f"MY-REASON-{stance}-{cond}" in prompt
        other = config.SCENARIO_FACTS_RISK if facts is config.SCENARIO_FACTS_GROWTH else config.SCENARIO_FACTS_GROWTH
        assert other.strip().splitlines()[0] not in prompt
    print("PASS: all 4 stance x condition cells get the right target verdict and the right fact-set.")


def test_route_is_idempotent_and_never_regenerates():
    pid = make()
    calls = []
    r1 = generate(pid, lambda p, timeout: (calls.append(1), (answer(S, words=91), "m"))[1])
    before = (row(pid)["ai_verdict_text"], attempts(pid))
    r2 = generate(pid, lambda p, timeout: (calls.append(1), (answer(S, words=99), "m"))[1])
    r3 = generate(pid, lambda p, timeout: (calls.append(1), (answer(S, words=99), "m"))[1])
    assert len(calls) == 1 and r1.get_json() == r2.get_json() == r3.get_json()
    assert (row(pid)["ai_verdict_text"], attempts(pid)) == before
    print("PASS: repeated requests return the stored verdict; the model is called once and nothing is overwritten.")


def test_route_serves_fallback_after_three_mismatches_and_logs_it():
    pid = make("contradict", "no")
    r = generate(pid, lambda p, timeout: (answer(S), "m"))  # target is not_supported, model keeps saying supported
    x = row(pid)
    assert r.get_json() == {"verdict": FALLBACKS[("no", "contradict")]}, "response must not leak that a fallback was used"
    assert (x["verdict_fallback_used"], x["verdict_fallback_reason"], x["verdict_attempts"], x["verdict_target"]) == (1, "verification_failed", 3, N)
    assert json.loads(x["verdict_fields"]) == [S, S, S] and x["ai_verdict_text"] == FALLBACKS[("no", "contradict")]
    assert x["ai_verdict_word_count"] == len(FALLBACKS[("no", "contradict")].split()) and len(attempts(pid)) == 3
    print("PASS: 3 mismatches -> that cell's stored fallback is served and logged; the response reveals nothing.")


def test_route_api_errors_are_logged_but_not_counted():
    pid = make()
    script = [ModelAPIError("daily_quota", "PerDay limit")]
    def model(p, timeout):
        raise script[0]
    r = generate(pid, model)
    x = row(pid)
    assert r.status_code == 200 and x["verdict_fallback_used"] == 1 and x["verdict_fallback_reason"] == "daily_quota"
    assert (x["verdict_attempts"], x["verdict_api_error_count"]) == (0, 1)
    assert attempts(pid) == [(1, "api_error", None, "api_error", None, None, None, "daily_quota")]
    print("PASS: an API failure serves the fallback with its reason, 0 attempts counted, 1 API error noticed.")


def test_route_missing_fallback_fails_closed():
    pid = make("agree", "no")
    r = generate(pid, lambda p, timeout: (answer(N), "m"), fallbacks={k: None for k in FALLBACKS})
    assert r.status_code == 503 and r.get_json()["error"] == "fallback_unavailable"
    assert row(pid)["ai_verdict_text"] is None and len(attempts(pid)) == 3
    r = generate(pid, lambda p, timeout: (answer(S), "m"))  # fallbacks now configured and the model behaves
    assert r.status_code == 200 and row(pid)["verdict_attempts"] == 1
    print("PASS: with no fallback written the participant gets an error (no invented stand-in), and can retry.")


def test_route_first_writer_wins():
    pid = make()
    def model(p, timeout):
        db.save_verdict_result(pid, text="WRITTEN BY ANOTHER REQUEST", word_count=4, target=S, attempts=1,
                               fallback_used=False, fallback_reason=None, fields_json="[]", api_error_count=0, generated_at="t")
        return answer(S), "m"
    r = generate(pid, model)
    assert r.get_json() == {"verdict": "WRITTEN BY ANOTHER REQUEST"} and row(pid)["ai_verdict_text"] == "WRITTEN BY ANOTHER REQUEST"
    print("PASS: if another request stored a verdict first, it is kept and returned (no overwrite).")


def test_route_guards():
    r = client.post("/api/generate-verdict", json={"participant_id": "test-does-not-exist"})
    assert r.status_code == 400
    pid = "test-route-nostance"
    client.post("/api/consent", json={"participant_id": pid, "test_token": A.TEST_MODE_TOKEN})
    assert client.post("/api/generate-verdict", json={"participant_id": pid}).status_code == 400
    pid = make()
    saved, A.ai_client = A.ai_client, None
    try:
        r = client.post("/api/generate-verdict", json={"participant_id": pid})
        assert r.status_code == 500 and row(pid)["ai_verdict_text"] is None and not attempts(pid)
    finally:
        A.ai_client = saved
    print("PASS: unknown participant / missing stance -> 400; missing API key -> 500, never a silent fallback.")


def test_attempt_log_is_append_only():
    pid = make(); generate(pid, lambda p, timeout: (answer(S), "m"))
    try:
        raw("UPDATE verdict_attempts SET outcome = 'match' WHERE participant_id = ?", (pid,))
        raise AssertionError("update was allowed")
    except Exception as e:
        assert "append-only" in str(e), e
    print("PASS: verdict_attempts rows cannot be edited after the fact.")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nAll verdict tests passed.")
