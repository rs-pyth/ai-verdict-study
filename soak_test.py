"""
Soak test: run flagged TEST participants through the live study end to end and
report how the verdict loop actually behaves (match rate, retries, fallbacks,
word counts, model names).

Every participant is a test run: id 'test-soak-<uuid>', is_test = 1, drawn from
the separate 'test' allocation pool, excluded from the real export. Each one
uses the real Gemini quota (1-3 calls each, plus retries after API errors), so
mind the free-tier limits (5/min, 20/day): the default is 4 participants,
~15 s apart.

Secrets come from YOUR environment, never from arguments or files:
    TEST_MODE_TOKEN        required: the same value you set on Render
    ADMIN_EXPORT_PASSWORD  optional: lets the script download the results
                           (the participants export and the per-call verdict log)
                           and print the summary. Without it you get the ids to
                           look up yourself.

Usage:
    TEST_MODE_TOKEN=... ADMIN_EXPORT_PASSWORD=... python soak_test.py --n 4
    python soak_test.py --base-url http://127.0.0.1:5050 --n 2     # a local server
"""

import argparse
import csv
import io
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_URL = "https://ai-verdict-study.onrender.com"

# Varied on purpose, including vague and one-word rationales, which are the
# ones that made the earlier prompt miss its target in the pilot.
RATIONALES = {
    "yes": [
        "The stock is up 40% and they won a big government contract, so growth looks strong to me.",
        "Revenue is growing and the CEO is planning more government deals, so I would invest.",
        "Good momentum.",
        "I think the company is doing well and the contract shows the product is trusted, so it is a reasonable bet for part of my savings.",
    ],
    "no": [
        "Too risky.",
        "The original subscription product only grew 3% while the industry grew 12%, so the core business looks weak.",
        "One contract is 35% of revenue, which seems like a lot to depend on, so I would stay away.",
        "No.",
    ],
}


def call(base, path, body=None, headers=None, timeout=150, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method or ("POST" if data is not None else "GET"),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:  # connection problems, timeouts
        return 0, f"{type(e).__name__}: {e}"


def post_json(base, path, body, headers=None, timeout=150):
    status, text = call(base, path, body, headers, timeout)
    try:
        return status, json.loads(text)
    except ValueError:
        return status, {"raw": text[:200]}


def run_participant(base, token, stance, rationale):
    pid = f"test-soak-{uuid.uuid4()}"
    out = {"pid": pid, "stance": stance, "rationale": rationale}
    st, j = post_json(base, "/api/consent", {"participant_id": pid, "recruitment_source": "soak", "test_token": token})
    if st != 200 or j.get("is_test") is not True:
        out["error"] = f"consent failed ({st}, {j}); NOT continuing: the run must be flagged as a test"
        return out
    st, j = post_json(base, "/api/covariates", {
        "participant_id": pid, "ai_use_frequency": 3, "tech_affinity": [4, 4, 4], "fin_lit_correct": [True, True, True],
        "fin_lit_self_rating": 4, "age": 17, "grade_year": "11th grade", "gender": "prefer_not_to_say"})
    if st != 200:
        out["error"] = f"covariates failed ({st}, {j})"
        return out
    st, j = post_json(base, "/api/submit-stance", {"stance": stance, "confidence": 5, "rationale": rationale},
                      headers={"X-Participant-Id": pid})
    if st != 200:
        out["error"] = f"submit-stance failed ({st}, {j})"
        return out
    t0 = time.time()
    st, j = post_json(base, "/api/generate-verdict", {"participant_id": pid})
    out["verdict_status"], out["verdict_seconds"] = st, round(time.time() - t0, 1)
    if st != 200:
        out["error"] = f"generate-verdict failed ({st}, {j})"
        return out
    out["verdict"] = j.get("verdict", "")
    st1, _ = post_json(base, "/api/post-verdict", {
        "participant_id": pid, "stance": stance, "confidence": 5, "rationale": "soak test follow-up",
        "trust_items": [4] * 12, "competence_items": [4, 4, 4, 4], "bias_items": [4, 4, 4],
        "intention_items": [4, 4], "attention_check_response": 5})
    st2, _ = post_json(base, "/api/debrief-feedback", {"participant_id": pid, "feedback": ""})
    if st1 != 200 or st2 != 200:
        out["error"] = f"post-verdict/debrief failed ({st1}, {st2})"
    return out


def fetch_csv(base, password, query):
    status, text = call(base, "/admin/export?" + urllib.parse.urlencode({"password": password}) + query)
    if status != 200:
        raise RuntimeError(f"export failed with HTTP {status}")
    return list(csv.DictReader(io.StringIO(text)))


def summarize(base, password, results):
    pids = {r["pid"] for r in results}
    raw = {p["id"]: p for p in fetch_csv(base, password, "&format=raw&include_test=1") if p["id"] in pids}
    attempts = [a for a in fetch_csv(base, password, "&format=attempts&include_test=1") if a["participant_id"] in pids]

    print("\n=== Per participant ===")
    print(f"{'stance':6} {'condition':10} {'target':14} {'tries':5} {'fallback':9} {'reason':20} {'apierr':6} {'words':5}")
    words, first_try, fallbacks, errs = [], 0, 0, 0
    for r in results:
        p = raw.get(r["pid"])
        if not p:
            print(f"{r['stance']:6} (no row found for {r['pid']})")
            continue
        fb = p["verdict_fallback_used"] == "1"
        w = int(p["ai_verdict_word_count"]) if p["ai_verdict_word_count"] else None
        words.append(w) if w else None
        first_try += (p["verdict_attempts"] == "1" and not fb)
        fallbacks += fb
        errs += int(p["verdict_api_error_count"] or 0)
        print(f"{p['pre_stance']:6} {p['condition']:10} {p['verdict_target']:14} {p['verdict_attempts']:5} "
              f"{'YES' if fb else 'no':9} {p['verdict_fallback_reason']:20} {p['verdict_api_error_count']:6} {w if w else '':5}")

    missing = [r["pid"] for r in results if r["pid"] in raw and not raw[r["pid"]]["ai_verdict_text"]]
    if missing:
        print("\n=== Participants with NO stored verdict (what the server logged for each) ===")
        for pid in missing:
            p = raw[pid]
            print(f"{pid}: stance={p['pre_stance']} condition={p['condition']} "
                  f"stance_recorded={p['stance_recorded_at']} verdict_requested={p['verdict_requested_at'] or 'NEVER'} "
                  f"created={p['created_at']}")
            mine = [a for a in attempts if a["participant_id"] == pid]
            if not mine:
                print("   no model calls were logged: the verdict request either never reached the model "
                      "(never sent, or failed before the first call) or was cut off before the first call finished")
            for a in mine:
                print(f"   call {a['seq']}: {a['kind']} outcome={a['outcome']} error={a['error_kind'] or '-'} "
                      f"latency={a['latency_ms']}ms {a['error_detail'][:140]}")

    gens = [a for a in attempts if a["kind"] == "generation"]
    by_outcome = {}
    for a in gens:
        by_outcome[a["outcome"]] = by_outcome.get(a["outcome"], 0) + 1
    api = [a for a in attempts if a["kind"] == "api_error"]
    n = len(results)
    print("\n=== Summary ===")
    print(f"participants run: {n}   first-try match: {first_try}/{n}   fallback served: {fallbacks}/{n}")
    print(f"generations: {len(gens)}   outcomes: {by_outcome or '-'}")
    print(f"  (match = accepted; mismatch = wrong verdict; malformed = not the required JSON; inconsistent = opener contradicts the field)")
    print(f"API errors: {len(api)}   kinds: {sorted({a['error_kind'] for a in api}) or '-'}")
    print(f"models reported by the API: {sorted({a['model'] for a in gens if a['model']}) or '-'}   "
          f"temperatures: {sorted({a['temperature'] for a in gens if a['temperature']!=''}) or '-'}")
    if words:
        inside = sum(80 <= w <= 100 for w in words)
        print(f"word count of served verdicts: min {min(words)}, mean {statistics.mean(words):.1f}, max {max(words)}; "
              f"{inside}/{len(words)} inside 80-100")
    lat = [int(a["latency_ms"]) for a in gens if a["latency_ms"]]
    if lat:
        print(f"model latency per generation: median {statistics.median(lat)/1000:.1f}s, max {max(lat)/1000:.1f}s")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=DEFAULT_URL)
    ap.add_argument("--n", type=int, default=4, help="participants to run (default 4)")
    ap.add_argument("--pause", type=float, default=15, help="seconds between participants (default 15)")
    ap.add_argument("--yes", action="store_true", help="skip the quota confirmation")
    ap.add_argument("--summarize-only", action="store_true",
                    help="run nothing: summarise the soak participants ('test-soak-...') already on the server "
                         "(needs ADMIN_EXPORT_PASSWORD only, uses no model quota)")
    a = ap.parse_args()
    base = a.base_url.rstrip("/")
    token = os.environ.get("TEST_MODE_TOKEN")
    password = os.environ.get("ADMIN_EXPORT_PASSWORD")
    if a.summarize_only:
        if not password:
            sys.exit("Set ADMIN_EXPORT_PASSWORD in your environment.")
        try:
            rows = [p for p in fetch_csv(base, password, "&format=raw&include_test=1") if p["id"].startswith("test-soak-")]
        except Exception as e:
            sys.exit(f"Could not download results: {e}")
        if not rows:
            sys.exit("No test-soak participants found on the server.")
        summarize(base, password, [{"pid": p["id"], "stance": p["pre_stance"]} for p in rows])
        return
    if not token:
        sys.exit("Set TEST_MODE_TOKEN in your environment (the value you put on Render).")
    if a.n < 1 or a.n > 20:
        sys.exit("--n must be 1-20")

    st, _ = call(base, "/?test=" + urllib.parse.quote(token), timeout=90)
    if st != 200:
        sys.exit(f"The server did not accept the test token (HTTP {st}). Is TEST_MODE_TOKEN set on the server and deployed?")

    print(f"{a.n} test participants against {base}: about {a.n}-{3 * a.n} model calls "
          f"(free tier: 5/min, 20/day).")
    if not a.yes and input("Continue? [y/N] ").strip().lower() != "y":
        sys.exit("Cancelled.")

    results, failures = [], 0
    for i in range(a.n):
        stance = "yes" if i % 2 == 0 else "no"
        rationale = RATIONALES[stance][(i // 2) % len(RATIONALES[stance])]
        r = run_participant(base, token, stance, rationale)
        results.append(r)
        ok = "error" not in r
        failures = 0 if ok else failures + 1
        print(f"[{i + 1}/{a.n}] {r['pid']}  stance={stance}  {'OK' if ok else 'FAILED: ' + r['error']}"
              + (f"  ({r.get('verdict_seconds')}s)" if ok else ""))
        if ok:
            print("      \"" + r["verdict"][:160] + ("..." if len(r["verdict"]) > 160 else "") + "\"")
        if failures >= 3:
            print("Three failures in a row: stopping.")
            break
        if i < a.n - 1:
            time.sleep(a.pause)

    if password:
        try:
            summarize(base, password, [r for r in results if r.get("verdict_status") == 200 or "error" not in r])
        except Exception as e:
            print(f"\nCould not download results: {e}")
    else:
        print("\nADMIN_EXPORT_PASSWORD not set: no summary. Download "
              "/admin/export?format=raw&include_test=1 and ?format=attempts&include_test=1 yourself; "
              "the participant ids are:")
        for r in results:
            print("  ", r["pid"])
    print("\nThese were TEST rows (is_test = 1). Delete them with cleanup_synthetic_rows.sql before launch.")


if __name__ == "__main__":
    main()
