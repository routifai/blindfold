# omnigent-runner-ro-e2e — throwaway image for the rollover E2E proof.
# Mirrors dev/blindfold/runner.Dockerfile (see rollover-muse), trimmed to the
# two harnesses rollover phase 1/2 covers (claude, codex).
#
# Build from the repo ROOT so the COPY paths below resolve:
#   docker build -t omnigent-runner-ro-e2e -f dev/rollover/runner.Dockerfile .

ARG PYTHON_VERSION=3.12
ARG NODE_VERSION=22

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

FROM node:${NODE_VERSION}-slim AS node-runtime

FROM python:${PYTHON_VERSION}-slim AS runner

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:${PATH}" \
    IS_SANDBOX=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git tmux procps lsof bubblewrap curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# claude-native + codex-native only — the two harnesses rollover phase 1/2 covers.
RUN npm install -g --no-audit --no-fund \
      @anthropic-ai/claude-code \
      @openai/codex \
 && npm cache clean --force

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /build /build
RUN pip install --no-cache-dir /build /build/sdks/python-client /build/sdks/ui \
 && pip install --no-cache-dir uv

RUN echo 'export PATH="/opt/venv/bin:${PATH}"' > /etc/profile.d/omnigent-venv.sh

RUN mkdir -p /root/.omnigent

WORKDIR /root

CMD ["sleep", "infinity"]
