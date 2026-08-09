# Feature Phone Clank (FEATURE-01) — Linux AMD64 staging image.
# Experimental / actively developing — not approved for production. Only
# hmd-nokia is in config/scope.yaml's production_collectors as of this build;
# other collectors, if any, do not persist until explicitly promoted there.
FROM python:3.12-slim-bookworm

# Full Git SHA this image was built from. Must be passed at build time (e.g.
# `--build-arg GIT_REVISION=$(git rev-parse HEAD)`, or via docker-compose.yml's
# build.args, defaulting to "unknown" for local/non-Git builds). Never derived
# from a .git directory at runtime. Pattern proven on OEM Radar / Chinese Tech Wire.
ARG GIT_REVISION=unknown
LABEL clank.id="feature-phone-clank" \
      org.opencontainers.image.revision="${GIT_REVISION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FEATURE_PHONE_CLANK_RELEASE_CHANNEL=experimental \
    FEATURE_PHONE_CLANK_SOURCE_REVISION=${GIT_REVISION}

WORKDIR /app

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin clank

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config

RUN pip install --upgrade pip \
    && pip install . \
    && mkdir -p /app/data \
    && chown -R clank:clank /app

USER clank

HEALTHCHECK --interval=60s --timeout=15s --start-period=20s --retries=3 \
    CMD ["feature-phone-clank", "health"]

ENTRYPOINT ["feature-phone-clank"]
CMD ["run"]
