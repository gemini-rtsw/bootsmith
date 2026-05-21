from __future__ import annotations

import argparse
import os
from pathlib import Path

from . import profiles as profiles_mod
from .app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="bootsmith")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--profiles-dir",
        default=os.environ.get("BOOTSMITH_PROFILES_DIR"),
        help=(
            "Directory where profile JSON files live. "
            "Defaults to ./profiles if it exists in the current working "
            "directory, otherwise ~/.bootsmith/profiles. "
            "Can also be set via BOOTSMITH_PROFILES_DIR."
        ),
    )
    args = parser.parse_args()

    # Resolve profile dir: explicit flag > env var (already in flag default)
    # > repo-local ./profiles/ if present > legacy ~/.bootsmith/profiles.
    if args.profiles_dir:
        chosen = Path(args.profiles_dir).expanduser()
    elif Path("profiles").is_dir():
        chosen = Path("profiles").resolve()
    else:
        chosen = Path("~/.bootsmith/profiles").expanduser()
    profiles_mod.set_profile_dir(chosen)
    print(f"[bootsmith] profiles directory: {chosen}", flush=True)

    app = create_app()
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
