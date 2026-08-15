# clont — read-only monitoring & FinOps agent.
#
#   docker build -t clont:local .
#   docker run --rm -v "$PWD/clont.yaml:/etc/clont/clont.yaml:ro" clont:local
#
# The image ships the agent only: no config, no credentials. Mount the config
# at $CLONT_CONFIG (a ConfigMap on k8s) and supply AWS credentials the usual
# way — IRSA / instance role in the cloud, `-e AWS_*` or a mounted ~/.aws
# locally. The default command is the daemon (`clont run`); override it for a
# one-shot scan:
#
#   docker run --rm -v ... clont:local run --summary - --format text

# --- build: resolve deps into a self-contained venv --------------------------
FROM python:3.14-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /src

# Only what the wheel needs, so dep resolution is cached until they change.
# (hatchling reads the version out of clont/__init__.py, hence the package copy.)
COPY pyproject.toml README.md LICENSE ./
COPY clont ./clont

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install .

# --- runtime -----------------------------------------------------------------
FROM python:3.14-slim

LABEL org.opencontainers.image.title="clont" \
      org.opencontainers.image.description="Read-only multi-cloud monitoring & FinOps agent" \
      org.opencontainers.image.source="https://github.com/clont-app/clont" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CLONT_CONFIG=/etc/clont/clont.yaml

COPY --from=build /opt/venv /opt/venv

# tini as PID 1. The agent loop installs no SIGTERM handler, and a PID-1 process
# ignores signals whose disposition is the default — so without an init, `docker
# stop` / a k8s pod delete would stall for the whole grace period and end in
# SIGKILL. tini forwards the signal to clont, which then exits at once.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

# Unprivileged by default — the agent never needs root, and it only reads.
# /etc/clont is owned by that user so a first run with no mounted config can
# still write itself a default one; /var/lib/clont holds any run output.
RUN useradd --system --create-home --home-dir /var/lib/clont --shell /usr/sbin/nologin --uid 10001 clont \
 && install -d -o clont -g clont /etc/clont

USER 10001
WORKDIR /var/lib/clont

ENTRYPOINT ["/usr/bin/tini", "--", "clont"]
CMD ["run"]
