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
  - [Local Development](#local-development)
- [Initial Setup](#initial-setup)
- [Web Interface](#web-interface)
- [REST API](#rest-api)
- [Authentication](#authentication)
- [Project Structure](#project-structure)

---

## Overview

The FABRIC Publication Tracker allows FABRIC users and operators to record, browse, and manage research publications associated with FABRIC projects. It supports two publication workflows:

- **Full publications** (`publications` app) — Rich entries with BibTeX import/export, author claiming, and FABRIC project linkage.
- **Simple publications** (`pubsimple` app) — Lightweight manual entries for quick data capture.

Users authenticate via FABRIC's federated identity (CILogon / OAuth2). Role-based permissions control who can create publications and who has admin access.

---

## Architecture

### Services (Docker)

| Service | Image | Port | Purpose |
|---|---|---|---|
| `pubtrkr-database` | `postgres:18` | 5432 | PostgreSQL database |
| `pubtrkr-nginx` | `nginx:1` | 8080 (HTTP), 8443 (HTTPS) | Reverse proxy + SSL termination |
| `pubtrkr-vouch-proxy` | `fabrictestbed/vouch-proxy:0.27.1` | 9090 (internal) | OAuth2/OIDC authentication |

The Django application runs outside of Docker (via `run_server.sh`) for local development, or can be containerized using the provided `Dockerfile` for production. Alternate compose files in `compose/` provide configurations for local-ssl and production-ssl deployments.

All services communicate on a private bridge network (`pubtrkr-network`).

### Django Apps

| App | Models | Purpose |
|---|---|---|
| `apiuser` | `ApiUser`, `TaskTimeoutTracker` | FABRIC identity, role caching |
| `publications` | `Publication`, `Author` | Full BibTeX-enabled publication tracking |
| `pubsimple` | `PubSimple` | Simple publication entries |

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
| `AUTHOR_REFRESH_CHECK_DAYS` | `1` | How often to refresh author data (days) |
| `API_USER_ANON_UUID` | `00000000-0000-0000-0000-000000000000` | UUID for the anonymous (unauthenticated) user |
| `API_USER_ANON_NAME` | `Anonymous API User` | Display name for anonymous user |

#### Task Timeout Tracker (API result caching)

| Variable | Default | Description |
|---|---|---|
| `ARC_NAME` | `author_refresh_check` | Author Refresh Check task name |
| `ARC_DESCRIPTION` | `Author Refresh Check` | Author Refresh Check description |
| `ARC_TIMEOUT_IN_SECONDS` | `86400` | Author Refresh Check timeout (seconds) |
| `PSK_NAME` | `public_signing_key` | Public Signing Key task name |
| `PSK_DESCRIPTION` | `Public Signing Key` | Public Signing Key description |
| `PSK_TIMEOUT_IN_SECONDS` | `86400` | Public Signing Key cache timeout (seconds) |
| `TRL_NAME` | `token_revocation_list` | Token Revocation List task name |
| `TRL_DESCRIPTION` | `Token Revocation List` | Token Revocation List description |
| `TRL_TIMEOUT_IN_SECONDS` | `300` | Token Revocation List cache timeout (seconds) |

#### FABRIC Services

| Variable | Example | Description |
|---|---|---|
| `FABRIC_CORE_API` | `https://uis.fabric-testbed.net/` | FABRIC Core API base URL |
| `FABRIC_CREDENTIAL_MANAGER` | `https://cm.fabric-testbed.net/` | Credential Manager URL |
| `FABRIC_PORTAL` | `https://portal.fabric-testbed.net` | FABRIC Portal base URL (used for project links) |

#### Vouch Proxy

| Variable | Example | Description |
|---|---|---|
| `VOUCH_COOKIE_NAME` | `fabric-service` | Name of the JWT cookie set by Vouch |
| `VOUCH_JWT_SECRET` | `<secret>` | Shared secret for JWT validation |

#### Django

| Variable | Example | Description |
|---|---|---|
| `PYTHONPATH` | `./:./venv:./.venv` | Python module search path |
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
| `POSTGRES_USER` | `postgres` | Database user |
| `POSTGRES_DB` | `postgres` | Database name |
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
| `UWSGI_UID` | `<user_uid>` | uWSGI process UID |
| `UWSGI_GID` | `<user_gid>` | uWSGI process GID |

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
# Start all services
docker compose up -d

# Follow logs
docker compose logs -f

# Stop all services
docker compose down
```

The application is available at:
- `http://localhost:8080` (redirects to HTTPS)
- `https://localhost:8443`

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

# Generate new migrations and start
./run_server.sh -r local-dev -m

# Load fixtures and start
./run_server.sh -r local-dev -l
```

The development server starts at `http://localhost:8000`.

---

## Initial Setup

After the database is up, run these once to initialize required records:

```bash
# Inside the Django container (Docker) or local venv
python manage.py migrate
python manage.py collectstatic --no-input
python manage.py init_anon_api_user
python manage.py init_task_timeout_tracker
```

### Migrate data from PubSimple to Publications

If you have existing `PubSimple` records and want to promote them to full `Publication` entries:

```bash
# Preview what would be imported
python manage.py import_from_pubsimple --dry-run

# Run the import
python manage.py import_from_pubsimple
```

The command preserves original `created`/`modified` timestamps and `created_by`/`modified_by` references. Records with duplicate (title, link) combinations are skipped.

---

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
| `/publications/by-author-uuid/<fabric_uuid>` | All | All publications by a specific FABRIC user |
| `/publications/projects/` | All | Publications grouped by FABRIC project (name, count, link) |
| `/publications/projects/<project_uuid>` | All | All publications for a specific project |
| `/pubsimple/` | All | Browse simple publications |
| `/pubsimple/create/` | Authenticated | Create a simple publication |
| `/pubsimple/<uuid>` | All | Simple publication detail |
| `/pubsimple/<uuid>/update` | Owner, Admins | Edit a simple publication |
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

### Endpoints

#### Publications — `/api/publications`

| Method | URL | Description |
|---|---|---|
| `GET` | `/api/publications` | List publications (paginated) |
| `POST` | `/api/publications` | Create a publication |
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
```

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

#### Simple Publications — `/api/pubsimple`

| Method | URL | Description |
|---|---|---|
| `GET` | `/api/pubsimple` | List simple publications |
| `POST` | `/api/pubsimple` | Create a simple publication |
| `GET` | `/api/pubsimple/<uuid>` | Get a simple publication |
| `PUT` | `/api/pubsimple/<uuid>` | Update a simple publication |
| `DELETE` | `/api/pubsimple/<uuid>` | Delete a simple publication |

**Simple publication schema:**

```json
{
  "authors": ["Smith, Jane", "Doe, John"],
  "link": "https://doi.org/10.1000/example",
  "project_name": "My FABRIC Project",
  "project_uuid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "title": "Using FABRIC for Large-Scale Network Experiments",
  "year": "2024"
}
```

---

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
    │   └── wsgi.py
    ├── apps/
    │   ├── apiuser/              # FABRIC identity & role management
    │   │   ├── models.py         # ApiUser, TaskTimeoutTracker
    │   │   ├── views.py          # apiuser_list, apiuser_detail
    │   │   ├── tests.py
    │   │   ├── urls.py
    │   │   └── management/commands/
    │   │       ├── init_anon_api_user.py
    │   │       └── init_task_timeout_tracker.py
    │   ├── publications/         # Full publication tracking (BibTeX)
    │   │   ├── models.py         # Publication, Author
    │   │   ├── views.py          # publication_*, author_*
    │   │   ├── tests.py
    │   │   ├── urls.py
    │   │   ├── forms.py          # PublicationForm, AuthorForm
    │   │   ├── api/
    │   │   │   ├── viewsets.py   # PublicationViewSet, AuthorViewSet
    │   │   │   ├── serializers.py
    │   │   │   └── validators.py
    │   │   ├── templatetags/
    │   │   │   └── publications_tags.py
    │   │   └── management/commands/
    │   │       └── import_from_pubsimple.py
    │   └── pubsimple/            # Simple publication entries
    │       ├── models.py         # PubSimple
    │       ├── views.py
    │       ├── tests.py
    │       ├── urls.py
    │       ├── fixtures/         # apiuser.json, pubsimple.json
    │       └── api/
    │           ├── viewsets.py   # PubSimpleViewSet
    │           ├── serializers.py
    │           └── validators.py
    ├── utils/
    │   ├── fabric_auth.py        # Cookie & bearer token authentication
    │   ├── core_api.py           # FABRIC Core API wrappers
    │   └── bibtex_utils.py       # BibTeX parsing & generation
    └── templates/
        ├── publicationtrkr/      # base.html, navbar.html, home.html, footer.html
        ├── apiuser/              # apiuser_list.html, apiuser_detail.html
        ├── publications/         # publication_*.html, author_*.html
        └── pubsimple/            # pubsimple_*.html
```
