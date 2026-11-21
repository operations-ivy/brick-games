# brick-games: the catalog page (`web`) and the nightly scan (`scan`), one image.
# Built and pushed by brick-cicd-config's build-image Jenkins job (a reproducible
# rootless BuildKit build on brick2000): IMAGE=whitepatrick/brick-games,
# REF=<full commit>, TAG=<version>.
#
# The base is pinned by digest (the same as presto's) and dependencies come
# from poetry.lock. Bump the base deliberately, with the digest from:
#   docker buildx imagetools inspect python:3.11-slim-bookworm
ARG PYTHON=python:3.11-slim-bookworm@sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b

FROM ${PYTHON} AS deps
ARG POETRY_VERSION=2.1.2
ENV PIP_DEFAULT_TIMEOUT=120 POETRY_NO_INTERACTION=1 VIRTUAL_ENV=/venv PATH=/venv/bin:$PATH
# One package at a time: the parallel installer races on the umask (wigle-sync).
ENV POETRY_INSTALLER_PARALLEL=false
RUN pip install --no-cache-dir "poetry==${POETRY_VERSION}"
WORKDIR /code
COPY pyproject.toml poetry.lock /code/
# Poetry leaves its clone of a git dependency in /venv/src; the package is in
# site-packages, and the clone's .git/index changes every build (and the digest).
RUN python -m venv --without-pip /venv && poetry install --no-root --only main && rm -rf /venv/src

FROM ${PYTHON}
COPY --from=deps /venv /venv
# Read-only root filesystem in the cluster, so no .pyc files.
ENV VIRTUAL_ENV=/venv PATH=/venv/bin:$PATH PYTHONPATH=/code PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /code
# An explicit file list: a new module must be added here (tests/test_image.py
# checks every module the app imports is listed).
COPY brick_games/__init__.py brick_games/__main__.py brick_games/cache.py brick_games/catalog.py brick_games/guides.py \
     brick_games/library.py brick_games/manuals.py \
     brick_games/ratings.py brick_games/saves.py brick_games/saves_agent.py brick_games/saves_web.py \
     brick_games/sources.py brick_games/strategy.py brick_games/web.py brick_games/systems.yaml \
     brick_games/saves.yaml /code/brick_games/
COPY brick_games/static/index.html /code/brick_games/static/

USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["python", "-m", "brick_games"]
