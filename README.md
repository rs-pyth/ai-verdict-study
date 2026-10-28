# Investment Reasoning Study

Flask + libSQL (SQLite-compatible) web app for the AI-verdict study.
Single-page multi-step form; see `app.py` for routes, `db.py` for the data
model, and `randomization.py` for the condition-assignment logic (read
that one first — it's the part the whole study's validity depends on, and
`test_randomization.py` has the tests proving it).

## Before running a real study

Most content in `config.py` is filled in — consent text, scenario, debrief,
covariates, and all five DV scales. A few things still need a human, not code:

- **Review `SCENARIO_FACTS_GROWTH` and `SCENARIO_FACTS_RISK`** in
  `config.py` — drafted, not placeholders, but not vetted either. The AI
  genuinely audits the participant's reasoning against whichever fact-set
  the randomized condition and the participant's own stance select
  (`agree` = the set that backs their stance, `contradict` = the set that
  opposes it), rather than being told what to conclude — so these two
  fact-sets ARE the experimental manipulation. They're split by which
  true facts each includes (growth-case vs. risk-case), not by tone. Read
  the comment above them: it explains why the stance is part of the
  mapping (the first pilot showed the condition alone barely moved the
  verdict) and spells out a real consequence — a participant with
  genuinely good reasoning can still come back "unsupported" if they
  cited a fact outside their assigned set. That's fine for this study's
  actual question, but worth being explicit about in any write-up. Rows
  collected before 2026-09-21 used the old condition-only mapping and
  aren't comparable. Re-run the blind-audit pilot under the new mapping
  before real participants see this.
- **Have Ben review `VERDICT_PROMPT_TEMPLATE`** in `config.py` (v3: the model is told the target verdict, and the result is verified; approved by the researcher 2026-10-28, advisor review still pending), and the fact-set
  curation approach above, before either touches real participants.
- **Flag the minors / parental-consent question to Ben.** The consent
  checkbox gates on self-attested age 13+ (enforced again numerically in
  Step 1 — see `MINIMUM_AGE` in `config.py`), which is reasonable for
  classmates recruited under normal school-context norms, but if your
  recruitment pool goes beyond that (e.g. `?src=prolific` or any public
  link), that self-attestation is probably not enough on its own — this is
  a judgment call for your advisor's review, not something to default your
  way through in code.

## Confirmed design parameters (researcher, 2026-10-28)

Verdicts are written live by the model on demand (no pre-generated bank); the
four stored fallbacks are only a safety net. Condition labels `agree` /
`contradict`; stance-stratified permuted blocks of 4 (`perm_block4_v1`);
temperature 0; at most 3 generations (API errors don't count); 90 s budget;
80-100 word target (actual count recorded); agree -> `supported`, contradict ->
`not_supported`; completion time = active time (excludes the AI wait). Jian
item 2 stays reworded and is disclosed as a deviation. Not yet specified: the
speeder-exclusion floor (left unspecified on purpose; see below).

## Analysis reference: DV → hypothesis mapping

For your write-up, not enforced anywhere in code:

| Hypothesis | Primary DV(s) |
|---|---|
| H1 (sycophancy trap — trust tracks agreement, not correctness) | DV1 (Trust) |
| H2 (grounding buys trust back) | Out of scope for this two-cell (agree/contradict only) version — would need a third bare-vs-evidence factor. Say so explicitly in the write-up rather than dropping it silently. |
| H3 (bias blind spot) | DV4 (Perceived bias) |
| Exploratory: does disagreement reduce advice-uptake | DV3 — stance-switch rate (`pre_stance` vs. `post_stance`) and confidence delta (`pre_confidence` vs. `post_confidence`), broken out by `condition`. Not asked directly — compute from the CSV export. |
| Exploratory: disagreement → perceived competence, independent of trust | DV2 |
| Exploratory: downstream behavioral intent | DV5 |
| Moderator check | Covariates (financial literacy, prior AI use, tech affinity) as moderators of condition → trust. Gender (`gender`: woman / man / nonbinary / another / prefer_not_to_say) is collected the same way; with small samples the sparse categories will have very few rows, so decide how to collapse them before looking at results. Rows collected before this question was added have `gender` NULL. |

Scoring notes live as comments right above `TRUST_ITEMS` and `BIAS_ITEMS`
in `config.py` (which items are reverse-scored, which item to
report/analyze separately).

### Pre-registered exclusion rules

Applied identically across both conditions:

- **Attention check.** Item 13 in the trust grid (`config.ATTENTION_CHECK_ITEM`),
  stored in its own `attention_check_response` column — not part of the
  trust composite. Exclude any row where this doesn't equal
  `config.ATTENTION_CHECK_CORRECT_VALUE` (5, "Somewhat agree"). Not
  enforced live — a wrong answer is still recorded and the participant
  proceeds normally; the exclusion happens at analysis time, from the
  CSV export.
- **Completion-time floor.** Use `completion_time_s` in the export (active
  time: consent to debrief minus the wait for the AI verdict, so a slow API
  call doesn't change who is excluded). **The cutoff is deliberately left
  unspecified**: set it, and write it into the pre-registration, before
  looking at any real data.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit .env with your real GEMINI_API_KEY
                        # and a strong ADMIN_EXPORT_PASSWORD
python app.py
```

Visit `http://localhost:5050`. Add `?src=classmates` (or whatever pool
name) to tag recruitment source, e.g. `http://localhost:5050/?src=prolific`.

Without `TURSO_DATABASE_URL` set, `data/study.db` is created automatically
on first run and is git-ignored — don't commit it, it contains response
data. This local-file mode is only for development; see the deploy section
below for why production needs Turso instead.

## Export data

```
http://localhost:5050/admin/export?password=YOUR_ADMIN_EXPORT_PASSWORD
```

One CSV, one row per participant, **real participants only** by default (add
`&include_test=1` for test and legacy rows, told apart by `is_test`). Columns
are defined and documented in `analysis.py`: participant id, `is_test`,
stance, condition, `verdict_id` (+ `verdict_source` live/fallback, attempts,
word count), `trust_1`..`trust_12` raw, `attention_check_item13` raw, the
scored variables, pre/post confidence, three time columns and
`attention_check_pass`. Scoring (7-point items): `trust_composite` = mean of
items 1-11 with items 1-5 reversed (8 - x); `distrust_subscore` = mean of
items 1-5 **not** reversed; `trust_subscore` = mean of items 6-11;
`perceived_bias` = mean of the 3 bias items with item 2 reversed. Item 12
(familiarity) is in no composite. A score is blank unless every item feeding it
was answered; nothing is imputed. `verdict_id` is `fallback-<stance>-<condition>`
for the four stored fallbacks and `live-<hash of the text>` otherwise.
`completion_time_s` is **active time** = `total_time_s` (consent to debrief
submitted) minus `verdict_wait_s` (asking for the verdict to receiving it).
`?format=raw` returns the full database table (every column, free text
included) for auditing. Tests: `python test_export.py`.

## Test runs (the `is_test` flag)

Every row carries `is_test`: `1` = test/soak run, `0` = real participant,
`NULL` = legacy row from before the flag existed (never counts as real).
It is set once, at consent, and is **immutable**: database triggers
(`TRIGGERS` in `db.py`) refuse any change to it or to the participant id,
and refuse an insert whose flag disagrees with the id namespace. It is not
derived from `recruitment_source` (a URL parameter anyone can set) and
nothing is filtered at analysis time.

- A test run needs **both** `TEST_MODE_TOKEN` (an env var you set on the
  host; unset = test mode is disabled entirely) **and** a participant id
  starting `test-` (real browsers only ever generate bare UUIDs).
- In a browser, open `/?test=<token>`: a red TEST RUN banner shows, the id
  is a fresh `test-...` every load, and `localStorage` is never touched.
  A wrong or empty `?test=` is refused with a 403 rather than falling
  through to a normal (real) session.
- From a script, send `test_token` and a `test-` id to `/api/consent`; the
  response echoes `is_test`, so assert it is `true`.
- Every inconsistent combination (test id without token, token without test
  id, wrong token) is refused with a 400 and creates no row.
- Run `python test_test_flag.py` after touching any of this.

## Tests

Plain scripts, no pytest; each prints PASS lines and ends with "All ... passed".
They use throwaway databases and never touch `data/study.db`, Turso or Gemini.

```bash
python test_randomization.py        # stance-stratified permuted blocks, free text can't reach the draw
python test_double_submit.py        # repeated + concurrent submits (separate processes) -> one draw
python test_test_flag.py            # immutable is_test flag
python test_verdicts.py             # verification loop, retries, fallback, logging (fake model)
python test_export.py               # analysis CSV scoring and route
python test_double_click_browser.py # REAL double/triple clicks in headless Chromium (needs Playwright)
```

The browser test is dev-only: `pip install -r requirements-dev.txt && python -m
playwright install chromium` (Render installs `requirements.txt` only). It
makes the test server answer slowly so the 2nd/3rd click land while the first
request is in flight, then checks the database itself: one slot, one condition.

## Soak test (live verdict behaviour)

`soak_test.py` runs flagged test participants through a running server and
summarises match rate, retries, fallbacks, word counts and the model name the
API reported. Secrets come from your environment only:

```bash
TEST_MODE_TOKEN=... ADMIN_EXPORT_PASSWORD=... python soak_test.py --n 4
```

It uses real Gemini quota (1-3 calls per participant; free tier is 5/min and
20/day), so the default is 4 participants 15 s apart. The rows are test rows:
delete them with `cleanup_synthetic_rows.sql` before launch.
`/admin/export?format=attempts` is the per-call verdict log it reads.

## Storage: why Turso, not just a local file

Render's free tier (and most free hosts) has no persistent disk — local
files get wiped on every redeploy *and* on spin-down after ~15 minutes of
inactivity. That silently lost real participant data before this was
fixed: two test runs a redeploy apart ended up with only one row in the
export. `db.py` now connects directly to a remote
[Turso](https://turso.tech) database (SQLite-compatible, free tier, no
local file at all) whenever `TURSO_DATABASE_URL` is set, so every write
lands somewhere durable immediately instead of on Render's disk. Without
that env var set, it falls back to a local file — fine for development,
not for a real data-collection run.

To set up Turso: create a free account and a database at
[turso.tech](https://turso.tech), then grab the database URL (starts with
`libsql://`) and generate an auth token from its dashboard — both go in
your `.env` locally and in Render's environment variables for production.

## Deploy free (Render)

1. Push this repo to GitHub (data/ and .env are already git-ignored).
2. On [render.com](https://render.com), New → Web Service → connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app`
5. Add environment variables in the Render dashboard: `GEMINI_API_KEY`,
   `ADMIN_EXPORT_PASSWORD`, `CONTACT_EMAIL`, `TURSO_DATABASE_URL`,
   `TURSO_AUTH_TOKEN`. Optionally `AI_MODEL` if the default Gemini model
   slug in `app.py` is out of date — check
   [ai.google.dev/gemini-api/docs/models](https://ai.google.dev/gemini-api/docs/models)
   for the current list. (`CONTACT_EMAIL` isn't secret, but it's set via
   env var rather than hardcoded in `config.py` since that file is in a
   public repo.)
6. Without `TURSO_DATABASE_URL` set, the app still runs, but every
   redeploy or spin-down wipes all collected data — see the storage
   section above. Set it before collecting anything you care about.

## Notes on the design

- **Randomization** (`randomization.py`, `/api/submit-stance`, scheme
  `perm_block4_v1`). The order is: (1) validate and record the participant's
  stance (write-once); (2) draw the condition, **stratified on that stance**;
  (3) only later, serve the verdict. Within each stance, conditions are dealt
  from shuffled blocks of 4 (2 `agree` + 2 `contradict`), so the arms stay
  balanced within each stance throughout recruitment. The draw can see only the
  validated `yes`/`no` stance (read back from storage); the rationale,
  confidence and covariates never reach it. Blocks come from OS entropy and are
  stored in `allocation_slots`; a slot is claimed by one atomic UPDATE and
  belongs to one participant id forever, so a double-click, retry or second tab
  cannot cause a second draw. Triggers make `condition` and `pre_stance`
  write-once. Test runs (`is_test = 1`) draw from a separate `test` pool and
  never consume real participants' blocks. Logged on every row: `pre_stance`,
  `stance_recorded_at` (always <= `condition_assigned_at`), `assignment_scheme`,
  `assignment_stratum`, `assignment_block`, `assignment_position`. Tests:
  `python test_randomization.py`, `python test_double_submit.py`.
- **Verdicts are generated live, then verified** (`verdicts.py`,
  `/api/generate-verdict`). The model is told the target verdict for the
  assigned condition (`agree` -> `supported`, `contradict` -> `not_supported`)
  and returns JSON `{verdict, prose}` at temperature 0. A generation is
  accepted only if `verdict` equals the target and the prose's mandatory
  first sentence agrees with it; otherwise it is regenerated, up to 3
  generations. API errors (503/429/timeouts) are logged but do **not** count
  as attempts; the whole loop has a 90 s budget (under gunicorn's 120 s
  timeout). If verification fails 3 times, or the budget/quota runs out, the
  stored fallback for that (stance, condition) cell is served
  (`FALLBACK_VERDICTS` in `config.py`, written by the researcher; validate
  with `python check_fallbacks.py`) and the reason is logged. The response to
  the browser never says a fallback was used.
  Logged per participant on `participants`: `verdict_target`,
  `verdict_attempts`, `verdict_fallback_used` / `verdict_fallback_reason`,
  `verdict_fields` (the `verdict` returned by each generation, JSON),
  `verdict_api_error_count`, `ai_verdict_word_count`; and per call in the
  append-only `verdict_attempts` table (outcome, verdict field, prose, word
  count, the model name the API reported, temperature, latency, error kind).
  The endpoint is idempotent: a stored verdict is returned, never regenerated.
  Tests: `python test_verdicts.py` (fake model, no API quota).
- **Condition means agree / contradict the participant's own stance.** At
  verdict time (`/api/generate-verdict`) the stored condition plus the stored
  pre-stance pick the fact-set (`scenario_facts_for_condition()` in
  `app.py`), so `condition` means "audited against facts that back / oppose
  what the participant said" for everyone. Because the draw is stratified on
  stance, condition is balanced within each stance by design.
- **No PII**: participant IDs are random UUIDs generated in the browser
  (`static/js/app.js`) and never tied to a name/email. The server doesn't
  log IPs beyond Flask/Render's normal access logs.
- **Duplicate prevention**: a `localStorage` flag set only after the
  debrief step blocks re-entry from the same browser. It's not
  bulletproof (clearing storage or using another browser bypasses it) but
  matches the "simple" bar from the spec.
