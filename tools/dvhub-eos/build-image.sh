#!/bin/sh
# build-image.sh – baut das DVhub-EOS-Image aus einem Checkout von dvhub-eos/main (lokal, ohne Push).
#   tools/dvhub-eos/build-image.sh --worktree DIR [--platform linux/arm64] [--image NAME] [--docker-tag TAG]
#                                  [--allow-python]
# 1. Basis: unverändertes Root-Dockerfile des Stands (wie upstream), BUILD_VERSION = Docker-Tag
# 2. native Schicht: image/Dockerfile.native (Wheel aus native/ gebaut, Backend native)
# 3. Rauchtest im Image (image/native-smoke.py): Kern installiert und für die GENETIC-Quellen
#    zertifiziert, ein kurzer Lauf mit Netzbezugsgrenze und E-Auto rechnet nativ, ohne Rückfall.
#    Scheitert er, wird das Image wieder entfernt und der Bau schlägt fehl – DVhub liefert nie
#    ein DVhub-EOS aus, das in Python rechnet. --allow-python behält es trotzdem (nur Entwicklungs-
#    und Messbauten; nie für ein Release-Tag dvhub-eos-*).
# Der Docker-Tag kommt aus dem Tag dvhub-eos-<version> auf HEAD (ohne Präfix); ohne Tag
# "<upstream-version>-dvhub.dev-<sha>".
set -eu
TOOLS=$(cd "$(dirname "$0")" && pwd)
WT=${DVHUB_EOS_WORKTREE:-}
PLATFORM=linux/arm64
IMAGE=bikinibottomcapital/dvhub-eos
TAG=
ALLOW_PYTHON=0
while [ $# -gt 0 ]; do
  case "$1" in
    --worktree) WT=$2; shift 2 ;;
    --platform) PLATFORM=$2; shift 2 ;;
    --image) IMAGE=$2; shift 2 ;;
    --docker-tag) TAG=$2; shift 2 ;;
    --allow-python) ALLOW_PYTHON=1; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unbekannte Option: $1" >&2; exit 64 ;;
  esac
done
[ -n "$WT" ] || { echo "--worktree fehlt" >&2; exit 64; }
[ -z "$(git -C "$WT" status --porcelain --untracked-files=no)" ] || { echo "ABBRUCH: $WT hat Änderungen" >&2; exit 1; }
REV=$(git -C "$WT" rev-parse HEAD)
RELEASE=0
if git -C "$WT" describe --exact-match --match 'dvhub-eos-*' HEAD >/dev/null 2>&1; then RELEASE=1; fi
if [ "$ALLOW_PYTHON" = 1 ] && [ "$RELEASE" = 1 ]; then
  echo "ABBRUCH: --allow-python nie für ein Release-Tag (HEAD trägt dvhub-eos-*)" >&2; exit 1
fi
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

echo "== Rauchtest: nativer Kern im Image"
if docker run --rm --platform "$PLATFORM" --entrypoint /opt/venv/bin/python \
     -v "$TOOLS/image:/smoke:ro" "$IMAGE:$TAG" /smoke/native-smoke.py; then
  echo "== nativer Kern aktiv, kein Python-Rückfall"
elif [ "$ALLOW_PYTHON" = 1 ]; then
  echo "== WARNUNG: dieses Image rechnet in Python (--allow-python, nur Entwicklung/Messung)" >&2
else
  docker rmi "$IMAGE:$TAG" >/dev/null || true
  echo "ABBRUCH: Rauchtest gescheitert – das Image rechnete in Python und wurde entfernt" >&2
  exit 1
fi
docker image inspect "$IMAGE:$TAG" --format '{{.Id}} {{.Size}} {{.Architecture}}'
echo "IMAGE=$IMAGE:$TAG"
