#!/usr/bin/env bash
# upstream-watch.sh – meldet upstream-Änderungen, die den nativen GENETIC-Kern betreffen.
#
# Holt upstream/main und listet die Commits seit der gepinnten Basis (Zeile '@base' in stack.txt),
# die fingerabdruck-relevante Dateien ändern: die Quellen aus FINGERPRINT_SOURCES/FINGERPRINT_DATA
# von src/akkudoktoreos/optimization/genetic/native.py, native.py selbst und native/. Jede solche
# Änderung ändert den Quell-Fingerabdruck; ohne Kern-Port rechnete ein DVhub-EOS auf neuer Basis
# in Python. Je upstream-PR (Squash-Merge "(#<nr>)" im Betreff, sonst je Commit) legt das Skript
# ein Ledger-Ticket an – idempotent: steht schon ein Ticket "Kern nachziehen: upstream <PR/commit> "
# im Ledger, kommt kein zweites. Der Ledger-Sync spiegelt die Tickets als Issues.
#
# Aufruf (aus einem Checkout des Zweigs dvhub-eos/tooling):
#   tools/dvhub-eos/upstream-watch.sh [--dry-run] [--no-fetch]
#
# Umgebung: UPSTREAM_REMOTE (upstream), FORK_REMOTE (fork), LEDGER (ledger.py des Orchestrators),
#           LEDGER_TICKETS (dessen tickets.json), PYTHON (python3).
# Täglicher Lauf: launchd/net.dvhub.eos-upstream-watch.plist (siehe README.md).
set -euo pipefail

TOOLS=$(cd "$(dirname "$0")" && pwd)
STACK=${DVHUB_EOS_STACK:-$TOOLS/stack.txt}
UPSTREAM_REMOTE=${UPSTREAM_REMOTE:-upstream}
FORK_REMOTE=${FORK_REMOTE:-fork}
LEDGER=${LEDGER:-$HOME/Projects/orchestrator/.planning/ledger/ledger.py}
LEDGER_TICKETS=${LEDGER_TICKETS:-$(dirname "$LEDGER")/tickets.json}
PYTHON=${PYTHON:-python3}
NATIVE_PY=src/akkudoktoreos/optimization/genetic/native.py

DRY=0 FETCH=1
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --no-fetch) FETCH=0; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unbekannte Option: $1" >&2; exit 64 ;;
  esac
done

die() { echo "ABBRUCH: $*" >&2; exit 1; }

COMMON=$(cd "$TOOLS" && git rev-parse --path-format=absolute --git-common-dir)
REPO_TOP=$(dirname "$COMMON")
[ -f "$STACK" ] || die "$STACK fehlt"

# Gepinnte Basis: '@base <remote>/<zweig> <sha>' in stack.txt
read -r BASE_REF BASE_PIN < <(sed -E 's/(^|[[:space:]])#.*$//' "$STACK" | awk '$1 == "@base" { print $2, $3; exit }') || true
[ -n "${BASE_PIN:-}" ] || die "stack.txt: keine Zeile '@base <ref> <sha>'"

if [ "$FETCH" = 1 ]; then
  git -C "$REPO_TOP" fetch --quiet "$UPSTREAM_REMOTE"
fi
BASE_SHA=$(git -C "$REPO_TOP" rev-parse --verify "$BASE_PIN^{commit}") || die "Basis $BASE_PIN unbekannt"
HEAD_REF="$UPSTREAM_REMOTE/main"
HEAD_SHA=$(git -C "$REPO_TOP" rev-parse --verify "$HEAD_REF^{commit}")
git -C "$REPO_TOP" merge-base --is-ancestor "$BASE_SHA" "$HEAD_SHA" || die "Basis $BASE_PIN liegt nicht in $HEAD_REF"

# Fingerabdruck-relevante Pfade aus native.py: upstream (sobald #1520 dort ist, zählt eine neue
# Quelle in der Liste schon mit), Basis, dvhub-eos/main und die Stack-Zweige.
REVS=("$HEAD_SHA" "$BASE_SHA")
git -C "$REPO_TOP" rev-parse --verify --quiet "$FORK_REMOTE/dvhub-eos/main^{commit}" >/dev/null && REVS+=("$FORK_REMOTE/dvhub-eos/main")
while IFS= read -r ref; do
  git -C "$REPO_TOP" rev-parse --verify --quiet "$ref^{commit}" >/dev/null && REVS+=("$ref")
done < <(sed -E 's/(^|[[:space:]])#.*$//' "$STACK" | awk 'NF >= 2 && $1 != "@base" { print $1 }')
PATHS=()
while IFS= read -r p; do PATHS+=("$p"); done < <(
  for rev in "${REVS[@]}"; do
    git -C "$REPO_TOP" show "$rev:$NATIVE_PY" 2>/dev/null || true
  done | "$PYTHON" -c '
import re, sys
text = sys.stdin.read()
paths = set()
for name in ("FINGERPRINT_SOURCES", "FINGERPRINT_DATA"):
    for block in re.findall(name + r"[^=]*=\s*\(([^)]*)\)", text):
        paths.update("src/akkudoktoreos/" + p for p in re.findall(r"\"([^\"]+)\"", block))
if "FINGERPRINT_DATA" in text and "SELF_CONSUMPTION_TABLE" in text:
    for table in re.findall(r"SELF_CONSUMPTION_TABLE\s*=\s*\"([^\"]+)\"", text):
        paths.add("src/akkudoktoreos/" + table)
paths.discard("src/akkudoktoreos/")
for p in sorted(paths):
    print(p)
'
)
PATHS+=("$NATIVE_PY" "native")
[ ${#PATHS[@]} -gt 2 ] || die "keine FINGERPRINT_SOURCES in $NATIVE_PY gefunden"

COMMITS=$(git -C "$REPO_TOP" log --first-parent --reverse --format='%H%x09%s' "$BASE_SHA..$HEAD_SHA" -- "${PATHS[@]}")
BEHIND=$(git -C "$REPO_TOP" rev-list --count "$BASE_SHA..$HEAD_SHA")
echo "== Basis $BASE_REF ${BASE_SHA:0:9}, $HEAD_REF ${HEAD_SHA:0:9} ($BEHIND Commits weiter)"
if [ -z "$COMMITS" ]; then
  echo "== keine fingerabdruck-relevante upstream-Änderung seit der Basis"
  exit 0
fi

# Eigene Stack-PRs (Herkunft upstream#<nr>): gemergt heißt Zeile löschen + Basis anheben.
OWN=$(sed -E 's/(^|[[:space:]])#.*$//' "$STACK" | awk '$1 != "@base" && $2 ~ /^upstream#/ { sub("upstream#", "", $2); print $2 }')

new=0 known=0
while IFS=$'\t' read -r sha subject; do
  [ -n "$sha" ] || continue
  if [[ "$subject" =~ \(#([0-9]+)\)[[:space:]]*$ ]]; then
    key="#${BASH_REMATCH[1]}"
    title_text=$(printf '%s' "$subject" | sed -E 's/[[:space:]]*\(#[0-9]+\)[[:space:]]*$//')
  else
    key=${sha:0:9}
    title_text=$subject
  fi
  title="Kern nachziehen: upstream $key $title_text"
  if printf '%s\n' "$OWN" | grep -qx "${key#\#}"; then
    title="$title (eigener Stack-PR: Zeile aus stack.txt löschen, Basis anheben)"
  fi
  if "$PYTHON" - "$LEDGER_TICKETS" "Kern nachziehen: upstream $key " <<'EOF'
import json, sys
path, prefix = sys.argv[1], sys.argv[2]
try:
    tickets = json.load(open(path, encoding="utf-8"))
except FileNotFoundError:
    tickets = []
sys.exit(0 if any(str(t.get("title", "")).startswith(prefix) for t in tickets) else 1)
EOF
  then
    known=$((known + 1))
    echo "   bekannt   $key  $title_text"
    continue
  fi
  new=$((new + 1))
  files=$(git -C "$REPO_TOP" diff --name-only "$sha^1" "$sha" -- "${PATHS[@]}" | tr '\n' ' ')
  req="upstream-watch $(date +%F): $HEAD_REF ${sha:0:9} ändert fingerabdruck-relevante Dateien: $files"
  if [ "$DRY" = 1 ]; then
    echo "   NEU (dry) $key  $title_text  [$files]"
  else
    "$PYTHON" "$LEDGER" add --title "$title" --req "$req" --to "DVhub-Sitzung (Claude)" \
      --domain EOS --status open --release 1.0.11 --labels strang,eos-upstream
  fi
done <<< "$COMMITS"
echo "== $new neu, $known schon im Ledger"
