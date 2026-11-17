"""PROTOTYP: verteilt die Testdateien dauerbalanciert auf K Shards.

Aufruf: python .github/scripts/shard_files.py K G   -> Dateien von Shard G (0..K-1)

Greedy "longest processing time first": die schwersten Dateien zuerst auf den
jeweils leichtesten Shard. Die Gewichte stehen in .ci-test-durations.json
(Sekunden je Datei, ohne die Server-Tests). Dateien ohne Eintrag (neu) zaehlen
1 s. Die Aufteilung ist deterministisch und deckt jede Datei genau einmal ab.
"""

import glob
import json
import sys


def shards(k: int) -> list[list[str]]:
    try:
        with open(".ci-test-durations.json", encoding="utf-8") as fd:
            dur = json.load(fd)
    except FileNotFoundError:
        # Ohne Gewichte zaehlt jede Datei 1 s: schlechtere Balance, gleiche Abdeckung.
        dur = {}
    files = sorted(glob.glob("tests/test_*.py"))
    load = [0.0] * k
    groups: list[list[str]] = [[] for _ in range(k)]
    for name in sorted(files, key=lambda f: (-dur.get(f, 1.0), f)):
        i = min(range(k), key=lambda j: (load[j], j))
        groups[i].append(name)
        load[i] += dur.get(name, 1.0)
    return groups


if __name__ == "__main__":
    k, g = int(sys.argv[1]), int(sys.argv[2])
    print(" ".join(shards(k)[g]))
