# omnigent-runner-test — a throwaway image that mimics Omnigent "hosted
# compute": a Linux container running the `omnigent` host daemon, connecting
# out to a server over the WS tunnel, with the coding-harness CLIs baked in.
#
# Modeled on deploy/docker/Dockerfile's `--target host` stage (see that file
# for the full rationale on each package), trimmed for local testing: no
# kiro-cli / agy / gh pins (not needed for claude/pi/codex smoke tests).
#
# Build from the omnigent-fresh repo ROOT so the COPY paths below resolve:
#   docker build -t omnigent-runner-test \
#                -f .local-test/runner/Dockerfile .

ARG PYTHON_VERSION=3.12
ARG NODE_VERSION=22

# ── Python builder ───────────────────────────────────────
# Installs the omnigent package (editable-equivalent) into a standalone venv,
# mirroring deploy/docker/Dockerfile's `builder` stage. Only the source the
# package needs is copied in (not tests/, web/, .git/, node_modules/) to keep
# the build context small and fast.
FROM python:${PYTHON_VERSION}-slim AS builder

ARG PYPI_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --index-url ${PYPI_INDEX_URL} --no-cache-dir uv

WORKDIR /build

COPY pyproject.toml setup.py ./
COPY LICENSE NOTICE ./
COPY sdks/ ./sdks/
COPY omnigent/ ./omnigent/
COPY examples/ ./examples/

RUN python -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

RUN uv pip install --no-cache-dir --index-url ${PYPI_INDEX_URL} -e .

# ── Node alias stage ─────────────────────────────────────
FROM node:${NODE_VERSION}-slim AS node-runtime

# ── Runner runtime ───────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS runner

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:${PATH}" \
    IS_SANDBOX=1

# git (workspaces/worktrees), tmux (native-harness terminal sessions),
# procps/lsof (native-harness process/port discovery), bubblewrap (mandatory
# OS-sandboxing for native harness terminals on Linux), curl+ca-certificates
# (outbound HTTPS to the LLM backends and to the server's WS tunnel).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git tmux procps lsof bubblewrap curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Node runtime for the harness CLIs (claude / codex / pi are npm packages).
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# The three harness CLIs this test environment covers. Mirrors
# omnigent/onboarding/harness_install.py / deploy/docker/Dockerfile's default
# set (minus kiro-cli, which this test doesn't exercise). Unpinned on purpose,
# same as upstream.
RUN npm install -g --no-audit --no-fund \
      @anthropic-ai/claude-code \
      @openai/codex \
      @earendil-works/pi-coding-agent \
 && npm cache clean --force

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /build /build
RUN pip install --no-cache-dir /build /build/sdks/python-client /build/sdks/ui \
 && pip install --no-cache-dir uv

RUN echo 'export PATH="/opt/venv/bin:${PATH}"' > /etc/profile.d/omnigent-venv.sh

# Pre-create so a single-file bind mount of config.yaml (provider config —
# see .local-test/runner/config.yaml) has a parent dir to land in.
RUN mkdir -p /root/.omnigent

WORKDIR /root

# No ENTRYPOINT — the `docker run` command supplies
# `omnigent host --server ... --non-interactive --no-open` (see RUNNING.md).
CMD ["sleep", "infinity"]
