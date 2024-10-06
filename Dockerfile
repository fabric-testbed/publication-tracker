FROM python:3
LABEL org.opencontainers.image.authors="Michael J. Stealey <mjstealey@gmail.com>"

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Install postgresql-client, virtualenv, add /code directory, and add appuser
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    && postgresql-client \
    && pip install virtualenv \
    && mkdir /code/ \
    && useradd -r -u 20049 appuser

WORKDIR /code
VOLUME ["/code"]
ENTRYPOINT ["/code/docker-entrypoint.sh"]
