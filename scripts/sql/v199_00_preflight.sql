-- v1.9.9 preflight -- READ ONLY
--
-- Baseline task_timeout_tracker before running v199_01_dedupe.sql. Nothing here
-- writes. Capture the output; v199_02_verify.sql compares against it.
--
-- Usage:
--   docker exec -i pubtrkr-database bash -c \
--     'psql -U $POSTGRES_USER -d $POSTGRES_DB -v ON_ERROR_STOP=1 -f -' \
--     < scripts/sql/v199_00_preflight.sql

\set ON_ERROR_STOP on
\pset pager off

\echo ''
\echo '== 1. rows per tracker name (expect 3 each before the dedupe, 1 each after) =='
SELECT name,
       count(*)                 AS rows,
       count(nullif(value, '')) AS rows_with_value,
       max(last_updated)        AS newest
FROM task_timeout_tracker
GROUP BY name
ORDER BY name;

\echo ''
\echo '== 2. duplicated tracker names (these are what break objects.get(name=...)) =='
SELECT name, count(*) AS rows
FROM task_timeout_tracker
GROUP BY name
HAVING count(*) > 1
ORDER BY name;

\echo ''
\echo '== 3. every row, ranked the way the dedupe ranks them -- rn = 1 survives =='
SELECT name,
       row_number() OVER (PARTITION BY name
                          ORDER BY (nullif(value, '') IS NOT NULL) DESC,
                                   last_updated DESC,
                                   uuid ASC) AS rn,
       uuid,
       last_updated,
       timeout_in_seconds,
       (nullif(value, '') IS NOT NULL) AS has_value
FROM task_timeout_tracker
ORDER BY name, rn;

\echo ''
\echo '== 4. rows that would lose a cached value -- expect 0, otherwise stop and look =='
SELECT name, count(*) AS rows_with_value
FROM task_timeout_tracker
WHERE nullif(value, '') IS NOT NULL
GROUP BY name
HAVING count(*) > 1
ORDER BY name;

\echo ''
\echo '== 5. constraints on the table -- unique_task_timeout_tracker_name must NOT'
\echo '      exist yet; migrate creates it when the container restarts into 1.9.9 =='
SELECT conname, pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conrelid = 'task_timeout_tracker'::regclass
ORDER BY conname;

\echo ''
\echo '== 6. surviving rows will be timed out, so the first caller repopulates value =='
SELECT DISTINCT ON (name)
       name,
       last_updated,
       timeout_in_seconds,
       (now() > last_updated + make_interval(secs => timeout_in_seconds)) AS timed_out
FROM task_timeout_tracker
ORDER BY name,
         (nullif(value, '') IS NOT NULL) DESC,
         last_updated DESC,
         uuid ASC;
