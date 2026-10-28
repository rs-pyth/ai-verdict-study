-- One-off: remove every synthetic / test / legacy row before real data collection.
-- Run it yourself in the Turso shell (or dashboard SQL console). Nothing in the app can do this.
--
-- ORDER (do not skip step 1):
--   1. ARCHIVE. Download the full table first, then keep the file somewhere safe:
--        https://ai-verdict-study.onrender.com/admin/export?password=...&format=raw&include_test=1
--      (needs the version with is_test, i.e. deploy first; the first request after
--       deploy adds the column, leaving every existing row NULL = "legacy, not real")
--   2. COUNT (record these numbers; they are the "rows removed" figure):

SELECT COALESCE(recruitment_source, '(none)') AS source,
       CASE WHEN is_test IS NULL THEN 'legacy (NULL)' WHEN is_test = 1 THEN 'test (1)' ELSE 'REAL (0)' END AS kind,
       COUNT(*) AS n
FROM participants GROUP BY 1, 2 ORDER BY 3 DESC;

SELECT COUNT(*) AS verdict_attempt_rows_to_remove
FROM verdict_attempts WHERE participant_id IN (SELECT id FROM participants WHERE is_test IS NOT 0);

--   3. DELETE. "IS NOT 0" keeps ONLY rows explicitly flagged real, so it removes
--      legacy NULL rows and test rows and can never touch a real participant.
--      (If any row in step 2 shows as REAL (0) before launch, stop and look at it.)

DELETE FROM verdict_attempts WHERE participant_id IN (SELECT id FROM participants WHERE is_test IS NOT 0);
DELETE FROM participants WHERE is_test IS NOT 0;

--   4. VERIFY: should return no rows, or only REAL (0) rows.
SELECT is_test, COUNT(*) FROM participants GROUP BY is_test;
