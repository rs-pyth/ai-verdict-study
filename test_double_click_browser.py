"""
Real-browser double-/triple-click test (Playwright + headless Chromium).

Drives the actual page with real mouse clicks and asserts that the condition is
drawn exactly ONCE per participant, whatever the browser sends:

  (the test server answers the stance request slowly, like a real network,
   so the 2nd and 3rd click land while the first request is still in flight)
  A. triple-click on the page as shipped          -> 1 stance request
  B. double-click on the page as shipped          -> 1 stance request
  C. triple-click with the page's click guard REMOVED (so the browser really
     sends 3 requests) -> the server still draws once. This also proves the
     test can fail: if the server redrew, the DB check would catch it.

Each case ends by inspecting the database itself: one slot claimed, one
condition, set after the stance. It uses a throwaway database and a server
with the AI client switched off, so it never touches data/study.db, Turso or
the Gemini quota.

Setup (dev only, not needed on Render):
    pip install -r requirements-dev.txt && python -m playwright install chromium
Run:  python test_double_click_browser.py
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import libsql
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = "browser-test-token-not-a-secret"

SERVER = r"""
import os, sys
sys.path.insert(0, {here!r}); os.chdir({here!r})
os.environ.pop("TURSO_DATABASE_URL", None)
import db
db.DB_PATH = {dbpath!r}
import app as A
A.TEST_MODE_TOKEN = {token!r}
A.ai_client = None                      # never call the real model
import time
@A.app.before_request
def slow_stance_endpoint():
    # A real network is slower than localhost. Without a delay the page has already moved on
    # to the next step before the 2nd/3rd click lands, and the click guard is never exercised.
    if A.request.path == "/api/submit-stance":
        time.sleep(0.8)
@A.app.after_request
def log(resp):
    with open({logpath!r}, "a") as fh:
        fh.write(f"{{A.request.method}} {{A.request.path}} {{resp.status_code}}\n")
    return resp
A.app.run(port={port}, debug=False, threaded=True)
"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(tmp):
    port, dbpath, logpath = free_port(), os.path.join(tmp, "t.db"), os.path.join(tmp, "requests.log")
    open(logpath, "w").close()
    code = SERVER.format(here=HERE, dbpath=dbpath, token=TOKEN, logpath=logpath, port=port)
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/?test={TOKEN}", timeout=1)
            return proc, port, dbpath, logpath
        except Exception:
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError("test server did not start")


def posts(logpath, path):
    return sum(1 for line in open(logpath) if line.startswith(f"POST {path} "))


def db_rows(dbpath, pid):
    c = libsql.connect(dbpath)
    try:
        part = c.execute("SELECT condition, pre_stance, stance_recorded_at, condition_assigned_at "
                         "FROM participants WHERE id = ?", (pid,)).fetchall()
        slots = c.execute("SELECT condition FROM allocation_slots WHERE participant_id = ?", (pid,)).fetchall()
        return part, slots
    finally:
        c.close()


def pick(page, name, value):
    page.click(f'label:has(input[name="{name}"][value="{value}"])')


def reach_step2(browser, port, stance):
    """A fresh participant who has done consent and covariates, through the real UI."""
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(f"http://127.0.0.1:{port}/?test={TOKEN}")
    page.click("#step-0 input[type=checkbox]")
    page.click("#consent-continue")
    page.wait_for_selector("#step-1", state="visible")
    pick(page, "ai_use_frequency", 3)
    for n in ("tech_affinity_1", "tech_affinity_2", "tech_affinity_3"):
        pick(page, n, 4)
    for n in ("fin_lit_0", "fin_lit_1", "fin_lit_2"):
        pick(page, n, 0)
    pick(page, "fin_lit_self_rating", 4)
    page.locator('input[name="gender"]').first.check(force=True)
    page.fill("#age", "17")
    page.fill("#grade_year", "11th grade")
    page.click("#step1-continue")
    page.wait_for_selector("#step-2", state="visible")
    pick(page, "pre_stance", stance)
    pick(page, "pre_confidence", 5)
    page.fill("#pre_rationale", "The company has strong revenue growth and a clear plan to expand into "
                                "new markets over the next year, so my decision follows from that.")
    pid = page.evaluate("participantId")
    assert pid.startswith("test-"), pid
    return ctx, page, pid


def settle(page):
    page.wait_for_timeout(2500)


def assert_drawn_once(dbpath, pid, stance):
    part, slots = db_rows(dbpath, pid)
    assert len(part) == 1, part
    cond, st, rec_at, drawn_at = part[0]
    assert st == stance and cond in ("agree", "contradict"), part
    assert len(slots) == 1 and slots[0][0] == cond, f"slots held: {slots}"
    assert rec_at <= drawn_at, "stance must be recorded before the draw"
    return cond


def main():
    tmp = tempfile.mkdtemp(prefix="browsertest-")
    proc, port, dbpath, logpath = start_server(tmp)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()

            # A. triple-click, page as shipped
            ctx, page, pid = reach_step2(browser, port, "yes")
            before = posts(logpath, "/api/submit-stance")
            page.click("#step2-continue", click_count=3, delay=30)
            settle(page)
            n = posts(logpath, "/api/submit-stance") - before
            assert n == 1, f"triple-click sent {n} stance requests"
            assert_drawn_once(dbpath, pid, "yes")
            ctx.close()
            print("PASS: A. real triple-click -> 1 stance request; one slot, one condition, stance before draw.")

            # B. double-click, page as shipped
            ctx, page, pid = reach_step2(browser, port, "no")
            before = posts(logpath, "/api/submit-stance")
            page.dblclick("#step2-continue", delay=30)
            settle(page)
            n = posts(logpath, "/api/submit-stance") - before
            assert n == 1, f"double-click sent {n} stance requests"
            assert_drawn_once(dbpath, pid, "no")
            ctx.close()
            print("PASS: B. real double-click -> 1 stance request; one slot, one condition.")

            # C. click guard removed: the browser really sends 3 requests
            ctx, page, pid = reach_step2(browser, port, "yes")
            page.evaluate("""() => {
                const old = document.getElementById('step2-continue');
                const b = old.cloneNode(true); old.replaceWith(b);      // drops the guarded listener
                b.addEventListener('click', () => fetch('/api/submit-stance', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json', 'X-Participant-Id': participantId},
                    body: JSON.stringify({stance: 'yes', confidence: 5, rationale: document.getElementById('pre_rationale').value}),
                }));
            }""")
            before = posts(logpath, "/api/submit-stance")
            page.click("#step2-continue", click_count=3, delay=30)
            settle(page)
            n = posts(logpath, "/api/submit-stance") - before
            assert n == 3, f"with the guard removed the browser should send 3 requests, sent {n}"
            assert_drawn_once(dbpath, pid, "yes")
            ctx.close()
            print("PASS: C. guard removed -> browser sent 3 stance requests; the server still drew once.")
            browser.close()
        print("\nAll browser double-click tests passed.")
    finally:
        proc.kill()


if __name__ == "__main__":
    main()
