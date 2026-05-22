# modpackctl

A lightweight CLI for managing a CurseForge modpack across versions, with git-backed publishing to GitHub Releases and a self-updating GUI for players.

## Overview

`modpackctl.py` is the pack maintainer's tool. It tracks mod versions by importing CurseForge export zips, generates changelogs, builds client and server release zips, and publishes them to GitHub with a single command.

`client-updater.py` ships alongside the release zip. Players double-click it to check for updates and apply them without re-downloading the entire modpack.

## Prerequisites

- Python 3.11+
- `pip install requests`
- [GitHub CLI](https://cli.github.com) (`gh`) — required only for `publish`

## Setup

1. Copy `modpackctl.toml.example` to `modpackctl.toml` (or one will be created automatically) and fill in your GitHub details:

```toml
[github]
user = "YourGitHubUsername"
repo = "YourRepoName"

[settings]
modpack_prefix = "YourModpackName"

# CurseForge project IDs to exclude from client releases
server_only = [123456, 789012]

# CurseForge project IDs to exclude from server releases
client_only = [345678]
```

2. Export your modpack from CurseForge (the `.zip` file).

3. Initialize the repository:

```
python modpackctl.py init MyModpack-1.0.0.zip
```

## Typical Workflow

```
# Export updated modpack from CurseForge, then:
python modpackctl.py commit MyModpack-updated.zip

# Review what changed and view version numbers
python modpackctl.py log

# View more detailed chanelog between any two versions
python modpackctl.py changelog 1.0.0 1.1.0

# Publish client release to GitHub (builds zip + creates release + updates versions.json)
python modpackctl.py publish 1.2.0

# Optionally include a message in the release notes
python modpackctl.py publish 1.2.0 --message "Improved performance and fixed crashes."
```

## Commands

| Command | Description |
|---|---|
| `init <zip> [--force]` | Initialize from a CurseForge export zip. `--force` resets history but keeps the download cache. |
| `commit <zip> [--major]` | Record a new version from an updated export. Version is bumped automatically. `--major` forces a major bump. |
| `log` | List all committed versions with diff stats. |
| `changelog <v1> [output.md]` | Generate a changelog for `v1` as an initial release. |
| `changelog <v1> <v2> [output.md]` | Generate a changelog between two versions. |
| `release <version> [--client\|--server]` | Build a release zip. `--client` also produces a baked `releases/client-updater.py`. Without a flag, includes all mods. |
| `publish <version> [--message "..."]` | Build a client release, create a GitHub Release, and push `versions.json` to `gh-pages`. |
| `update <version> [--client\|--server]` | Rebuild the `build/` folder for a version without zipping. |
| `purge [--all]` | Remove stale files from the download cache. Without `--all`, only removes mods not in the latest snapshot. |
| `bake-updater` | Write a pre-configured `releases/client-updater.py` using credentials from `modpackctl.toml`, without building a full release. |
| `export-example` | Write the built-in config template to `modpackctl.toml.example`. |

## Version Bumping

Versions follow `major.minor.patch`. The next version is calculated automatically when you `commit`:

- Mods added or removed → minor bump, patch resets
- Mods updated only → patch bump
- Modloader version changes → major bump (automatic)
- `--major` flag → always bumps major

## Player Updater

`client-updater.py` is a standalone Tkinter GUI. Players can save it anywhere (Desktop, Downloads, etc.) and double-click it to update their modpack — the script asks for the modpack folder rather than needing to live inside it.

The flow:

1. **Folder picker** — autodetects a likely `.minecraft` folder and remembers the last choice between runs.
2. **Checking** — fetches `versions.json` and the relevant snapshots from GitHub Pages.
3. **Changelog** — shows the player exactly what will be added, removed, and updated, by mod name.
4. **Confirm & Update** — explicit click before any files are touched.
5. **Atomic install** — all new files download to a temp folder first; if anything fails the install is left untouched.
6. **Outcome** — clear success/error summary.

**Generating client-updater.py:** Running `release --client` (or `publish`) reads your `modpackctl.toml` and substitutes your GitHub username and repo name into `client-updater.py`, writing the result to `releases/client-updater.py`. This is the file players download — it is pre-configured for your repo and requires no setup on their end.

**Distribution:** `publish` uploads two assets to the GitHub Release: the modpack zip and the pre-configured `releases/client-updater.py`. `publish` also pushes enriched snapshots (with mod names) to the `gh-pages` branch so the changelog displays real names like "Sodium" instead of project IDs.

- **New players** — download and extract the modpack zip, then download `client-updater.py` (save it anywhere).
- **Existing players** — re-run their saved `client-updater.py`; the prefs file remembers the modpack folder.

**Player prefs:** the last-selected modpack folder is saved to `~/.modpack-updater/` (namespaced per modpack).

**Requirements for players:** Python 3.8+ and an internet connection. No extra packages needed; `client-updater.py` uses only the standard library.

## Repository Layout

```
.modpackctl/
  log.json          — version history with diff stats
  mod_index.json    — current mod list (project_id -> file, category)
  mod_cache.json    — CurseForge API cache (mod names and file names)
  snapshots/        — per-commit mod state (used for diffs and updates)
  overrides/        — stored CurseForge overrides (configs, resource files)
  dl_cache/         — persistent jar store (avoids re-downloading on rebuild)
build/              — current working build (mods/, shaderpacks/, resourcepacks/)
releases/           — output zips from release / publish
modpackctl.toml     — your config (not committed)
```

## GitHub Pages Integration

`publish` maintains a `gh-pages` branch with `versions.json` at the repo root. `client-updater.py` reads this file to discover available versions and the commit hash needed to fetch the corresponding mod snapshot.

The branch is created automatically as an orphan on first publish.