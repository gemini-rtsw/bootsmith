# Shared target profiles

Each `*.json` file in this directory is one Bootsmith target. Commit them so
the team has a shared library of known boards.

Bootsmith reads/writes this directory by default when it's launched from the
repo root (or when `--profiles-dir ./profiles` is passed explicitly).

## Migrating existing profiles

If you previously had profiles under `~/.bootsmith/profiles/`, copy them in:

```sh
cp ~/.bootsmith/profiles/*.json profiles/
git add profiles/*.json
git commit -m "REL-4933: Import target profiles"
```

## File format

```json
{
  "name": "MCS",
  "wti_host": "10.1.5.85",
  "wti_port": 2308,
  "loader_hint": "vxworks",
  "boot_params": {
    "boot_device": "geisc",
    "unit_number": "0",
    "host_name": "host",
    "file_name": "/tornado/mv2604/vxWorks",
    "...": "..."
  },
  "prompts": {},
  "banners": {},
  "notes": ""
}
```

The filename must match `name + ".json"` and `name` must be
`[A-Za-z0-9._-]{1,64}`.
