FROM python:3
MAINTAINER Michael J. Stealey <mjstealey@gmail.com>

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

RUN apt-get update --yes \
  && apt-get install --yes --no-install-recommends \
  postgresql-client \
  && pip install virtualenv \
  && mkdir /code/

RUN  apt-get clean && rm -rf /var/lib/apt/lists/*

# specifies nrig-service UID
RUN useradd -r -u 20049 appuser

WORKDIR /code
VOLUME ["/code"]
ENTRYPOINT ["/code/docker-entrypoint.sh"]