#!/bin/sh
# Feature Phone Clank — EXPERIMENTAL SOAK cron entry point.
# Usage: deploy_run_experimental.sh <itel|lava>
# Mirrors deploy_run.sh's pattern (.deployed-id -> IMAGE_TAG) but targets
# docker-compose.experimental.yml and a single named service per source,
# so itel and lava can run on independent cron cadences while sharing one
# image/volume/lock. Never touches docker-compose.staging.yml or the
# production feature_phone_clank_staging_data volume.
set -eu
cd "$(dirname "$0")/.."

service="${1:?usage: deploy_run_experimental.sh <itel|lava>}"
case "$service" in
  itel|lava) ;;
  *) echo "unknown service: $service (expected 'itel' or 'lava')" >&2; exit 1 ;;
esac

export IMAGE_TAG
IMAGE_TAG="$(cat .deployed-id-experimental)"
exec docker compose -f docker-compose.experimental.yml run --rm "$service"
