#!/bin/sh
# Owner root invocation; isolated images and hermetic fixtures ONLY.
# Caller hash-pins this script. Args pin the exact Git archive and wheels.
set -eu
umask 077
fail() { echo "observer_export_qa_failed=$1" >&2; exit 1; }
[ "$(id -u)" = 0 ] || fail root_required
[ "$#" = 3 ] || fail expected_revision_sourcehash_wheelhash
REV=$1
SOURCE_HASH=$2
WHEEL_HASH=$3
case "$REV:$SOURCE_HASH:$WHEEL_HASH" in *[!0-9a-f:]*|'') fail invalid_hex ;; esac
[ "${#REV}" = 40 ] && [ "${#SOURCE_HASH}" = 64 ] && [ "${#WHEEL_HASH}" = 64 ] || fail invalid_hash_length
STAGE=/volume2/clank/feature-phone-clank/build/cops-000089
DOCKER=/var/packages/ContainerManager/target/usr/bin/docker
BASE=sha256:aee27cca626c1b0dfd81c4f9d5be0bfa76f702ea83462ac442f81cd961dd93e3
SOURCE="$STAGE/feature-phone-observer-export-$REV.tar"
WHEELS="$STAGE/observer-pytest-wheels.tar"
[ -d "$STAGE" ] && [ ! -L "$STAGE" ] || fail unsafe_stage
[ -f "$SOURCE" ] && [ ! -L "$SOURCE" ] && [ -f "$WHEELS" ] && [ ! -L "$WHEELS" ] || fail missing_archives
printf '%s  %s\n' "$SOURCE_HASH" "$SOURCE" "$WHEEL_HASH" "$WHEELS" | sha256sum -c - >/dev/null || fail archive_hash_mismatch
[ "$($DOCKER image inspect "$BASE" --format '{{.Id}}')" = "$BASE" ] || fail deployed_base_image_mismatch
TAG="feature-phone-clank:observer-export-cops-000089-$REV"
QA_TAG="feature-phone-clank:observer-export-qa-cops-000089-$REV"
# Never overwrite either image, even on a rerun. Preserve results/context.
if $DOCKER image inspect "$TAG" >/dev/null 2>&1; then fail candidate_tag_already_exists; fi
if $DOCKER image inspect "$QA_TAG" >/dev/null 2>&1; then fail qa_tag_already_exists; fi
CONTEXT=$(mktemp -d "$STAGE/qa-context.XXXXXXXX")
tar -xf "$SOURCE" -C "$CONTEXT"
tar -xf "$WHEELS" -C "$CONTEXT"
[ -z "$(find "$CONTEXT" -type l -print)" ] || fail source_symlink
FINALIZER_HASH=$(sha256sum "$CONTEXT/src/feature_phone_clank/observer_publication.py" | cut -d ' ' -f 1)
$DOCKER build --pull=false --network=none -f "$CONTEXT/deploy/Dockerfile.observer-export" \
  --build-arg "EXPORT_SHA=$REV" --build-arg "FINALIZER_SHA256=$FINALIZER_HASH" -t "$TAG" "$CONTEXT"
IMAGE=$($DOCKER image inspect "$TAG" --format '{{.Id}}')
[ "$($DOCKER image inspect "$IMAGE" --format '{{.Config.User}}')" = 10001:10001 ] || fail candidate_user_mismatch
[ "$($DOCKER image inspect "$IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = "$REV" ] || fail candidate_revision_mismatch
RESULTS=$(mktemp -d "$STAGE/qa-results.XXXXXXXX")
# Verify installed modules, not a source-tree PYTHONPATH supplied by tests.
$DOCKER run --rm --pull never --network none --read-only --user 10001:10001 \
  --cap-drop ALL --security-opt no-new-privileges --entrypoint python "$IMAGE" \
  -c 'import hashlib,os,feature_phone_clank.observer_publication as p; print("exporter_revision="+os.environ["FEATURE_PHONE_CLANK_SOURCE_REVISION"]); print("finalizer_sha256="+hashlib.sha256(open(p.__file__,"rb").read()).hexdigest()); print("uid="+str(os.getuid()))' > "$RESULTS/identity.txt"
grep -qx "exporter_revision=$REV" "$RESULTS/identity.txt" || fail runtime_revision_mismatch
grep -qx "finalizer_sha256=$FINALIZER_HASH" "$RESULTS/identity.txt" || fail runtime_finalizer_mismatch
grep -qx uid=10001 "$RESULTS/identity.txt" || fail runtime_uid_mismatch
$DOCKER build --pull=false --network=none -f "$CONTEXT/deploy/Dockerfile.observer-export-qa" \
  --build-arg "CANDIDATE_IMAGE=$IMAGE" -t "$QA_TAG" "$CONTEXT"
QA_IMAGE=$($DOCKER image inspect "$QA_TAG" --format '{{.Id}}')
set +e
$DOCKER run --rm --pull never --network none --read-only --user 0:0 \
  --cap-drop ALL --cap-add CHOWN --cap-add FOWNER --cap-add DAC_OVERRIDE \
  --security-opt no-new-privileges --tmpfs /tmp:rw,nosuid,nodev,size=512m \
  "$QA_IMAGE" -q -p no:cacheprovider --tb=short > "$RESULTS/tests.log" 2>&1
EXIT=$?
set -e
{
  echo "source_revision=$REV"
  echo "source_archive_sha256=$SOURCE_HASH"
  echo "candidate_image=$IMAGE"
  echo "qa_image=$QA_IMAGE"
  echo "finalizer_sha256=$FINALIZER_HASH"
  cat "$RESULTS/identity.txt"
  tail -n 20 "$RESULTS/tests.log"
  echo "linux_qa_exit=$EXIT"
  echo "results=$RESULTS"
} > "$RESULTS/summary.txt"
# Only test/identity summary becomes operator-readable. Full logs remain private.
chmod 0644 "$RESULTS/summary.txt"
cat "$RESULTS/summary.txt"
[ "$EXIT" = 0 ] || fail linux_qa_nonzero
echo "observer_export_linux_qa_pass image=$IMAGE results=$RESULTS"
