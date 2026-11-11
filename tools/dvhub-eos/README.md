# DVhub-EOS

DVhub-EOS ist Akkudoktor-EOS `main` plus die Änderungen, die DVhub braucht und die upstream noch
nicht gemergt sind. Es wird je DVhub-Release frisch gebaut:

    dvhub-eos/main = gepinnte Basis (Zeile '@base' in stack.txt, ein upstream/main-Stand)
                   + Merge je Zeile in stack.txt (Reihenfolge der Datei)
                   + overlay/        (nur DVhub-EOS: CI für dvhub-eos/main)
                   + make gen-docs   (openapi.json, docs/_generated/* neu erzeugt)

Zweige im Fork `chloepriceless/EOS`:

| Zweig | Inhalt | Historie |
|---|---|---|
| `dvhub-eos/tooling` | nur `tools/dvhub-eos/` (dieses Verzeichnis), eigener Wurzel-Commit | normal, nie umgeschrieben |
| `dvhub-eos/main` | das Ergebnis von `build-integration.sh` | wird bei jedem Bau neu erzeugt (force-push) |
| Feature-Zweige | je ein PR, Basis `upstream/main` | normal |

Warum ein eigener Tooling-Zweig: Die PR-Zweige müssen unverändert upstream einreichbar bleiben,
`dvhub-eos/main` wird bei jedem Bau neu erzeugt und kann daher nichts Dauerhaftes halten. Stack-Liste
und Konfliktauflösungen brauchen eine stabile Historie (wer hat wann was aufgenommen) – die hat nur
ein eigener Zweig.

## Bauen

    git worktree add ~/Projects/eos-dvhub-tooling dvhub-eos/tooling     # einmalig
    cd ~/Projects/eos-dvhub-tooling
    tools/dvhub-eos/build-integration.sh --worktree <bau-worktree> [--tag]

Das Skript

1. holt `upstream` und `fork`, liest `stack.txt` (Basis-Pin, je Zeile Ref, Herkunft, optional Pin-SHA),
2. setzt `dvhub-eos/main` im Bau-Worktree auf die gepinnte Basis und berechnet die upstream-Version
   (`scripts/get_version.py` am unveränderten Stand),
3. merged jeden Eintrag mit `--no-ff` – mit `git rerere` und den Auflösungen aus `rr-cache/`;
   `openapi.json` und `docs/_generated/**` werden dabei nie zusammengeführt (`merge.gitattributes`),
4. übernimmt `overlay/`, erzeugt die generierten Dateien mit `make gen-docs` neu,
5. bricht ab, wenn ein Konflikt ungelöst bleibt oder Konfliktmarker im Ergebnis stehen,
   **oder wenn der Quell-Fingerabdruck der GENETIC-Quellen des Ergebnisses nicht in
   `native/akkudoktoreos-native/python/akkudoktoreos_native/certified.json` steht** (für die
   Kern-Version des Stands) – ein solches DVhub-EOS rechnete in Python. `--allow-python` lässt
   den Bau trotzdem durch, nur für Entwicklungs- und Messbauten und nie zusammen mit `--tag`,
6. setzt mit `--tag` das annotierte Tag `dvhub-eos-<upstream-version>-dvhub.<N>`; die Tag-Nachricht
   nennt alle Eingänge mit SHA (Basis, Stack, Tooling-Commit).

Der Bau ist reproduzierbar: Alle Commits tragen den Zeitpunkt des jüngsten Eingangs-Commits; gleiche
Eingänge und gleiche Git-Identität ergeben denselben `dvhub-eos/main`-Commit.

rerere und der Merge-Treiber gelten nur für die Aufrufe des Skripts (`git -c ...`); an der
gemeinsamen Repo-Konfiguration ändert sich nichts.

### Konflikt, den rerere nicht kennt

Das Skript hält an und lässt den Merge im Bau-Worktree stehen. Dann:

    # Dateien im Bau-Worktree auflösen
    tools/dvhub-eos/build-integration.sh --record --worktree <bau-worktree>   # rerere + nach rr-cache/
    git -C <bau-worktree> add -A && git -C <bau-worktree> commit --no-edit
    git add tools/dvhub-eos/rr-cache && git commit -m "dvhub-eos: Auflösung <A> x <B>"
    tools/dvhub-eos/build-integration.sh --worktree <bau-worktree>           # neu, rerere spielt sie ein

Konflikte in generierten Dateien gibt es nicht; sie entstehen am Ende neu.

## Versionen und Tags

EOS selbst versioniert nach PEP 440 (`src/akkudoktoreos/core/version.py`): `x.y.z`, `x.y.zrcN`, und
auf `main` `x.y.z.dev<JJMMTTHH><hash>` (Datum des jüngsten Commits + Quelltext-Hash). Daraus:

| | Beispiel |
|---|---|
| upstream-Version (`main`, unverändert) | `0.3.0.dev2611110839161148` |
| Git-Tag | `dvhub-eos-0.3.0.dev2611110839161148-dvhub.1` |
| Docker-Tag (`bikinibottomcapital/dvhub-eos`) | `0.3.0.dev2611110839161148-dvhub.1` (Git-Tag ohne Präfix) |
| PEP-440-Schreibweise, falls je gebraucht | `0.3.0.dev2611110839161148+dvhub.1` |

`-dvhub.<N>` statt `+dvhub.<N>`: Docker-Tags dürfen kein `+` enthalten; so sind Git-Tag und Docker-Tag
bis auf das Präfix gleich. pip sieht den Tag-Namen nie als Version: Das Paket nimmt seine Version
weiter aus `version.py`; `EOS_BUILD_VERSION` übernimmt EOS nur in der Form `x.y.z(.devN)`, sonst
berechnet es sie selbst (`/v1/health` meldet daher die berechnete `0.3.0.dev…`, das Image-Label
`org.opencontainers.image.version` den Docker-Tag). `<N>` zählt je upstream-Version hoch; ein Bau mit
unverändertem Baum bekommt kein neues Tag.

## Image

    tools/dvhub-eos/build-image.sh --worktree <checkout von dvhub-eos/main mit Tag> [--platform linux/arm64]

Baut zuerst das unveränderte Root-`Dockerfile` des Stands (wie upstream), dann `image/Dockerfile.native`:
das Wheel `akkudoktoreos-native` aus `native/` des Stands (Rust, maturin) und
`EOS_OPTIMIZATION__GENETIC__BACKEND=native`. EOS nimmt den Rust-Kern nur, wenn der Fingerabdruck der
GENETIC-Quellen in seinem `certified.json` steht, sonst rechnet es mit Python (eine Warnung im Log).
Darum folgt ein Rauchtest im fertigen Image (`image/native-smoke.py`): Kern zertifiziert, und ein
kurzer Lauf mit Netzbezugsgrenze und E-Auto (Ladestart, Frist im Slot, Wertstufen, Lauf mitten im
Slot) loggt `GENETIC native run` ohne Rückfall. Scheitert er, entfernt das Skript das Image und
bricht ab; `--allow-python` behält es (nur Entwicklung/Messung, nie auf einem Release-Tag).
`EOS_OPTIMIZATION__GENETIC__BACKEND=python` schaltet den Kern ohne neues Image ab. Nichts wird gepusht.

## Release-Assets: Wheels für DVhub

DVhub installiert den Kern nicht aus einem Eigenbau, sondern als gepinntes Wheel: nativ
(`eos-provision.sh`, passend zur Plattform des EOS-venv) und in seinen Docker-Images
(`docker/eos-native` im DVhub-Repo). Die Wheels liegen als Release-Assets am Tag
`dvhub-eos-<version>-dvhub.<N>`, dazu `SHA256SUMS`; DVhub pinnt Dateiname und sha256 je Architektur
in `eos-version.env` und nimmt keine anderen Bytes.

`overlay/.github/workflows/dvhub-eos-release.yml` erzeugt sie: Jeder gepushte Tag `dvhub-eos-*`
(die Datei liegt ab dem nächsten Bau im getaggten Baum) baut die sechs Wheels wie `native.yml`
(x86_64, aarch64, armv7; manylinux 2014 und musllinux 1.2, `--locked`), prüft x86_64 und aarch64
im getaggten EOS („zertifiziert“ und ein nativer Szenario-Lauf mit `--strict`) und legt das Release
mit allen Wheels und `SHA256SUMS` an. Ein bestehendes Release wird nie überschrieben. Danach im
DVhub-Repo:

    gh release download <tag> -R chloepriceless/EOS -p SHA256SUMS -D /tmp/<tag>
    scripts/eos-native-wheels.sh update-pins /tmp/<tag>/SHA256SUMS     # eos-version.env
    # EOS_PIN, EOS_NATIVE_URL, docker/.env.example (EOS_TAG, EOS_IMAGE_TAG) nachziehen

Tags, deren Baum den Workflow noch nicht enthält (bis einschließlich
`dvhub-eos-0.3.0.dev2611110839161148-dvhub.2`), bekommen ihre Wheels von Hand: dieselben Ziele mit
maturin und zig gebaut (`maturin build --release --locked --zig --target <ziel> --compatibility
manylinux2014|musllinux_1_2`) und mit `gh release create <tag> *.whl SHA256SUMS --latest=false`
hochgeladen — genau die Dateien, deren sha256 DVhub gepinnt hat.

## Nativer Kern: immer ausgeliefert, immer benutzt

DVhub liefert DVhub-EOS immer mit dem Rust-Kern aus und rechnet damit; ein Python-Rückfall in
einem ausgelieferten DVhub-EOS ist ein Fehler. Daraus folgen drei Regeln:

1. **Jeder Fork-PR, der GENETIC ändert, enthält Python und Rust.** GENETIC heißt: eine Datei aus
   `FINGERPRINT_SOURCES`/`FINGERPRINT_DATA` in `src/akkudoktoreos/optimization/genetic/native.py`.
   Der PR portiert die Änderung nach `native/` und trägt den neuen Fingerabdruck in
   `certified.json` ein (Verfahren: `native/README.md`, Nachweis per Compare-Lauf über die Suite und
   `native/scripts/parity_scenarios.py`). Die Fork-CI (`overlay/.github/workflows/dvhub-eos-native.yml`)
   prüft auf jedem PR gegen `dvhub-eos/main` „nativ zertifiziert ja/nein“ und vergleicht die
   Szenarien-Sammlung; „nein“ oder eine Abweichung ist ein roter Check.
2. **Die Basis ist gepinnt** (`@base` in `stack.txt`) und wird nur bewusst angehoben: zusammen mit
   dem Kern-Port aller GENETIC-Änderungen zwischen alter und neuer Basis.
3. **upstream-Änderungen anderer** an diesen Dateien meldet `upstream-watch.sh` als Ledger-Tickets
   („Kern nachziehen: upstream <PR/commit> <Titel>“). Sie werden portiert, bevor die Basis
   angehoben wird. Ein gemergter eigener Stack-PR erscheint dort mit dem Hinweis, seine Zeile zu
   löschen und die Basis anzuheben.

### Basis anheben

    tools/dvhub-eos/upstream-watch.sh --dry-run          # was seit der Basis GENETIC ändert
    # je Ticket: Kern-Port im Fork-PR (Python ist Referenz), certified.json neu
    # stack.txt: @base auf den neuen upstream/main-SHA, gemergte upstream#-Zeilen löschen
    tools/dvhub-eos/build-integration.sh --worktree <bau-worktree>   # bricht ohne Zertifikat ab

### Upstream-Wächter

`upstream-watch.sh` holt `upstream/main`, sucht seit der gepinnten Basis die Commits, die
fingerabdruck-relevante Dateien ändern (die Liste aus `native.py` von upstream, Basis,
`dvhub-eos/main` und den Stack-Zweigen, dazu `native.py` und `native/`), und legt je upstream-PR
(Squash-Merge „(#<nr>)“, sonst je Commit) ein Ledger-Ticket an (`--domain EOS --release 1.0.11
--labels strang,eos-upstream`). Idempotent: steht ein Ticket mit dem Titelanfang
„Kern nachziehen: upstream <PR/commit> “ schon im Ledger, entsteht kein zweites. Der Ledger-Sync
spiegelt die Tickets als Issues. `--dry-run` zeigt nur an.

Täglicher Lauf per **launchd auf dem Mac** (`launchd/net.dvhub.eos-upstream-watch.plist`, 06:30,
verpasste Läufe nach dem Aufwachen), nicht als geplanter Workflow im Fork: Das Ledger
(`ledger.py`, `tickets.json`) liegt nur auf dem Mac, und nur von dort spiegelt der Sync die
Tickets als Issues. Ein Workflow im Fork müsste Issues direkt anlegen (Token als Secret, am Ledger
vorbei) und bräuchte einen zweiten Weg zurück ins Ledger; der Abruf von upstream ist dagegen
öffentliches GitHub und braucht kein Heimnetz. Installation (einmalig):

    cp tools/dvhub-eos/launchd/net.dvhub.eos-upstream-watch.plist ~/Library/LaunchAgents/
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/net.dvhub.eos-upstream-watch.plist

## Fork-PR-Ablauf (eigene Änderungen)

1. Feature-Zweig auf **`upstream/main`** (nicht auf `dvhub-eos/main`) – dann bleibt er unverändert
   upstream einreichbar. Ausnahme: ein Zweig, der offene Stack-PRs voraussetzt (z. B. der
   Kern-Port `feat/t0420-native-parity-ev-grid` für #1531/#1532), setzt auf `dvhub-eos/main` auf
   und geht upstream erst als Folge-PR, wenn seine Voraussetzungen dort gemergt sind.
2. PR im Fork `chloepriceless/EOS` gegen `dvhub-eos/main`. Die upstream-Workflows (pytest,
   pre-commit, docker-build, native) und `dvhub-eos-native.yml` laufen auf jedem PR, also auch auf
   diesem. Ändert der PR GENETIC, enthält er Python **und** den Kern-Port samt `certified.json`
   (siehe „Nativer Kern“).
3. Nach dem Merge des Fork-PR: Zeile `fork/<zweig>  fork#<nr>` in `stack.txt`, commit auf
   `dvhub-eos/tooling`, neu bauen. (Der Merge-Commit des Fork-PR geht beim nächsten Neubau verloren;
   maßgeblich ist `stack.txt`.)

## Stufen eines Eintrags und wann er herausfällt

| Stufe | Eintrag in stack.txt |
|---|---|
| nur im DVhub-EOS (Fork-PR gemergt) | `fork/<zweig>  fork#<nr>` |
| upstream eingereicht | `fork/<zweig>  upstream#<nr>` (Herkunft umstellen; Zweig bleibt derselbe) |
| upstream gemergt | **Zeile löschen** – die Änderung kommt jetzt über `upstream/main` |

Herausnehmen erst, wenn der PR upstream wirklich gemergt ist **und** `upstream/main` geholt wurde.
Wird ein upstream-PR geschlossen statt gemergt, bleibt die Zeile (Herkunft zurück auf `fork#<nr>`),
bis entschieden ist, ob DVhub die Änderung weiter braucht. Ein Eintrag, der nach einem Upstream-Merge
noch in der Liste steht, merged meist leer oder mit Konflikt – beides ist das Zeichen zum Löschen.

## Dateien

| Datei | Zweck |
|---|---|
| `build-integration.sh` | baut `dvhub-eos/main`, prüft das Kern-Zertifikat, optional Tag |
| `build-image.sh`, `image/Dockerfile.native` | baut das Docker-Image lokal |
| `image/native-smoke.py` | Rauchtest im Image: Kern zertifiziert, Lauf nativ ohne Rückfall |
| `upstream-watch.sh`, `launchd/` | meldet fingerabdruck-relevante upstream-Änderungen als Ledger-Tickets |
| `stack.txt` | Basis-Pin, Zweige und Herkunft, Reihenfolge = Merge-Reihenfolge |
| `rr-cache/` | rerere-Auflösungen (`<id>/preimage`, `<id>/postimage`), versioniert |
| `merge.gitattributes` | generierte Dateien beim Merge nicht zusammenführen |
| `overlay/` | Dateien, die nur DVhub-EOS hat (CI-Workflows: Suite für `dvhub-eos/main`, Kern-Prüfung für PRs, Release-Wheels je Tag) |
