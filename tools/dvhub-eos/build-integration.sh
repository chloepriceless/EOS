#!/usr/bin/env bash
# build-integration.sh – baut den Zweig dvhub-eos/main neu:
#   upstream/main + alle Zweige aus stack.txt (in dieser Reihenfolge, je ein --no-ff-Merge)
#   + overlay/ (DVhub-eigene Zusatzdateien, z. B. CI für dvhub-eos/main)
#   + neu erzeugte openapi.json und docs/_generated/* (make gen-docs)
# und setzt auf Wunsch das Tag dvhub-eos-<upstream-version>-dvhub.<N>.
#
# Konflikte: git rerere mit den versionierten Auflösungen aus rr-cache/ (dieses Verzeichnis).
# Generierte Dateien werden nie zusammengeführt (merge.gitattributes), sondern am Ende neu erzeugt.
# Bleibt ein Konflikt ungelöst, bricht das Skript ab und lässt den Merge im Bau-Worktree stehen.
#
# Aufruf (aus einem Checkout des Zweigs dvhub-eos/tooling):
#   tools/dvhub-eos/build-integration.sh [--worktree DIR] [--tag] [--no-fetch]
#   tools/dvhub-eos/build-integration.sh --record [--worktree DIR]
#       nach einer Handauflösung: Auflösung für rerere festhalten und nach rr-cache/ übernehmen
#
# Umgebung: DVHUB_EOS_WORKTREE (Bau-Worktree, Vorgabe <repo>/../eos-dvhub-build),
#           UPSTREAM_REMOTE (upstream), FORK_REMOTE (fork), BASE_REF (<UPSTREAM_REMOTE>/main),
#           TARGET_BRANCH (dvhub-eos/main), PYTHON (python3, >= 3.11), UV_CACHE_DIR (an uv durchgereicht).
# Commit-Identität: wie in git konfiguriert oder per GIT_AUTHOR_*/GIT_COMMITTER_*.
set -euo pipefail

TOOLS=$(cd "$(dirname "$0")" && pwd)
STACK="$TOOLS/stack.txt"
RR_DIR="$TOOLS/rr-cache"
OVERLAY="$TOOLS/overlay"
ATTR="$TOOLS/merge.gitattributes"

UPSTREAM_REMOTE=${UPSTREAM_REMOTE:-upstream}
FORK_REMOTE=${FORK_REMOTE:-fork}
BASE_REF=${BASE_REF:-$UPSTREAM_REMOTE/main}
TARGET_BRANCH=${TARGET_BRANCH:-dvhub-eos/main}
PYTHON=${PYTHON:-python3}
TAG_PREFIX=dvhub-eos-

DO_FETCH=1 DO_TAG=0 DO_RECORD=0
WT=${DVHUB_EOS_WORKTREE:-}
while [ $# -gt 0 ]; do
  case "$1" in
    --worktree) WT=$2; shift 2 ;;
    --tag) DO_TAG=1; shift ;;
    --no-fetch) DO_FETCH=0; shift ;;
    --record) DO_RECORD=1; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unbekannte Option: $1" >&2; exit 64 ;;
  esac
done

# Die Commits dieses Skripts sind erzeugt (Merges, overlay/, gen-docs); ihr Inhalt wird in den
# PR-Zweigen bzw. im Zweig dvhub-eos/tooling geprüft. Kein Codex-Review je Neubau.
export CODEX_REVIEW_SKIP=1

die() { echo "ABBRUCH: $*" >&2; exit 1; }
say() { echo "== $*"; }

case "$WT" in ""|/*) ;; *) WT="$PWD/$WT" ;; esac   # relativ zum Aufrufer, nicht zum Repo

COMMON=$(cd "$TOOLS" && git rev-parse --path-format=absolute --git-common-dir)
REPO_TOP=$(dirname "$COMMON")
WT=${WT:-$(dirname "$REPO_TOP")/eos-dvhub-build}
RR_GIT="$COMMON/rr-cache"

# Alle Git-Aufrufe im Bau-Worktree: rerere und Merge-Treiber nur für diesen Aufruf, nichts landet
# in der (mit anderen Worktrees geteilten) Repo-Konfiguration.
g() {
  git -C "$WT" -c rerere.enabled=true -c rerere.autoUpdate=true \
    -c core.attributesFile="$ATTR" \
    -c merge.dvhub-eos-generated.name="DVhub-EOS: generierte Datei, wird neu erzeugt" \
    -c merge.dvhub-eos-generated.driver=true "$@"
}

# Versionierte Auflösungen <-> .git/rr-cache. rr-cache/ ist maßgeblich: rr_import überschreibt den
# gemeinsamen Cache bei jedem Bau; zurück nach rr-cache/ geht es nur mit --record.
rr_import() {
  [ -d "$RR_DIR" ] || return 0
  for d in "$RR_DIR"/*/; do
    [ -d "$d" ] || continue
    id=$(basename "$d")
    mkdir -p "$RR_GIT/$id"
    cp -p "$d"/preimage* "$d"/postimage* "$RR_GIT/$id/"
  done
}
rr_export() {
  [ -d "$RR_GIT" ] || return 0
  n=0
  for d in "$RR_GIT"/*/; do
    ls "$d"/postimage* >/dev/null 2>&1 || continue
    id=$(basename "$d")
    for f in "$d"/preimage* "$d"/postimage*; do
      if ! cmp -s "$f" "$RR_DIR/$id/$(basename "$f")"; then
        mkdir -p "$RR_DIR/$id"
        cp -p "$f" "$RR_DIR/$id/"
        n=$((n + 1))
      fi
    done
  done
  [ "$n" -eq 0 ] || say "$n Datei(en) mit Auflösungen nach $RR_DIR übernommen – bitte im Zweig dvhub-eos/tooling committen"
}

if [ "$DO_RECORD" = 1 ]; then
  [ -d "$WT" ] || die "Bau-Worktree $WT fehlt"
  g rerere
  left=$(g rerere remaining)
  [ -z "$left" ] || die "noch ungelöst: $left"
  rr_export
  echo "Danach: im Bau-Worktree 'git add' + 'git commit --no-edit', dann dieses Skript erneut ohne --record starten."
  exit 0
fi

# ---------- Vorbereitung ----------
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "$PYTHON ist älter als 3.11"
command -v uv >/dev/null || die "uv fehlt (für make gen-docs)"
[ -f "$STACK" ] || die "$STACK fehlt"

if [ "$DO_FETCH" = 1 ]; then
  say "hole $UPSTREAM_REMOTE und $FORK_REMOTE"
  git -C "$REPO_TOP" fetch --quiet "$UPSTREAM_REMOTE"
  git -C "$REPO_TOP" fetch --quiet "$FORK_REMOTE"
  # Release-Tags zeigen nach einem Neubau auf Commits außerhalb jedes Zweigs; ohne ausdrückliches
  # Holen fehlen sie und das Skript vergäbe ein schon veröffentlichtes dvhub.<N> erneut.
  git -C "$REPO_TOP" fetch --quiet --no-prune "$FORK_REMOTE" "refs/tags/$TAG_PREFIX*:refs/tags/$TAG_PREFIX*"
fi
BASE_SHA=$(git -C "$REPO_TOP" rev-parse --verify "$BASE_REF^{commit}")

# stack.txt lesen: Ref, Herkunft, optional Pin
REFS=() ORIGINS=() SHAS=()
set -f
while IFS= read -r line || [ -n "$line" ]; do
  # Kommentar = '#' am Zeilenanfang oder nach Leerraum ('upstream#1518' bleibt ganz)
  line=$(printf '%s\n' "$line" | sed -E 's/(^|[[:space:]])#.*$//')
  # shellcheck disable=SC2086
  set -- $line
  [ $# -eq 0 ] && continue
  ref=$1 origin=${2:-} pin=${3:-}
  case "$origin" in
    upstream\#[0-9]*|fork\#[0-9]*) ;;
    *) die "stack.txt: Herkunft '$origin' bei $ref (erlaubt: upstream#<nr>, fork#<nr>)" ;;
  esac
  tip=$(git -C "$REPO_TOP" rev-parse --verify --quiet "$ref^{commit}") || die "stack.txt: $ref unbekannt (gepusht? geholt?)"
  sha=$tip
  if [ -n "$pin" ]; then
    sha=$(git -C "$REPO_TOP" rev-parse --verify --quiet "$pin^{commit}") || die "stack.txt: Pin $pin von $ref unbekannt"
    git -C "$REPO_TOP" merge-base --is-ancestor "$sha" "$tip" || die "stack.txt: Pin $pin liegt nicht in $ref"
  fi
  REFS+=("$ref") ORIGINS+=("$origin") SHAS+=("$sha")
done < "$STACK"
set +f
[ ${#REFS[@]} -gt 0 ] || die "stack.txt ist leer"

# Bau-Worktree: frisch anlegen oder sauber vorfinden
if [ ! -e "$WT/.git" ]; then
  say "lege Bau-Worktree $WT an"
  git -C "$REPO_TOP" worktree add --detach "$WT" "$BASE_SHA" >/dev/null
fi
[ -z "$(git -C "$WT" status --porcelain --untracked-files=no)" ] || die "Bau-Worktree $WT hat Änderungen oder einen offenen Merge"
if git -C "$WT" rev-parse -q --verify MERGE_HEAD >/dev/null; then die "offener Merge in $WT"; fi

rr_import

# Reproduzierbar: alle Commits des Laufs tragen den jüngsten Commit-Zeitpunkt der Eingänge. Gleiche
# Eingänge (und gleiche Identität) ergeben so denselben Commit – und dieselbe Versionszeile in den
# generierten Dateien, die EOS aus dem Datum des jüngsten Commits bildet.
STAMP=$(git -C "$REPO_TOP" log -1 --no-walk=sorted --format=%ct "$BASE_SHA" "${SHAS[@]}" | sort -n | tail -1)
export GIT_AUTHOR_DATE="@$STAMP +0000" GIT_COMMITTER_DATE="@$STAMP +0000"

say "setze $TARGET_BRANCH auf $BASE_REF ($(git -C "$REPO_TOP" log -1 --format='%h %s' "$BASE_SHA"))"
git -C "$WT" checkout --quiet -B "$TARGET_BRANCH" "$BASE_SHA"

# Upstream-Version nach dem EOS-Schema (src/akkudoktoreos/core/version.py, PEP 440:
# x.y.z | x.y.zrcN | x.y.z.dev<JJMMTTHH><hash>), berechnet am unveränderten upstream-Stand.
UPSTREAM_VERSION=$(cd "$WT" && "$PYTHON" scripts/get_version.py)
case "$UPSTREAM_VERSION" in
  [0-9]*.[0-9]*.[0-9]*) ;;
  *) die "unerwartete upstream-Version '$UPSTREAM_VERSION'" ;;
esac
say "upstream-Version $UPSTREAM_VERSION"

# ---------- Merges ----------
for i in "${!REFS[@]}"; do
  ref=${REFS[$i]} origin=${ORIGINS[$i]} sha=${SHAS[$i]}
  say "merge $ref ($origin) @ ${sha:0:9}"
  if g merge --quiet --no-ff --no-edit -m "Merge $ref ($origin) into $TARGET_BRANCH" "$sha"; then
    continue
  fi
  unresolved=$(g diff --name-only --diff-filter=U)
  if [ -n "$unresolved" ]; then
    echo "Ungelöste Konflikte beim Merge von $ref:" >&2
    echo "$unresolved" | sed 's/^/  /' >&2
    cat >&2 <<EOF
Der Merge bleibt in $WT stehen. Auflösen, dann:
  $0 --record --worktree $WT      # Auflösung festhalten (rerere) und nach rr-cache/ übernehmen
  git -C $WT add -A && git -C $WT commit --no-edit
  $0 --worktree $WT ...            # neu bauen; rerere spielt die Auflösung ein
Verwerfen: git -C $WT merge --abort
EOF
    exit 2
  fi
  # rerere hat alles gelöst (autoUpdate hat es vorgemerkt) – oder der Merge scheiterte anders
  g rev-parse -q --verify MERGE_HEAD >/dev/null || die "Merge von $ref gescheitert (siehe oben)"
  say "  Konflikte per rerere (rr-cache/) gelöst"
  g commit --quiet --no-edit
done

# Keine Konfliktmarker durchrutschen lassen (z. B. aus einer fehlerhaften Auflösung)
markers=$(git -C "$WT" grep -lIE '^(<<<<<<<|>>>>>>>)( |$)' HEAD -- || true)
[ -z "$markers" ] || die "Konfliktmarker im Ergebnis: $markers"

# ---------- Overlay ----------
if [ -d "$OVERLAY" ] && [ -n "$(ls -A "$OVERLAY")" ]; then
  say "overlay/ übernehmen"
  (cd "$OVERLAY" && find . -type f -print) | while IFS= read -r f; do
    f=${f#./}
    mkdir -p "$WT/$(dirname "$f")"
    cp -p "$OVERLAY/$f" "$WT/$f"
    git -C "$WT" add -- "$f"
  done
  if ! git -C "$WT" diff --cached --quiet; then
    git -C "$WT" commit --quiet -m "chore(dvhub-eos): add DVhub-EOS integration files (tools/dvhub-eos/overlay)"
  fi
fi

# ---------- generierte Dateien ----------
say "make gen-docs"
GEN_LOG="$COMMON/dvhub-eos-gen-docs.log"
(cd "$WT" && make gen-docs UV_PYTHON="$PYTHON" >"$GEN_LOG" 2>&1) || die "make gen-docs gescheitert, siehe $GEN_LOG"
git -C "$WT" add -A openapi.json docs/_generated
if ! git -C "$WT" diff --cached --quiet; then
  git -C "$WT" commit --quiet -m "chore(dvhub-eos): regenerate openapi.json and docs/_generated"
fi
[ -z "$(git -C "$WT" status --porcelain --untracked-files=no)" ] || die "make gen-docs hat weitere Dateien geändert: $(git -C "$WT" status --porcelain --untracked-files=no)"

HEAD_SHA=$(git -C "$WT" rev-parse HEAD)
say "$TARGET_BRANCH = ${HEAD_SHA:0:9}"

# ---------- Tag ----------
if [ "$DO_TAG" = 1 ]; then
  [ -z "$(git -C "$TOOLS" status --porcelain -- .)" ] || die "tools/dvhub-eos hat uncommittete Änderungen – erst committen (das Tag nennt den Tooling-Stand)"
  TOOLING_SHA=$(git -C "$TOOLS" rev-parse HEAD)
  # gleicher Baum wie das letzte Tag dieser upstream-Version -> kein neues Tag
  last=$(git -C "$REPO_TOP" tag -l "$TAG_PREFIX$UPSTREAM_VERSION-dvhub.*" | sed 's/.*-dvhub\.//' | sort -n | tail -1)
  if [ -n "$last" ] && [ "$(git -C "$REPO_TOP" rev-parse "$TAG_PREFIX$UPSTREAM_VERSION-dvhub.$last^{tree}")" = "$(git -C "$WT" rev-parse 'HEAD^{tree}')" ]; then
    TAG="$TAG_PREFIX$UPSTREAM_VERSION-dvhub.$last"
    say "Baum unverändert gegenüber $TAG – kein neues Tag"
  else
    N=$(( ${last:-0} + 1 ))
    TAG="$TAG_PREFIX$UPSTREAM_VERSION-dvhub.$N"
    {
      echo "DVhub-EOS $UPSTREAM_VERSION-dvhub.$N"
      echo
      echo "base:    $BASE_REF $(git -C "$REPO_TOP" rev-parse "$BASE_SHA")"
      for i in "${!REFS[@]}"; do
        printf 'stack:   %-14s %s %s\n' "${ORIGINS[$i]}" "${SHAS[$i]}" "${REFS[$i]}"
      done
      echo "tooling: dvhub-eos/tooling $TOOLING_SHA"
      echo "docker:  bikinibottomcapital/dvhub-eos:$UPSTREAM_VERSION-dvhub.$N"
      echo "pep440:  $UPSTREAM_VERSION+dvhub.$N"
    } | git -C "$WT" tag -a -F - "$TAG" HEAD
    say "Tag $TAG"
  fi
  echo "DOCKER_TAG=${TAG#"$TAG_PREFIX"}"
fi

cat <<EOF
Fertig. Nächste Schritte:
  Prüfen:   Suite im Bau-Worktree ($WT)
  Push:     git -C $WT push --force-with-lease $FORK_REMOTE $TARGET_BRANCH   (+ ggf. das Tag)
EOF
