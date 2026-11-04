from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

# Default profile location. Overridden by --profiles-dir or BOOTSMITH_PROFILES_DIR.
_DEFAULT_PROFILE_DIR = Path(os.path.expanduser("~/.bootsmith/profiles"))
_PROFILE_DIR: Path = Path(
    os.environ.get("BOOTSMITH_PROFILES_DIR", str(_DEFAULT_PROFILE_DIR))
).expanduser()
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# System names: what the board is (GMOSCC, MCS, GNIRSDC1). Stored upper case.
_SYSTEM_RE = re.compile(r"^[A-Z0-9_-]{1,32}$")


def set_profile_dir(path: Path | str) -> None:
    """Override the directory profiles are read from / written to.

    Called from the CLI entry point if --profiles-dir is given.
    """
    global _PROFILE_DIR
    _PROFILE_DIR = Path(path).expanduser()


def profile_dir() -> Path:
    return _PROFILE_DIR


@dataclass
class TerminalServer:
    """A WTI serial-console port a Profile can be reached through.

    A Profile is a boot config (loader, params, prompts) that's independent
    of which physical VME it's talking to. The same config often needs to
    reach more than one WTI -- e.g. a production unit and a test-bench unit
    for the same board -- so a Profile holds a list of these instead of a
    single host/port.
    """

    name: str
    host: str
    port: int


@dataclass
class Profile:
    """A saved VME target.

    `loader_hint` is one of "auto", "ppcbug", "vxworks". The transport layer
    treats it as advisory; auto-detect is run at the prompt regardless.
    `prompts` and `banners` let the user tweak per-target match strings without
    editing code (some PPCBug / Tornado versions print slightly different text).
    """

    name: str
    terminal_servers: list[TerminalServer] = field(default_factory=list)
    loader_hint: str = "auto"
    prompts: dict = field(default_factory=dict)
    banners: dict = field(default_factory=dict)
    boot_params: dict = field(default_factory=dict)
    notes: str = ""
    # The board this preset is for (GMOSCC, MCS, ...). Set explicitly so a
    # preset's system never depends on how its name is spelled; "" only in
    # files written before the field existed (see system_of).
    system: str = ""

    def terminal_server(self, name: str) -> TerminalServer | None:
        for ts in self.terminal_servers:
            if ts.name == name:
                return ts
        return None


def normalize_system(system: str) -> str:
    """Upper-case and validate a system name; raises ValueError."""
    s = (system or "").strip().upper()
    if not _SYSTEM_RE.match(s):
        raise ValueError(
            f"invalid system name {system!r}: letters, digits, '-' or '_', max 32"
        )
    return s


def system_of(profile: Profile) -> str:
    """The preset's system: its `system` field, or for a file written
    before that field existed, the system read from its name."""
    return profile.system or system_name(profile.name)


def _profile_path(name: str) -> Path:
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid profile name: {name!r}")
    return _PROFILE_DIR / f"{name}.json"


def _dir() -> Path:
    return _PROFILE_DIR


def ensure_dir() -> None:
    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)


def list_profiles() -> list[Profile]:
    ensure_dir()
    out: list[Profile] = []
    for path in sorted(_PROFILE_DIR.glob("*.json")):
        try:
            out.append(_load_path(path))
        except Exception:
            continue
    return out


def get_profile(name: str) -> Profile | None:
    path = _profile_path(name)
    if not path.exists():
        return None
    return _load_path(path)


def save_profile(profile: Profile) -> None:
    ensure_dir()
    path = _profile_path(profile.name)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(profile), indent=2, sort_keys=True))
    tmp.replace(path)


def delete_profile(name: str) -> bool:
    path = _profile_path(name)
    if path.exists():
        path.unlink()
        return True
    return False


def rename_profile(old_name: str, new_name: str) -> None:
    """Move profile JSON from old_name to new_name. Raises if the source
    doesn't exist or the target already does.
    """
    src = _profile_path(old_name)
    dst = _profile_path(new_name)
    if not src.exists():
        raise FileNotFoundError(f"no profile named {old_name!r}")
    if src == dst:
        return
    if dst.exists():
        raise FileExistsError(f"profile {new_name!r} already exists")
    # Update the name inside the JSON, then move the file.
    profile = _load_path(src)
    profile.name = new_name
    tmp = dst.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(profile), indent=2, sort_keys=True))
    tmp.replace(dst)
    src.unlink()


def unique_name(base: str) -> str:
    """Return a profile name based on `base` that doesn't yet exist on disk.

    Tries `base-copy`, then `base-copy2`, `base-copy3`, ... so duplicating
    the same target repeatedly never collides. The result always satisfies
    `_NAME_RE` because `base` already does and the suffix is name-safe.
    """
    candidate = f"{base}-copy"
    n = 1
    while _profile_path(candidate).exists():
        n += 1
        candidate = f"{base}-copy{n}"
    return candidate


def duplicate_profile(name: str) -> Profile:
    """Copy the profile `name` to a fresh unique name and persist it.

    Returns the newly-saved Profile. Raises FileNotFoundError if the
    source doesn't exist.
    """
    src = get_profile(name)
    if src is None:
        raise FileNotFoundError(f"no profile named {name!r}")
    copy = Profile(
        name=unique_name(name),
        terminal_servers=[
            TerminalServer(ts.name, ts.host, ts.port) for ts in src.terminal_servers
        ],
        loader_hint=src.loader_hint,
        prompts=dict(src.prompts),
        banners=dict(src.banners),
        boot_params=dict(src.boot_params),
        notes=src.notes,
        system=system_of(src),
    )
    save_profile(copy)
    return copy


def _load_path(path: Path) -> Profile:
    data = json.loads(path.read_text())
    # Older profiles used `last_params`; accept either key.
    boot_params = data.get("boot_params") or data.get("last_params") or {}
    if "terminal_servers" in data:
        terminal_servers = [
            TerminalServer(name=ts["name"], host=ts["host"], port=int(ts["port"]))
            for ts in data["terminal_servers"]
        ]
    else:
        # Pre-multi-server profiles stored a single wti_host/wti_port pair
        # directly on the profile. Fold it into a one-entry list so callers
        # only ever deal with the list form.
        terminal_servers = [
            TerminalServer(name="default", host=data["wti_host"], port=int(data["wti_port"]))
        ]
    return Profile(
        name=data["name"],
        terminal_servers=terminal_servers,
        loader_hint=data.get("loader_hint", "auto"),
        prompts=data.get("prompts", {}),
        banners=data.get("banners", {}),
        boot_params=boot_params,
        notes=data.get("notes", ""),
        system=(data.get("system") or "").strip().upper(),
    )


def names(profiles: Iterable[Profile]) -> list[str]:
    return [p.name for p in profiles]


# --- Grouping for the target list: system -> connection -> preset ---------

# Leading name tokens that say where a preset is used rather than which
# board it is for: MKO-PROD-GNIRSCC-new-bootserver -> GNIRSCC.
_SITE_TOKENS = {"MKO", "PROD"}


def system_name(profile_name: str) -> str:
    """The system (board) a preset belongs to, derived from its name.

    Site/environment prefixes are dropped and the next token is the
    system; anything after it names the variant (new-bootserver, m2lab,
    sim8, ...). A name with nothing left after the prefixes is its own
    system.
    """
    tokens = [t for t in re.split(r"[-_]", profile_name) if t]
    rest = list(tokens)
    while rest and rest[0].upper() in _SITE_TOKENS:
        rest.pop(0)
    return (rest[0] if rest else profile_name).upper()


@dataclass
class ConnectionGroup:
    """One terminal-server port of a system and the presets that use it.

    `presets` pairs each preset with the name *it* gives this port, which
    is what /session/open needs (presets don't always agree on names).
    """

    name: str
    host: str
    port: int
    presets: list[tuple[Profile, str]] = field(default_factory=list)


@dataclass
class SystemGroup:
    name: str
    connections: list[ConnectionGroup] = field(default_factory=list)
    presets: list[Profile] = field(default_factory=list)


def _preset_order(system: str, profile: Profile) -> tuple:
    # The plain production preset (MKO-PROD-<system>) first, then by name.
    rest = [t for t in re.split(r"[-_]", profile.name) if t.upper() not in _SITE_TOKENS]
    return (len(rest) > 1, profile.name.lower())


def group_by_system(profiles: Iterable[Profile]) -> list[SystemGroup]:
    systems: dict[str, SystemGroup] = {}
    for p in profiles:
        s = system_of(p)
        systems.setdefault(s, SystemGroup(s)).presets.append(p)
    out = []
    for name in sorted(systems):
        sg = systems[name]
        sg.presets.sort(key=lambda p: _preset_order(name, p))
        conns: dict[tuple[str, int], ConnectionGroup] = {}
        labels: dict[tuple[str, int], list[str]] = {}
        for p in sg.presets:
            for ts in p.terminal_servers:
                key = (ts.host, ts.port)
                conns.setdefault(key, ConnectionGroup(ts.name, ts.host, ts.port)).presets.append((p, ts.name))
                labels.setdefault(key, []).append(ts.name)
        for key, cg in conns.items():
            # Label a port by the name most of its presets give it; on a
            # tie prefer "production", then the first preset's name.
            names = labels[key]
            cg.name = max(dict.fromkeys(names), key=lambda n: (names.count(n), n == "production"))
        sg.connections = sorted(
            conns.values(), key=lambda c: (c.name != "production", c.name.lower(), c.host, c.port)
        )
        out.append(sg)
    return out


def system_presets(system: str) -> list[Profile]:
    return [p for p in list_profiles() if system_of(p) == system]


def system_exists(system: str) -> bool:
    return any(system_of(p) == system for p in list_profiles())


# --- Connection edits for one system --------------------------------------
#
# A connection (WTI console port) is not stored on its own: each preset
# lists the consoles it may be used on in terminal_servers. Editing a
# connection therefore rewrites the matching entry in every preset of the
# system that has it. Each change is checked for every preset before any
# file is written, so a refused change leaves all files as they were.


class ConnectionError_(ValueError):
    """A connection change that would leave the presets inconsistent."""


def _save_all(presets: list[Profile]) -> None:
    for p in presets:
        if not p.system:
            p.system = system_of(p)
        save_profile(p)


def set_connection(
    system: str,
    old: tuple[str, int] | None,
    new: TerminalServer,
    used_by: Iterable[str],
) -> None:
    """Create (old=None) or edit the connection `old` of `system`.

    `used_by` names the presets that should list the connection; every
    other preset of the system drops it. Refused (nothing written) if no
    preset uses it, if a preset would be left with no connection, or if
    the new host:port is already another connection of the system.
    """
    used_by = set(used_by)
    presets = system_presets(system)
    names_ = {p.name for p in presets}
    unknown = used_by - names_
    if unknown:
        raise ConnectionError_(f"not presets of {system}: {', '.join(sorted(unknown))}")
    if not used_by:
        raise ConnectionError_("tick at least one preset that uses this connection")
    new_key = (new.host, int(new.port))
    if new_key != old and any(
        (t.host, t.port) == new_key for p in presets for t in p.terminal_servers
    ):
        raise ConnectionError_(f"{new.host}:{new.port} is already a connection of {system}")
    left_empty = []
    for p in presets:
        kept = [t for t in p.terminal_servers if old is None or (t.host, t.port) != old]
        if p.name in used_by:
            # Keep its place in the list when editing.
            idx = next(
                (i for i, t in enumerate(p.terminal_servers) if old and (t.host, t.port) == old),
                len(kept),
            )
            kept.insert(min(idx, len(kept)), TerminalServer(new.name, new.host, int(new.port)))
        if not kept:
            left_empty.append(p.name)
        p.terminal_servers = kept
    if left_empty:
        raise ConnectionError_(
            "these presets would have no connection left: " + ", ".join(left_empty)
        )
    _save_all(presets)


def delete_connection(system: str, key: tuple[str, int]) -> None:
    """Remove the connection from every preset of `system`. Refused if a
    preset would be left with no connection."""
    presets = system_presets(system)
    left_empty = [
        p.name for p in presets
        if p.terminal_servers and all((t.host, t.port) == key for t in p.terminal_servers)
    ]
    if left_empty:
        raise ConnectionError_(
            "these presets use only this connection; give them another one "
            "or delete them first: " + ", ".join(left_empty)
        )
    for p in presets:
        p.terminal_servers = [t for t in p.terminal_servers if (t.host, t.port) != key]
    _save_all(presets)
