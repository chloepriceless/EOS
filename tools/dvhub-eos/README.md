# DVhub-EOS

DVhub-EOS ist Akkudoktor-EOS `main` plus die Änderungen, die DVhub braucht und die upstream noch
nicht gemergt sind. Es wird je DVhub-Release frisch gebaut:

    dvhub-eos/main = upstream/main
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

1. holt `upstream` und `fork`, liest `stack.txt` (Ref, Herkunft, optional Pin-SHA),
2. setzt `dvhub-eos/main` im Bau-Worktree auf `upstream/main` und berechnet die upstream-Version
   (`scripts/get_version.py` am unveränderten Stand),
3. merged jeden Eintrag mit `--no-ff` – mit `git rerere` und den Auflösungen aus `rr-cache/`;
   `openapi.json` und `docs/_generated/**` werden dabei nie zusammengeführt (`merge.gitattributes`),
4. übernimmt `overlay/`, erzeugt die generierten Dateien mit `make gen-docs` neu,
5. bricht ab, wenn ein Konflikt ungelöst bleibt oder Konfliktmarker im Ergebnis stehen,
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
`EOS_OPTIMIZATION__GENETIC__BACKEND=python` schaltet ihn ohne neues Image ab. Nichts wird gepusht.

## Fork-PR-Ablauf (eigene Änderungen)

1. Feature-Zweig auf **`upstream/main`** (nicht auf `dvhub-eos/main`) – dann bleibt er unverändert
   upstream einreichbar.
2. PR im Fork `chloepriceless/EOS` gegen `dvhub-eos/main`. Die upstream-Workflows (pytest,
   pre-commit, docker-build, native) laufen auf jedem PR, also auch auf diesem.
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
| `build-integration.sh` | baut `dvhub-eos/main`, optional Tag |
| `build-image.sh`, `image/Dockerfile.native` | baut das Docker-Image lokal |
| `stack.txt` | Zweige und Herkunft, Reihenfolge = Merge-Reihenfolge |
| `rr-cache/` | rerere-Auflösungen (`<id>/preimage`, `<id>/postimage`), versioniert |
| `merge.gitattributes` | generierte Dateien beim Merge nicht zusammenführen |
| `overlay/` | Dateien, die nur DVhub-EOS hat (CI-Workflow für `dvhub-eos/main`) |
