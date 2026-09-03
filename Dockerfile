# Base image and uv are both pinned by digest (#31 finding 9). `FROM python:3` plus an
# installer fetched from the network and piped to `sh` meant two moving parts could
# change what a rebuild shipped without a line of this repo changing.
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

LABEL org.opencontainers.image.title="publication-tracker"
LABEL org.opencontainers.image.source="https://github.com/fabric-testbed/publication-tracker-dev"

# UV_PROJECT_ENVIRONMENT puts the virtualenv at /opt/venv, outside /code. /code is the
# host's git checkout, bind-mounted and read-only from this release on, so a venv under
# it was both a write into the deploy checkout and, now, impossible.
#
# PATH carries /opt/venv/bin, which is what makes a bare `python` in run_server.sh the
# project interpreter with no venv to activate. Anything cron runs still needs the
# absolute path -- cron inherits no PATH at all.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH

# uv from its own published image rather than a remote installer run at build time.
COPY --from=ghcr.io/astral-sh/uv:0.12.9@sha256:8b940d3a9d65bed080436972241af2e21c84b5e8c9193f7014ed71479ee795ff \
     /uv /usr/local/bin/uv

# Only the two files the resolve reads. The project is `source = { virtual = "." }` in
# uv.lock -- it is never installed into the venv, it is imported from the /code mount --
# so the dependency layer does not depend on any application source and is not
# invalidated by an ordinary code change.
WORKDIR /opt/build
COPY pyproject.toml uv.lock ./

# cron is not in the base image (only flock) and the user-sync sidecar reuses this image
# to run it; postgresql-client supplies the pg_isready both entrypoints wait on. The
# compilers build uwsgi, which publishes an sdist and no wheel, and are purged in the
# same layer so they are not in the image that ships. libpcre2-8-0 stays because the
# uwsgi binary links it.
#
# --locked, not --frozen. Both stop the re-resolve, but --frozen installs whatever the
# lock says and asks no questions, so a pyproject.toml edited without a `uv lock` ships
# a dependency set that does not match the declared one -- silently, exit 0. --locked
# refuses to build at all in that case ("the lockfile needs to be updated"), which is
# the answer you want from a release build. Before either, a restart could pull a
# brand-new release of any of the 14 unpinned dependencies and run its install hooks as
# root.
RUN set -eux; \
    apt-get update; \
    apt-get install --yes --no-install-recommends \
      cron \
      postgresql-client \
      libpcre2-8-0; \
    apt-get install --yes --no-install-recommends \
      gcc \
      libc6-dev \
      libpcre2-dev; \
    uv sync --locked --no-dev; \
    apt-get purge --yes --auto-remove gcc libc6-dev libpcre2-dev; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

# uid 20049 is nrig-service on the production host. That is not decoration: it is what
# lets a non-root container write the static/ and media/ sub-mounts on the host
# checkout, and what keeps `git status` there clean after a boot.
#
# The gid is pinned to 20049 as well, which `useradd -r` alone does not do -- it picks
# the next free system gid (996 here). A container running 20049:996 writes files the
# host sees as `nrig-service:<a group that does not exist>`, and no host file can be
# shared with the container by group, which is how a .env readable by both the operator
# account and the container has to work when they are not the same identity.
RUN groupadd -r -g 20049 appuser \
 && useradd -r -u 20049 -g appuser appuser \
 && mkdir -p /code \
 && chown appuser:appuser /opt/venv

USER appuser

WORKDIR /code
VOLUME ["/code"]
ENTRYPOINT ["/code/docker-entrypoint.sh"]
