"""Optional native (Rust) kernels for Akkudoktor-EOS.

EOS uses this package only when ``optimization.genetic.backend`` asks for it and falls back to
the Python optimizer whenever it is missing, unsupported or fails. Nothing here imports EOS.

The package knows which EOS GENETIC sources it reproduces: ``certified.json`` maps a
fingerprint of those sources (computed by EOS, see
``akkudoktoreos.optimization.genetic.native.source_fingerprint``) to the behaviour switches the
kernel needs for them. EOS sources without an entry run in Python.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any, Optional

from akkudoktoreos_native._core import (
    DOT_KERNELS,
    INTERFACE_VERSION,
    KERNEL_VERSION,
    SELF_CONSUMPTION_TABLE_SHA256,
    SEMANTICS,
    NativeRunError,
    dot,
    expected_direct_consumption,
    interp,
    run_genetic,
)

__version__ = KERNEL_VERSION

__all__ = [
    "DOT_KERNELS",
    "INTERFACE_VERSION",
    "KERNEL_VERSION",
    "SELF_CONSUMPTION_TABLE_SHA256",
    "SEMANTICS",
    "NativeRunError",
    "__version__",
    "certified_entries",
    "certified_semantics",
    "dot",
    "expected_direct_consumption",
    "interp",
    "run_genetic",
]


@lru_cache(maxsize=1)
def certified_entries() -> tuple[dict[str, Any], ...]:
    """Certified EOS source fingerprints shipped with this package."""
    text = resources.files(__package__).joinpath("certified.json").read_text(encoding="utf-8")
    data = json.loads(text)
    if data.get("format") != 1:
        return ()
    return tuple(data.get("entries", ()))


def certified_semantics(fingerprint: str) -> Optional[dict[str, bool]]:
    """Behaviour switches for EOS sources with this fingerprint, or None if not certified."""
    for entry in certified_entries():
        if entry.get("fingerprint") == fingerprint:
            semantics = dict(entry.get("semantics", {}))
            if set(semantics) - set(SEMANTICS):
                # Written for a newer kernel: do not guess.
                return None
            return {name: bool(semantics.get(name, False)) for name in SEMANTICS}
    return None
