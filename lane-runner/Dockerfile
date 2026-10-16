# syntax=docker/dockerfile:1.7
# Build from this repository root with the two private checkouts as named
# BuildKit contexts. Credentials never enter the build.
FROM kindness AS kindness_source
FROM drip-safe AS drip_safe_source
FROM node:22-bookworm-slim

ARG KINDNESS_REMOTE=https://github.com/chosenvdot/drip-kindness.git
ARG KINDNESS_REF
ARG DRIP_SAFE_REF
ARG OPENCODE_VERSION=2.0.30

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates git python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

COPY --from=kindness_source / /opt/drip-kindness/
COPY --from=drip_safe_source / /opt/drip-safe/

RUN test -n "$KINDNESS_REF" \
    && test -n "$DRIP_SAFE_REF" \
    && rm -rf /opt/drip-kindness/.git /opt/drip-safe/.git \
    && git -C /opt/drip-kindness init -b main \
    && git -C /opt/drip-kindness config user.name "drip-ai lane runner" \
    && git -C /opt/drip-kindness config user.email "lane-runner@localhost" \
    && git -C /opt/drip-kindness add -A \
    && git -C /opt/drip-kindness commit -m "Import ranked lane runner source" \
    && git -C /opt/drip-kindness remote add origin "$KINDNESS_REMOTE"

LABEL org.opencontainers.image.source-ref="$KINDNESS_REF" \
      org.opencontainers.image.drip-safe-ref="$DRIP_SAFE_REF"

RUN npm install --global "@opencode/cli@$OPENCODE_VERSION"
RUN python3 -m venv /opt/drip-safe/.venv \
    && /opt/drip-safe/.venv/bin/pip install --no-cache-dir "uv==0.9.28" \
    && /opt/drip-safe/.venv/bin/uv sync --project /opt/drip-safe --group dev --locked

COPY lane-runner/entrypoint.sh /usr/local/bin/drip-ai-lane-runner
RUN chmod 755 /usr/local/bin/drip-ai-lane-runner

ENV PATH=/opt/drip-safe/.venv/bin:$PATH \
    DRIP_TEST_PROJECT=/opt/drip-safe \
    KINDNESS_ROOT=/opt/drip-kindness
WORKDIR /opt/drip-kindness
ENTRYPOINT ["/usr/local/bin/drip-ai-lane-runner"]
