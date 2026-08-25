-- v1.9.9 dedupe -- DESTRUCTIVE. Defaults to a dry run.
--
-- task_timeout_tracker holds three rows per tracker name. Both consumers in
-- publicationtrkr/utils/fabric_auth.py resolve their tracker with
-- TaskTimeoutTracker.objects.get(name=...), which raises MultipleObjectsReturned
-- against duplicates, and both swallow it with a broad except:
--   get_oidc_sub_from_token()  -> oidc_sub = None  (no bearer token authenticates)
--   get_token_revocation_list() -> []              (revoked tokens are accepted)
--
-- Collapse each name to one row, preferring a row that still holds a cached
-- value, then the most recently updated, then the lowest uuid so the choice is
-- deterministic and re-running is a no-op.
--
-- This script deliberately does NOT create the unique constraint. The migration
-- run_server.sh generates on container start does that, and would error if the
-- constraint already existed.
--
-- ORDER MATTERS: run this BEFORE restarting into 1.9.9. run_server.sh runs
-- makemigrations + migrate unconditionally on start, the generated AddConstraint
-- fails against duplicates, and docker-entrypoint.sh runs under set -e -- so the
-- container would not come up.
--
-- Dry run (rolls back, prints what it would do):
--   docker exec -i pubtrkr-database bash -c \
--     'psql -U $POSTGRES_USER -d $POSTGRES_DB -v ON_ERROR_STOP=1 -f -' \
--     < scripts/sql/v199_01_dedupe.sql
--
-- For real (take a pg_dump first):
--   docker exec -i pubtrkr-database bash -c \
--     'psql -U $POSTGRES_USER -d $POSTGRES_DB -v ON_ERROR_STOP=1 -v dedupe_action=COMMIT -f -' \
--     < scripts/sql/v199_01_dedupe.sql

\set ON_ERROR_STOP on
\pset pager off

-- Default to ROLLBACK unless the operator passed -v dedupe_action=COMMIT.
\if :{?dedupe_action}
\else
\set dedupe_action ROLLBACK
\endif

BEGIN;

-- Take an exclusive lock for the duration so nothing writes underneath us.
LOCK TABLE task_timeout_tracker IN ACCESS EXCLUSIVE MODE;

\echo ''
\echo '== before =='
SELECT name, count(*) AS rows FROM task_timeout_tracker GROUP BY name ORDER BY name;

-- Refuse to run if collapsing would discard a distinct cached value.
DO $$
DECLARE
    conflicted text;
BEGIN
    SELECT string_agg(name, ', ' ORDER BY name) INTO conflicted
    FROM (
        SELECT name
        FROM task_timeout_tracker
        WHERE nullif(value, '') IS NOT NULL
        GROUP BY name
        HAVING count(DISTINCT value) > 1
    ) c;
    IF conflicted IS NOT NULL THEN
        RAISE EXCEPTION 'refusing to dedupe: % holds more than one distinct cached value', conflicted;
    END IF;
END $$;

\echo ''
\echo '== deleting surplus rows =='
WITH ranked AS (
    SELECT uuid,
           row_number() OVER (PARTITION BY name
                              ORDER BY (nullif(value, '') IS NOT NULL) DESC,
                                       last_updated DESC,
                                       uuid ASC) AS rn
    FROM task_timeout_tracker
)
DELETE FROM task_timeout_tracker t
USING ranked r
WHERE t.uuid = r.uuid
  AND r.rn > 1;

-- Abort the whole transaction if anything is still duplicated.
DO $$
DECLARE
    remaining int;
BEGIN
    SELECT count(*) INTO remaining
    FROM (
        SELECT 1 FROM task_timeout_tracker GROUP BY name HAVING count(*) > 1
    ) d;
    IF remaining > 0 THEN
        RAISE EXCEPTION 'dedupe left % duplicated tracker name(s)', remaining;
    END IF;
END $$;

\echo ''
\echo '== after =='
SELECT name, uuid, last_updated, timeout_in_seconds,
       (nullif(value, '') IS NOT NULL) AS has_value
FROM task_timeout_tracker
ORDER BY name;

\echo ''
\echo '== ending transaction with: ' :dedupe_action ' =='
:dedupe_action;
