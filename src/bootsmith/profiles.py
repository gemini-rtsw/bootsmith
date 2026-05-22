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


def set_profile_dir(path: Path | str) -> None:
    """Override the directory profiles are read from / written to.

    Called from the CLI entry point if --profiles-dir is given.
    """
    global _PROFILE_DIR
    _PROFILE_DIR = Path(path).expanduser()


def profile_dir() -> Path:
    return _PROFILE_DIR


@dataclass
class Profile:
    """A saved VME target.

    `loader_hint` is one of "auto", "ppcbug", "vxworks". The transport layer
    treats it as advisory; auto-detect is run at the prompt regardless.
    `prompts` and `banners` let the user tweak per-target match strings without
    editing code (some PPCBug / Tornado versions print slightly different text).
    """

    name: str
    wti_host: str
    wti_port: int
    loader_hint: str = "auto"
    prompts: dict = field(default_factory=dict)
    banners: dict = field(default_factory=dict)
    boot_params: dict = field(default_factory=dict)
    # Sorted list of diag-command keys (from ppcbug.DIAG_COMMANDS) to run
    # when the user clicks the Diag button. Stored as a list for JSON
    # friendliness; semantically a set.
    diag_commands: list = field(default_factory=list)
    notes: str = ""


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


def _load_path(path: Path) -> Profile:
    data = json.loads(path.read_text())
    # Older profiles used `last_params`; accept either key.
    boot_params = data.get("boot_params") or data.get("last_params") or {}
    return Profile(
        name=data["name"],
        wti_host=data["wti_host"],
        wti_port=int(data["wti_port"]),
        loader_hint=data.get("loader_hint", "auto"),
        prompts=data.get("prompts", {}),
        banners=data.get("banners", {}),
        boot_params=boot_params,
        diag_commands=list(data.get("diag_commands", [])),
        notes=data.get("notes", ""),
    )


def names(profiles: Iterable[Profile]) -> list[str]:
    return [p.name for p in profiles]
