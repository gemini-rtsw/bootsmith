"""Mapping from loader name -> field schema for the profile/edit UI.

Each schema is an ordered tuple of (label, key) pairs. The UI iterates over
this list to render form fields; the routes use the keys to read posted
values back out.
"""

from __future__ import annotations

from . import ppcbug, vxworks


# Loader name (as stored in profile.loader_hint) -> ordered field list.
SCHEMAS: dict[str, tuple[tuple[str, str], ...]] = {
    "vxworks": vxworks.FIELDS_WITH_UNIT,
    "ppcbug": ppcbug.FIELDS,
}

# Human-facing label for the loader in the UI. Internal code keeps using the
# `ppcbug` token because that's what the watcher / prompt patterns match.
LOADER_LABELS: dict[str, str] = {
    "vxworks": "VxWorks",
    "ppcbug": "RTEMS / PPC",
}


def fields_for(loader: str) -> tuple[tuple[str, str], ...]:
    return SCHEMAS.get(loader, ())


def keys_for(loader: str) -> set[str]:
    return {key for _label, key in fields_for(loader)}
