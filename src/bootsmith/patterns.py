from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class LoaderPatterns:
    """Regexes and abort sequence for one loader.

    `banner` is matched against a sliding window of recent bytes to detect that
    the board just rebooted into this loader's countdown phase.
    `prompt` is matched to know we've landed at the interactive prompt.
    `abort` is what the watcher spams to stop the auto-boot countdown.
    """

    name: str
    banner: re.Pattern[bytes]
    prompt: re.Pattern[bytes]
    abort: bytes


# Defaults are deliberately loose — they match the most common variants of each
# loader. Per-target overrides in Profile.prompts / Profile.banners let users
# pin tighter or different strings when a board prints something unusual.

PPCBUG = LoaderPatterns(
    name="ppcbug",
    # PPCBug banners across versions include "PPC[0-9]-Bug" or "PPCBug" plus
    # the Motorola copyright line. Either is a good early signal.
    banner=re.compile(
        rb"(Copyright Motorola|PPC[0-9]?[- ]?Bug|PPC[0-9]-Bug)", re.IGNORECASE
    ),
    # Prompt is typically "PPC1-Bug>" or "PPC1>" — match either.
    prompt=re.compile(rb"PPC[0-9](?:-Bug)?>\s*$"),
    # PPCBug stops the auto-boot when it sees Esc or a break during countdown;
    # space is also commonly accepted. Send Esc which is the most universal.
    abort=b"\x1b",
)

VXWORKS = LoaderPatterns(
    name="vxworks",
    # VxWorks boot ROM prints "VxWorks System Boot" and/or "Press any key to
    # stop auto-boot". Either is a strong signal.
    banner=re.compile(
        rb"(VxWorks(?:\s+System)?\s+Boot|Press any key to stop auto-boot)",
        re.IGNORECASE,
    ),
    # Prompt is "[VxWorks Boot]:" — sometimes with trailing space.
    prompt=re.compile(rb"\[VxWorks Boot\]:\s*$"),
    # Any character stops VxWorks auto-boot. Use space — won't trigger any
    # accidental command if we send a few too many.
    abort=b" ",
)

ALL: tuple[LoaderPatterns, ...] = (PPCBUG, VXWORKS)


def with_overrides(base: LoaderPatterns, overrides: dict) -> LoaderPatterns:
    """Return a copy of `base` with regex/abort overridden from a profile dict.

    Keys honored: "banner", "prompt", "abort". Missing keys keep the default.
    `abort` may be given as a string; standard backslash escapes are decoded
    (\\r, \\n, \\x1b, \\t) so a user can put "\\x1b" in JSON.
    """
    banner = base.banner
    prompt = base.prompt
    abort = base.abort
    if "banner" in overrides and overrides["banner"]:
        banner = re.compile(overrides["banner"].encode("utf-8"), re.IGNORECASE)
    if "prompt" in overrides and overrides["prompt"]:
        prompt = re.compile(overrides["prompt"].encode("utf-8"))
    if "abort" in overrides and overrides["abort"]:
        abort = overrides["abort"].encode("utf-8").decode("unicode_escape").encode("latin-1")
    return LoaderPatterns(name=base.name, banner=banner, prompt=prompt, abort=abort)
