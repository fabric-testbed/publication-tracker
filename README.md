# FABRIC Publication Tracker

A Django web application for tracking research publications that utilize [FABRIC](https://fabric-testbed.net) (Federated Research Infrastructure for Cloud Environments) services.

**DISCLAIMER: The code herein may not be up to date nor compliant with the most recent package and/or security notices. The frequency at which this code is reviewed and updated is based solely on the lifecycle of the project for which it was written to support, and is not actively maintained outside of that scope. Use at your own risk.**

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Configuration](#configuration)
- [Running the Application](#running-the-application)
  - [Docker (Production)](#docker-production)
  - [Deployment-Specific Configuration](#deployment-specific-configuration)
  - [Local Development](#local-development)
  - [Database Migrations](#database-migrations)
  - [FABRIC User Sync](#fabric-user-sync)
- [Author Claims](#author-claims)
- [Initial Setup](#initial-setup)
- [Web Interface](#web-interface)
- [REST API](#rest-api)
- [Authentication](#authentication)
- [Project Structure](#project-structure)

---

## Overview

The FABRIC Publication Tracker allows FABRIC users and operators to record, browse, and manage research publications associated with FABRIC projects. It supports two publication workflows:

- **Full publications** (`publications` app) — Rich entries with BibTeX import/export, author claiming, and FABRIC project linkage.

Users authenticate via FABRIC's federated identity (CILogon / OAuth2). Role-based permissions control who can create publications and who has admin access.

---

## Architecture

### Services (Docker)

| Service | Image | Port | Purpose |
|---|---|---|---|
| `pubtrkr-database` | `postgres:18` | 5432 | PostgreSQL database |
| `pubtrkr-nginx` | `nginx:1` | 8080 (HTTP), 8443 (HTTPS) | Reverse proxy + SSL termination |
| `pubtrkr-vouch-proxy` | `fabrictestbed/vouch-proxy:0.27.1` | 9090 (internal) | OAuth2/OIDC authentication |
| `pubtrkr-cron` | same image as `django` | none | Scheduled FABRIC user sync ([FABRIC User Sync](#fabric-user-sync)) |

The Django application runs outside of Docker (via `run_server.sh`) for local development, or can be containerized using the provided `Dockerfile` for production. Alternate compose files in `compose/` provide configurations for local-ssl and production-ssl deployments.

All services communicate on a private bridge network (`pubtrkr-network`).

### Django Apps

| App | Models | Purpose |
|---|---|---|
| `apiuser` | `ApiUser`, `TaskTimeoutTracker` | FABRIC identity, role caching, directory sync |
| `publications` | `Publication`, `Author` | Full BibTeX-enabled publication tracking |

### Authentication Flow

```
Browser → Nginx → /validate (Vouch Proxy)
                ↓ 401
            /login (Vouch) → CILogon OAuth2
                ↓ success
            JWT cookie set → Django decodes → FABRIC Core API → ApiUser created/updated
```

Two auth modes are supported:
- **Cookie-based** (web UI): Vouch Proxy JWT cookie, validated per request
- **Bearer token** (API): `Authorization: Bearer <token>` header

### Role-Based Access

| FABRIC Role | Permission |
|---|---|
| `publication-tracker-admins` | Full admin (CRUD on all records, API user list) |
| `Jupyterhub` (configurable) | Can create publications |
| Any authenticated user | Can view and claim authorship |
| Anonymous | Read-only access to publications and projects |

---

## Requirements

- Python >= 3.12
- Docker and Docker Compose (for production deployment)
- PostgreSQL 18 (provided by Docker in production; external for local dev)
- CILogon client credentials (for OAuth2/OIDC)

### Python Dependencies

```
bibtexparser
cryptography
Django
django-bootstrap5
django-cors-headers
django-filter
djangorestframework
drf-spectacular
fontawesomefree
markdown
psycopg2-binary
pyjwt
python-dateutil
requests
uwsgi
```

---

## Configuration

Copy `env.template` to `.env` and populate all values before starting the application.

```bash
cp env.template .env
```

### Environment Variables

#### Publication Tracker

| Variable | Example | Description |
|---|---|---|
| `API_DEBUG` | `false` | Enable debug output in API responses |
| `PUBLICATION_TRACKER_ADMINS_ROLE` | `publication-tracker-admins` | FABRIC role granting admin access |
| `CAN_CREATE_PUBLICATION_ROLE` | `Jupyterhub` | FABRIC role allowing publication creation |
| `API_USER_REFRESH_CHECK_MINUTES` | `5` | How often to re-fetch user details from FABRIC Core API (minutes) |
| `API_USER_ANON_UUID` | `00000000-0000-0000-0000-000000000000` | UUID for the anonymous (unauthenticated) user |
| `API_USER_ANON_NAME` | `Anonymous API User` | Display name for anonymous user |

#### Task Timeout Tracker (API result caching)

| Variable | Default | Description |
|---|---|---|
| `PSK_NAME` | `public_signing_key` | Public Signing Key task name |
| `PSK_DESCRIPTION` | `Public Signing Key` | Public Signing Key description |
| `PSK_TIMEOUT_IN_SECONDS` | `86400` | Public Signing Key cache timeout (seconds) |
| `TRL_NAME` | `token_revocation_list` | Token Revocation List task name |
| `TRL_DESCRIPTION` | `Token Revocation List` | Token Revocation List description |
| `TRL_TIMEOUT_IN_SECONDS` | `300` | Token Revocation List cache timeout (seconds) |
| `USR_NAME` | `user_sync_check` | User Sync task name; its `value` holds the sync watermark |
| `USR_DESCRIPTION` | `User Sync Check` | User Sync description |
| `USR_TIMEOUT_IN_SECONDS` | `86400` | Intended sync cadence, consulted by `sync_fabric_users --if-due` |
| `CLM_NAME` | `claim_scoring_check` | Author-claim scoring task name; its `value` holds the last run's timestamp |
| `CLM_DESCRIPTION` | `Author Claim Scoring Check` | Author-claim scoring description |
| `CLM_TIMEOUT_IN_SECONDS` | `86400` | Intended scoring cadence, consulted by `score_author_claims --if-due` |

#### FABRIC Services

| Variable | Example | Description |
|---|---|---|
| `FABRIC_CORE_API` | `https://uis.fabric-testbed.net/` | FABRIC Core API base URL |
| `FABRIC_CREDENTIAL_MANAGER` | `https://cm.fabric-testbed.net/` | Credential Manager URL |
| `FABRIC_PORTAL` | `https://portal.fabric-testbed.net` | FABRIC Portal base URL (used for project links) |
| `FABRIC_CORE_API_TOKEN` | — | Read-only Core API service token used by the user sync. Server-to-server only |
| `FABRIC_TOKEN_ISSUER` | *(unset)* | Expected `iss` on a FABRIC bearer token. Unset means unchecked; set to `fabric-core-api` once uis issues tokens with it |
| `FABRIC_TOKEN_AUDIENCE` | *(unset)* | Expected `aud` on a FABRIC bearer token. Empty means the claim is not checked |
| `FABRIC_HTTP_TIMEOUT_SECONDS` | `10` | Outbound timeout for calls made on the request path |
| `FABRIC_SYNC_HTTP_TIMEOUT_SECONDS` | `60` | Outbound timeout for the user sync, which runs off the request path |
| `USER_SYNC_CRON_SCHEDULE` | `0 3 * * *` | Crontab schedule for the user sync in the `pubtrkr-cron` sidecar |
| `CLAIM_SCORING_CRON_SCHEDULE` | `30 3 * * *` | Crontab schedule for author-claim scoring in the same sidecar |

#### Vouch Proxy

| Variable | Example | Description |
|---|---|---|
| `VOUCH_COOKIE_NAME` | `fabric-service` | Name of the JWT cookie set by Vouch |
| `VOUCH_JWT_SECRET` | `<secret>` | Shared secret for JWT validation |
| `VOUCH_JWT_ISSUER` | `Vouch` | Expected `iss` on the Vouch cookie. Vouch Proxy's own default; empty means the claim is not checked |
| `VOUCH_JWT_AUDIENCE` | *(unset)* | Expected `aud` on the Vouch cookie. Vouch stamps no top-level `aud` by default, so leave it empty unless yours does |

#### Django

| Variable | Example | Description |
|---|---|---|
| `PYTHONPATH` | `./` | Python module search path |
| `DJANGO_ALLOWED_HOSTS` | `127.0.0.1,localhost` | Comma-separated allowed hostnames |
| `DJANGO_SECRET_KEY` | `<random string>` | Django secret key |
| `DJANGO_DEBUG` | `false` | Django debug mode |
| `DJANGO_LOG_LEVEL` | `WARNING` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `DJANGO_SESSION_COOKIE_AGE` | `3600` | Session cookie lifetime (seconds) |
| `DJANGO_TIME_ZONE` | `America/New_York` | Django timezone |
| `REST_FRAMEWORK_PAGE_SIZE` | `20` | API pagination page size |

#### PostgreSQL

| Variable | Default | Description |
|---|---|---|
| `POSTGRES_PASSWORD` | `<secret>` | Database password |
| `POSTGRES_USER` | `<fabric_database_user>` | Database user. `env.template` ships a placeholder, not `postgres` |
| `POSTGRES_DB` | `<fabric_database_name>` | Database name. `env.template` ships a placeholder, not `postgres` |
| `POSTGRES_HOST` | `database` | Database host (use `database` in Docker) |
| `POSTGRES_PORT` | `5432` | Database port |
| `HOST_DB_DATA` | `./db_data` | Host path for persistent database data |
| `PGDATA` | `/var/lib/postgresql/data` | Container path for PostgreSQL data |

#### Nginx

| Variable | Default | Description |
|---|---|---|
| `NGINX_DEFAULT_CONF` | `./nginx/default.conf` | Path to Nginx virtual server config |
| `NGINX_NGINX_CONF` | `./nginx/nginx.conf` | Path to Nginx main config |
| `NGINX_SSL_CERTS_DIR` | `./ssl` | Path to SSL certificates directory |

#### uWSGI

| Variable | Default | Description |
|---|---|---|
| `UWSGI_UID` | `<user_uid>` | uWSGI process UID — `local-ssl` run-mode only |
| `UWSGI_GID` | `<user_gid>` | uWSGI process GID — `local-ssl` run-mode only |

The Docker run-mode ignores both. That container starts unprivileged as `appuser`
(uid 20049) from the image, so uwsgi has no privilege to drop.

### Vouch Proxy Configuration

Copy and populate the Vouch config template:

```bash
cp vouch/config.template vouch/config
```

Key fields in `vouch/config`:

```yaml
vouch:
  allowAllUsers: true      # Accept any CILogon-authenticated user
  publicAccess: true       # Also serve unauthenticated (read-only) requests
  jwt:
    secret: <matches VOUCH_JWT_SECRET in .env>
  cookie:
    name: fabric-service   # matches VOUCH_COOKIE_NAME in .env
    domain: 127.0.0.1
    secure: true           # session JWT is https-only
    httpOnly: true         # not readable from JavaScript
    sameSite: lax          # not sent on cross-site subrequests

oauth:
  provider: oidc
  client_id: <cilogon_client_id>
  client_secret: <cilogon_client_secret>
  auth_url: https://cilogon.org/authorize
  token_url: https://cilogon.org/oauth2/token
  callback_url: https://127.0.0.1:8443/auth
```

### SSL Certificates

TODO: Generation of development self-signed certificates is described in `ssl/` (never use in production). For production, replace with valid certificates mounted to the Nginx container at:

- `/etc/ssl/fullchain.pem`
- `/etc/ssl/privkey.pem`
- `/etc/ssl/chain.pem`

---

## Running the Application

### Docker (Production)

All commands run from `publication-tracker/`.

```bash
# Build the image -- required on every release, see below
docker compose build

# Start all services
docker compose up -d

# Follow logs
docker compose logs -f

# Stop all services
docker compose down
```

**`docker compose build` is required on every release.** Dependencies are installed
into `/opt/venv` inside the image at build time with `uv sync --frozen`; nothing
resolves or installs at container start any more, so a release that moves `uv.lock`
reaches the running container only through a rebuild. A restart alone will keep serving
the previous dependency set.

Two other properties of the production container are worth knowing before you change a
mount or a command:

- **It runs as `appuser`, 20049:20049**, which is `nrig-service` on the FABRIC host.
  That identity is what lets the container write the `static/` and `media/` sub-mounts on
  the host checkout, and it must also be able to *read* `.env` -- on a host where the
  operator account is not nrig-service, share it by group
  (`chown <operator>:20049 .env && chmod 0640 .env`). A different host needs its own
  mapping.
- **`/code` is mounted read-only**, with read-write sub-mounts for `static/` and
  `media/` alone. A boot leaves the deploy checkout untouched, so `git status` there
  stays clean. Anything that needs to write elsewhere under `/code` will fail with
  `EACCES`, and that is the intended answer.

The application is available at:
- `http://localhost:8080` (redirects to HTTPS)
- `https://localhost:8443`

### Deployment-Specific Configuration

A deployment should never need to edit a tracked file. Everything that differs
between hosts is environment-driven:

| What differs | How to set it |
|---|---|
| Public hostname | `DJANGO_ALLOWED_HOSTS` (comma-separated, added to the localhost defaults) |
| Browser origins allowed to call the API | `DJANGO_CORS_ALLOWED_ORIGINS` (comma-separated) |
| Published http/https ports | `NGINX_HTTP_PORT` / `NGINX_HTTPS_PORT` (default 8080/8443) |
| PostgreSQL data directory on the host | `HOST_DB_DATA` |
| nginx server config | `NGINX_DEFAULT_CONF` -- point it at a copy, e.g. `./nginx/default.prod.conf` |
| Public hostname nginx answers to | `server_name` inside that copy. The tracked `nginx/default.conf` names only `127.0.0.1` and `localhost`, and a catch-all server returns 444 for every other `Host` |
| TLS certificate directory | `NGINX_SSL_CERTS_DIR` |

For anything that genuinely cannot be expressed as a variable -- an extra bind
mount, say -- copy `docker-compose.override.yml.example` to
`docker-compose.override.yml`. Compose merges it automatically, with no `-f` flag.
Both that file and `nginx/default.prod.conf` are gitignored.

`nginx/default.conf.prod-example` shows how the shipped nginx config differs from a
production one: no `:8443` suffix (nginx publishes 80/443 directly), real certificate
filenames, and the deployment's own `server_name` in place of `<your.fqdn>`. Set that
last one before starting nginx -- the catch-all server above it answers every
unmatched `Host` with 444, so a wrong value means the site serves nothing.

Keeping the checkout clean this way means upgrading is `git fetch --tags && git
checkout <tag>` -- no stashing local edits across the switch, and no silent
auto-merge into a file the deployment had modified.

### Local Development

All commands run from `publication-tracker/publicationtrkr/`.

Set up a virtual environment:

```bash
cd publication-tracker
uv venv .venv --python 3.12
source .venv/bin/activate
uv pip install -e .
```

Start the development server (requires a running PostgreSQL instance and `.env` populated):

```bash
cd publicationtrkr

# Basic local dev server (no SSL)
./run_server.sh -r local-dev

# With SSL (uWSGI)
./run_server.sh -r local-ssl

# Load fixtures and start
./run_server.sh -r local-dev -l
```

The development server starts at `http://localhost:8000`.

### Database Migrations

Migration files are committed to the repository under
`publicationtrkr/apps/*/migrations/`. They are **not** generated at container
start; `run_server.sh` only applies what has been reviewed and merged.

When you change a model, generate the migration yourself and commit it:

```bash
python manage.py makemigrations
# review the generated file, then
git add publicationtrkr/apps/<app>/migrations/
```

On startup the server asserts that the committed migrations still describe the
current models, and refuses to start if they do not:

```
### VERIFY committed migrations match models ###
ERROR: models have changes with no corresponding committed migration.
```

That means a model change reached the branch without its migration. Generate and
commit it rather than working around the check. Migrations used to be gitignored
and regenerated on every container start, which meant the migration applied to
production had never been reviewed and could differ between hosts.

---

### FABRIC User Sync

`ApiUser` used to be a cache of people who had logged in, filled lazily on first
authenticated request. That made it useless as a directory: the author-claim flow
could only offer people who had already visited. `sync_fabric_users` populates it
from the whole FABRIC population instead, reading `GET /journey-tracker/people` on
Core API (~3,300 people, returned unpaginated).

```bash
python manage.py sync_fabric_users             # incremental: watermark -> now
python manage.py sync_fabric_users --dry-run   # preview, writes nothing
python manage.py sync_fabric_users --full      # whole population, walked backwards
python manage.py sync_fabric_users --since 2026-01-01
python manage.py sync_fabric_users --if-due    # no-op unless the cadence has elapsed
```

The endpoint filters on the person's `updated` timestamp and rejects a window wider
than 90 days, so a backfill is walked in 89-day chunks — currently 28 requests,
about 11 seconds. The watermark lives in the `user_sync_check` `TaskTimeoutTracker`
row and is advanced **only** when every window in a run succeeded; a window that
could not be read aborts the run, because a watermark moved past people who were
never read would hide them until the next full backfill.

**What the sync will not do.** It never deletes: `Publication.created_by` and
`modified_by` are `SET_NULL`, so removing an `ApiUser` silently destroys publication
provenance. Deactivated people are marked `active=False` and kept. It also never
writes `cilogon_id`, `access_expires`, `access_type` or `has_logged_in` on a row that
already exists — those belong to the login path. `/journey-tracker/people` does not
return `cilogon_id`, which is our login join key, and that resolves itself: a synced
row leaves it blank, and the first time that person signs in, `auth_user_by_cookie` /
`auth_user_by_token` find the row by `uuid` and fill it in.

#### Schedule

The `pubtrkr-cron` sidecar runs the sync on `USER_SYNC_CRON_SCHEDULE` (daily at 03:00
UTC by default). It reuses the django image and the same `./:/code` mount so it reads
the identical `.env`, but overrides the entrypoint — `docker-entrypoint.sh` would run
migrations, collectstatic and a second uwsgi. Its output is redirected to
`/proc/1/fd/1` so runs appear in the container log:

```bash
docker compose logs -f cron
```

Two syncs cannot corrupt the watermark by interleaving. The command takes a Postgres
advisory lock, which holds across containers, so an operator running a backfill by
hand while the sidecar fires simply causes the second run to exit without syncing.

The sidecar exists only in `docker-compose.yml` and `compose/docker-compose.yml.prod-ssl`.
`compose/docker-compose.yml.local-ssl` has no `django` service — Django runs on the host
there — so there is no image for it to reuse; run `python manage.py sync_fabric_users`
by hand in local development.

---

## Author Claims

Linking an author on a paper to the FABRIC user who wrote it is ambiguous work:
`Smith, J.`, `J. Smith` and `Jane Q. Smith` may or may not be the same person, which is
why claiming has always been the honor system. Author-claim scoring keeps that
self-service path exactly as it is and adds a second one: a nightly pass that *suggests*
pairs, and an admin queue where a human decides.

**Scoring never writes attribution.** `Author.fabric_uuid` stays the single authoritative
field, written only by a self-claim, by an admin on the author edit form, or by an
approval on the queue. That separation is what lets the whole queue be regenerated at
will, and it means a scoring run has no user-visible effect at all.

Two signals, both local, so a run makes no network calls:

| Signal | Weight | What it reads |
|---|---|---|
| Project co-membership | 0.55 | `Publication.project_uuid` against `ApiUser.projects` |
| Name compatibility | 0.45 | The author string against `ApiUser.name` |

Surname agreement is a **gate**, not a weight — without it every author pairs with every
one of ~3,300 users. Absence is never read as disagreement: an initial where the other
side has a full given name is missing information, not a mismatch, and ranks accordingly.
Each suggestion stores its per-signal breakdown, which the queue displays, because a bare
number is unreviewable.

### The queue

`/publications/authors/claims`, admins only, also linked as **Author Claims** in the
navbar. Suggestions are grouped by author and the best-scoring author comes first, so the
high-confidence decisions are the ones you meet on page one.

* **Approve** writes `Author.fabric_uuid`, stamps who decided and when, and withdraws that
  author's other standing suggestions — attribution is settled, and leaving the runners-up
  in the queue would invite overwriting it.
* **Reject** records that the pair is not a match and changes no attribution. Rejections
  are *kept*: they are the only thing that stops a pair being re-suggested every night.
* An author already claimed by somebody else is refused rather than overwritten. Use the
  author edit page to reassign one, where the current value is in front of you.

The `Approved`, `Rejected` and `Self-asserted` tabs are the ledger of every decision,
including the self-claims made through the ordinary path and the attributions admins type
into the author form.

### Schedule

The same `pubtrkr-cron` sidecar runs scoring on `CLAIM_SCORING_CRON_SCHEDULE`, half an
hour after the user sync by default. That order matters: the strongest signal reads
`ApiUser.projects`, so a run that went first would score against yesterday's directory.

```bash
# Preview, then run, from the sidecar (it sources .env, which `docker exec` does not)
docker exec pubtrkr-cron /code/scripts/run-claim-scoring.sh --dry-run
docker exec pubtrkr-cron /code/scripts/run-claim-scoring.sh
```

Useful flags: `--dry-run` reports without writing, `--full` rescores every author
including claimed ones, and `--if-due` exits unless `CLM_TIMEOUT_IN_SECONDS` has elapsed
(for a sidecar firing more often than the cadence — the shipped schedule fires exactly on
it, so the crontab entry does not use it). Two runs cannot collide: the command takes a
Postgres advisory lock, which holds across containers.

---

## Initial Setup

After the database is up, run these once to initialize required records:

```bash
# Inside the Django container (Docker) or local venv
python manage.py migrate
python manage.py collectstatic --no-input
python manage.py init_anon_api_user
python manage.py init_task_timeout_tracker

# First population of the user directory (~3,300 people, about 11 seconds).
# Preview it first; see "FABRIC User Sync" for what this does and does not touch.
python manage.py sync_fabric_users --full --dry-run
python manage.py sync_fabric_users --full

# First author-claim scoring pass, which fills the admin queue. Preview it first;
# see "Author Claims" for what it does and does not touch.
python manage.py score_author_claims --dry-run
python manage.py score_author_claims
```

## Web Interface

All pages are accessible at `https://<host>:8443/`.

| URL | Access | Description |
|---|---|---|
| `/` | All | Landing page |
| `/publications/` | All | Browse all publications with sorting (by year or title) and search |
| `/publications/create/` | Creators, Admins | Create a new publication (BibTeX or manual) |
| `/publications/<uuid>` | All | Publication detail with edit/delete for owners and admins |
| `/publications/<uuid>/update` | Owner, Admins | Edit publication fields |
| `/publications/authors/` | All | Browse all authors; "FABRIC linked" column shows claimed status |
| `/publications/authors/<uuid>/update` | Creators, Admins | Edit author display name and claim |
| `/publications/authors/claims` | Admins only | Review scored author-claim suggestions; approve or reject |
| `/publications/by-author-uuid/<fabric_uuid>` | All | All publications by a specific FABRIC user |
| `/publications/projects/` | All | Publications grouped by FABRIC project (name, count, link) |
| `/publications/projects/<project_uuid>` | All | All publications for a specific project |
| `/apiusers/` | Admins only | List all API users (name, email, UUID, affiliation) |
| `/apiusers/<uuid>` | Admins only | API user detail (roles, projects, access info) |

### Creating a Publication

Publications can be created two ways:

**1. BibTeX paste** — Paste a raw BibTeX entry into the form. Fields are auto-parsed:
```bibtex
@article{Smith2024,
  author  = {Smith, Jane and Doe, John},
  title   = {Using FABRIC for Large-Scale Network Experiments},
  journal = {IEEE INFOCOM},
  year    = {2024},
  url     = {https://doi.org/10.1000/example}
}
```

**2. Manual entry** — Fill in the form fields directly. Manual values override any parsed BibTeX values.

Required fields: **authors**, **title**, **year**.

### Claiming Authorship

Authors listed on a publication can "claim" their entry to link their FABRIC identity:

1. Navigate to `Authors` in the navbar.
2. Find your name — the **FABRIC linked** column shows `Yes` (linked) or `No` (unclaimed).
3. Click **Edit** to set your display name and associate your FABRIC UUID.
4. Once claimed, your name on any publication list becomes a link to all your publications.

Claiming stays immediate and needs nobody's approval. Admins additionally get a queue of
scored suggestions for authors nobody has claimed — see "Author Claims" above.

---

## REST API

The REST API is available at `/api/`. Interactive documentation:

- **Swagger UI**: `https://<host>:8443/api/swagger/`
- **ReDoc**: `https://<host>:8443/api/redoc/`
- **OpenAPI schema**: `https://<host>:8443/api/schema/`

### Authentication

API requests authenticate via Bearer token:

```bash
curl -H "Authorization: Bearer <token>" https://<host>:8443/api/publications
```

Unauthenticated requests are allowed for read operations (GET).

**Writes must not be form-encoded.** `POST`, `PUT`, `PATCH` and `DELETE` under `/api/`
are rejected with `403` when the body is `application/x-www-form-urlencoded`,
`multipart/form-data` or `text/plain` and no `X-Requested-With` header is present.
Those three content types are the ones a browser will send cross-origin with no
preflight, so they were forgeable against a visitor's Vouch cookie. Sending
`Content-Type: application/json`, as every example below does, is unaffected.

DRF's browsable-API login at `/api-auth/login/` has been removed and returns `404`.

### Endpoints

#### Publications — `/api/publications`

| Method | URL | Description |
|---|---|---|
| `GET` | `/api/publications` | List publications (paginated) |
| `POST` | `/api/publications` | Create a publication |
| `POST` | `/api/publications/bulk` | Create many publications from one document (admin only) |
| `GET` | `/api/publications/<uuid>` | Get a publication |
| `PUT` | `/api/publications/<uuid>` | Update a publication |
| `DELETE` | `/api/publications/<uuid>` | Delete a publication |
| `GET` | `/api/publications/<uuid>/bibtex` | Get BibTeX for a publication |
| `GET` | `/api/publications/by-author-uuid` | Publications by FABRIC user UUID |
| `GET` | `/api/publications/by-project-uuid` | Publications by FABRIC project UUID |

**Query parameters:**

| Parameter | Endpoints | Description |
|---|---|---|
| `?search=<term>` | `GET /api/publications` | Filter by title or project name (case-insensitive) |
| `?sort_by=<field>` | `GET /api/publications` | Sort field: `title` or `year` (default: `year`) |
| `?order_by=<dir>` | `GET /api/publications` | Sort direction: `asc` or `desc` (default: `desc`) |
| `?page=<n>` | All list endpoints | Pagination |
| `?fabric_uuid=<uuid>` | `by-author-uuid` | Required; must be a valid UUID v4 |
| `?project_uuid=<uuid>` | `by-project-uuid` | Required; must be a valid UUID v4 |
| `?search=<term>` | `by-project-uuid` | Optional; filter by title or project name (3+ chars) |

When `sort_by=year`, a secondary sort by title (ascending) is applied within each year.

**Examples:**

```bash
# List all publications (page 2)
curl "https://<host>:8443/api/publications?page=2"

# Search publications
curl "https://<host>:8443/api/publications?search=FABRIC+network"

# Sort by year descending (default)
curl "https://<host>:8443/api/publications?sort_by=year&order_by=desc"

# Sort by title ascending
curl "https://<host>:8443/api/publications?sort_by=title&order_by=asc"

# Get a specific publication
curl "https://<host>:8443/api/publications/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

# Get BibTeX
curl "https://<host>:8443/api/publications/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx/bibtex"

# Publications for a FABRIC user
curl "https://<host>:8443/api/publications/by-author-uuid?fabric_uuid=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

# Publications for a project (with search)
curl "https://<host>:8443/api/publications/by-project-uuid?project_uuid=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx&search=network"

# Create a publication (authenticated)
curl -X POST "https://<host>:8443/api/publications" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "authors": ["Smith, Jane", "Doe, John"],
    "title": "Using FABRIC for Large-Scale Network Experiments",
    "year": "2024",
    "venue": "IEEE INFOCOM",
    "link": "https://doi.org/10.1000/example",
    "project_name": "My FABRIC Project",
    "project_uuid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
  }'

# Bulk create from a JSON body (publication-tracker admins only)
curl -X POST "https://<host>:8443/api/publications/bulk" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"publications": [
    {"title": "First Paper", "authors": ["Smith, Jane"], "year": "2024"},
    {"title": "Second Paper", "authors": ["Doe, John"], "year": "2025"}
  ]}'

# Bulk create from a file: .jsonl (one object per line) or .bib (BibTeX).
# A multipart body under /api/ requires the X-Requested-With header.
curl -X POST "https://<host>:8443/api/publications/bulk" \
  -H "Authorization: Bearer <token>" \
  -H "X-Requested-With: XMLHttpRequest" \
  -F "file=@publications.bib"
```

**Bulk response schema:**

Every record is reported on by position, so a partial success can be acted on. `index`
is the position in the submitted document; a `.jsonl` upload also reports `line`, the
1-based file line. Records are created one at a time in their own transactions, so a
record whose title and link already exist is `skipped` and the rest of the batch still
lands.

```json
{
  "total": 3,
  "created": 1,
  "skipped": 1,
  "failed": 1,
  "results": [
    {"index": 0, "status": "created", "uuid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"},
    {"index": 1, "status": "skipped", "reason": "duplicate key value violates unique constraint ..."},
    {"index": 2, "status": "failed", "errors": [{"year": "must provide a year"}]}
  ]
}
```

Limits: `BULK_MAX_RECORDS` records (default 1000) and `BULK_MAX_UPLOAD_BYTES` bytes
(default 10 MB) per request, both enforced before anything is written, and the `bulk`
throttle scope (`THROTTLE_BULK`, default `6/hour`). Admins can also use the
`/publications/bulk-upload` page, which posts the same file through a CSRF-protected
Django form.

**Publication response schema:**

```json
{
  "authors": [
    {
      "author_name": "Smith, Jane",
      "display_name": "Smith, Jane",
      "fabric_uuid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "publication_uuid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "uuid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    }
  ],
  "bibtex": "@article{Smith2024, ...}",
  "created": "2024-01-15 10:30:00+00:00",
  "created_by": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "link": "https://doi.org/10.1000/example",
  "modified": "2024-01-15 10:30:00+00:00",
  "modified_by": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "project_name": "My FABRIC Project",
  "project_uuid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "title": "Using FABRIC for Large-Scale Network Experiments",
  "uuid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "venue": "IEEE INFOCOM",
  "year": "2024"
}
```

**Paginated list response schema:**

```json
{
  "count": 42,
  "next": "https://<host>/api/publications?page=3",
  "previous": "https://<host>/api/publications?page=1",
  "results": [ ... ]
}
```

#### Authors — `/api/authors`

| Method | URL | Description |
|---|---|---|
| `GET` | `/api/authors` | List authors (paginated) |
| `GET` | `/api/authors/<uuid>` | Get an author |
| `PUT` | `/api/authors/<uuid>` | Update display name / claim authorship |

```bash
# List authors (search by name)
curl "https://<host>:8443/api/authors?search=Smith"

# Get author detail
curl "https://<host>:8443/api/authors/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

## Authentication

### Web (Cookie-based)

1. Navigate to `https://<host>:8443/`
2. If unauthenticated, Nginx redirects to `/login` (Vouch Proxy)
3. Vouch handles CILogon OAuth2 — authenticate with your institutional credentials
4. On success, a JWT cookie (`fabric-service`) is set
5. Django decodes the cookie, calls FABRIC Core API to fetch roles and project memberships, and creates/updates an `ApiUser` record

### API (Bearer Token)

Obtain a FABRIC token via the [FABRIC Credential Manager](https://cm.fabric-testbed.net/) and pass it as a Bearer token:

```bash
curl -H "Authorization: Bearer <fabric_token>" https://<host>:8443/api/publications
```

---

## Project Structure

```
publication-tracker/
├── .env                          # Active configuration (not in git)
├── env.template                  # Configuration template
├── docker-compose.yml            # Docker composition (database, nginx, vouch)
├── compose/
│   ├── docker-compose.yml.local-ssl   # Alternate compose for local SSL
│   └── docker-compose.yml.prod-ssl    # Alternate compose for production SSL
├── Dockerfile                    # Django container image (python:3 + uv)
├── docker-entrypoint.sh          # Container startup script
├── docker-entrypoint-cron.sh     # Cron sidecar startup; writes the /etc/cron.d entries
├── scripts/
│   ├── run-user-sync.sh          # Cron wrapper for sync_fabric_users
│   └── run-claim-scoring.sh      # Cron wrapper for score_author_claims
├── run_server.sh                 # Server launcher (local-dev / local-ssl / docker)
├── pyproject.toml                # Python dependencies (requires 3.12+)
├── publicationtrkr.ini           # uWSGI configuration
├── nginx/
│   ├── nginx.conf                # Nginx main config
│   └── default.conf              # Virtual server (SSL, routing, Vouch auth)
├── ssl/                          # Development self-signed certificates
├── vouch/
│   ├── config.template           # Vouch Proxy config template
│   └── config                    # Active Vouch config (not in git)
└── publicationtrkr/              # Django project root
    ├── manage.py
    ├── server/
    │   ├── settings.py           # Django settings
    │   ├── urls.py               # Root URL configuration
    │   ├── middleware.py         # /api/ cross-site write guard
    │   └── wsgi.py
    ├── apps/
    │   ├── apiuser/              # FABRIC identity & role management
    │   │   ├── models.py         # ApiUser, TaskTimeoutTracker
    │   │   ├── views.py          # apiuser_list, apiuser_detail
    │   │   ├── tests.py
    │   │   ├── urls.py
    │   │   ├── fixtures/         # apiuser.json
    │   │   └── management/commands/
    │   │       ├── init_anon_api_user.py
    │   │       └── init_task_timeout_tracker.py
    │   └── publications/         # Full publication tracking (BibTeX)
    │       ├── models.py         # Publication, Author, AuthorClaim
    │       ├── views.py          # publication_*, author_*
    │       ├── tests.py
    │       ├── urls.py
    │       ├── forms.py          # PublicationForm, AuthorForm
    │       ├── utils/
    │       │   └── bibtex_utils.py   # BibTeX parsing & generation
    │       ├── api/
    │       │   ├── viewsets.py   # PublicationViewSet, AuthorViewSet
    │       │   ├── serializers.py
    │       │   └── validators.py
    │       └── templatetags/
    │           └── publications_tags.py
    ├── utils/
    │   ├── fabric_auth.py        # Cookie & bearer token authentication
    │   └── core_api.py           # FABRIC Core API wrappers
    └── templates/
        ├── publicationtrkr/      # base.html, navbar.html, home.html, footer.html
        ├── apiuser/              # apiuser_list.html, apiuser_detail.html
        └── publications/         # publication_*.html, author_*.html
```
