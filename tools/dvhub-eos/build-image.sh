#!/bin/sh
# build-image.sh – baut das DVhub-EOS-Image aus einem Checkout von dvhub-eos/main (lokal, ohne Push).
#   tools/dvhub-eos/build-image.sh --worktree DIR [--platform linux/arm64] [--image NAME] [--docker-tag TAG]
# 1. Basis: unverändertes Root-Dockerfile des Stands (wie upstream), BUILD_VERSION = Docker-Tag
# 2. native Schicht: image/Dockerfile.native (Wheel aus native/ gebaut, Backend native)
# Der Docker-Tag kommt aus dem Tag dvhub-eos-<version> auf HEAD (ohne Präfix); ohne Tag
# "<upstream-version>-dvhub.dev-<sha>".
set -eu
TOOLS=$(cd "$(dirname "$0")" && pwd)
WT=${DVHUB_EOS_WORKTREE:-}
PLATFORM=linux/arm64
IMAGE=bikinibottomcapital/dvhub-eos
TAG=
while [ $# -gt 0 ]; do
  case "$1" in
    --worktree) WT=$2; shift 2 ;;
    --platform) PLATFORM=$2; shift 2 ;;
    --image) IMAGE=$2; shift 2 ;;
    --docker-tag) TAG=$2; shift 2 ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "unbekannte Option: $1" >&2; exit 64 ;;
  esac
done
[ -n "$WT" ] || { echo "--worktree fehlt" >&2; exit 64; }
[ -z "$(git -C "$WT" status --porcelain --untracked-files=no)" ] || { echo "ABBRUCH: $WT hat Änderungen" >&2; exit 1; }
REV=$(git -C "$WT" rev-parse HEAD)
if [ -n "$TAG" ]; then
  :
elif GT=$(git -C "$WT" describe --exact-match --match 'dvhub-eos-*' HEAD 2>/dev/null); then
  TAG=${GT#dvhub-eos-}
else
  TAG="$(cd "$WT" && python3 scripts/get_version.py)-dvhub.dev-$(git -C "$WT" rev-parse --short=9 HEAD)"
fi
echo "== $IMAGE:$TAG ($PLATFORM, ${REV})"
docker build --platform "$PLATFORM" --build-arg BUILD_VERSION="$TAG" -t "$IMAGE:$TAG-base" "$WT"
docker build --platform "$PLATFORM" --build-arg BASE_IMAGE="$IMAGE:$TAG-base" \
  --build-arg DVHUB_EOS_VERSION="$TAG" --build-arg DVHUB_EOS_REVISION="$REV" \
  -f "$TOOLS/image/Dockerfile.native" -t "$IMAGE:$TAG" "$WT/native"
docker rmi "$IMAGE:$TAG-base" >/dev/null   # nur der Zwischen-Tag; die Schichten gehören zum Image
docker image inspect "$IMAGE:$TAG" --format '{{.Id}} {{.Size}} {{.Architecture}}'
echo "IMAGE=$IMAGE:$TAG"
