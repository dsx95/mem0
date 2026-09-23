"""Map stable stored paths to this checkout without changing memory text or vectors."""

import os
from pathlib import Path

LEGACY_DATA = Path("/Knowin/foundation/seb/mem0-main/.data")
LEGACY_MATERIALS = Path("/Knowin/foundation/seb/material/诺因公开资料")
PATH_KEYS = {
    "source_root",
    "source_path",
    "preview_file",
    "preview_files",
    "rendered_file",
    "audio_file",
    "path",
    "root",
}


def map_path(value, *, store=False):
    project = os.environ.get("MEM0_PORTABLE_ROOT")
    if not project or not isinstance(value, str):
        return value
    pairs = [(LEGACY_DATA, Path(project) / "data"), (LEGACY_MATERIALS, Path(project) / "materials")]
    candidate = Path(value)
    for legacy, current in pairs:
        before, after = (current, legacy) if store else (legacy, current)
        try:
            relative = candidate.relative_to(before)
        except ValueError:
            continue
        # Do not let stored paths escape a mapped directory through ../ or symlinks.
        resolved = (after / relative).resolve()
        if not resolved.is_relative_to(after.resolve()):
            raise ValueError("Stored attachment path escapes its directory")
        return str(after / relative)
    return value


def map_metadata(value, *, store=False, key=None):
    if isinstance(value, dict):
        return {k: map_metadata(v, store=store, key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [map_metadata(v, store=store, key=key) for v in value]
    return map_path(value, store=store) if key in PATH_KEYS else value
