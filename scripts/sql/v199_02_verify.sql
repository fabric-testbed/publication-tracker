-- v1.9.9 verify -- READ ONLY
--
-- Run twice:
--   (a) right after v199_01_dedupe.sql, before the containers restart --
--       section 3 must show NO unique constraint yet (migrate creates it)
--   (b) after restarting into 1.9.9 --
--       section 3 must now show unique_task_timeout_tracker_name
--
-- Usage:
--   docker exec -i pubtrkr-database bash -c \
--     'psql -U $POSTGRES_USER -d $POSTGRES_DB -v ON_ERROR_STOP=1 -f -' \
--     < scripts/sql/v199_02_verify.sql

\set ON_ERROR_STOP on
\pset pager off

\echo ''
\echo '== 1. one row per tracker (expect exactly 3 rows, count 1 each) =='
SELECT name, count(*) AS rows
FROM task_timeout_tracker
GROUP BY name
ORDER BY name;

\echo ''
\echo '== 2. duplicates remaining (expect 0 rows) =='
SELECT name, count(*) AS rows
FROM task_timeout_tracker
GROUP BY name
HAVING count(*) > 1;

\echo ''
\echo '== 3. constraints -- absent before the deploy, present after =='
SELECT conname, pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conrelid = 'task_timeout_tracker'::regclass
ORDER BY conname;

\echo ''
\echo '== 4. surviving rows, and whether they are timed out (want t, so the'
\echo '      first caller refreshes value) =='
SELECT name,
       uuid,
       last_updated,
       timeout_in_seconds,
       (nullif(value, '') IS NOT NULL) AS has_value,
       (now() > last_updated + make_interval(secs => timeout_in_seconds)) AS timed_out
FROM task_timeout_tracker
ORDER BY name;

\echo ''
\echo '== 5. POST-DEPLOY ONLY: value populated once traffic has hit the app =='
\echo '      public_signing_key and token_revocation_list should stop being null;'
\echo '      author_refresh_check stays null until workstream B consumes it.'
SELECT name,
       (nullif(value, '') IS NOT NULL) AS has_value,
       coalesce(length(value), 0) AS value_length,
       last_updated
FROM task_timeout_tracker
ORDER BY name;

\echo ''
\echo '== 6. POST-DEPLOY ONLY: token auth works again once an access_type = token'
\echo '      ApiUser appears. Before 1.9.9 every row was cookie. =='
SELECT access_type, count(*) AS users
FROM apiuser_apiuser
GROUP BY access_type
ORDER BY access_type;
