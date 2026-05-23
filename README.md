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
# Display name shown in the updater GUI and used as the release zip prefix
modpack_name = "YourModpackName"

# Optional: override the zip file prefix if it should differ from modpack_name
# file_prefix = "YourModpackName"

# URL to a logo image shown in the updater header (optional; PNG or GIF, ~32px tall)
# logo_url = "https://example.com/logo.png"

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
| `commit <zip> [--major] [--message "..."]` | Record a new version from an updated export. Version is bumped automatically. `--major` forces a major bump. `--message` sets the release note shown to players in the updater changelog. |
| `log` | List all committed versions with diff stats. |
| `changelog <v1> [output.md] [--client\|--server]` | Generate a changelog for `v1` as an initial release. |
| `changelog <v1> <v2> [output.md] [--client\|--server]` | Generate a changelog between two versions. `--client` excludes server-only mods; `--server` excludes client-only mods, shaderpacks, and resourcepacks. |
| `release <version> [--client\|--server]` | Build a release zip. `--client` also produces a baked `releases/client-updater.py`. Without a flag, includes all mods. |
| `publish <version> [--message "..."]` | Build a client release, create a GitHub Release with client-filtered changelog notes, and push `versions.json` and `snapshots/` to `gh-pages`. `--message` overrides the message set at `commit` time. |
| `update <version> [--client\|--server]` | Rebuild the `build/` folder for a version without zipping. |
| `purge [--all]` | Remove stale files from the download cache. Without `--all`, only removes mods not in the latest snapshot. |
| `build-pages` | Write `versions.json` and `snapshots/` to a local `gh-pages/` folder. Useful for manually pushing to `gh-pages` if `publish` fails. |
| `bake-updater` | Write a pre-configured `releases/client-updater.py` using credentials from `modpackctl.toml`, without building a full release. |
| `export-example` | Write the built-in config template to `modpackctl.toml.example`. |

## Version Bumping

Versions follow `major.minor.patch`. The next version is calculated automatically when you `commit`:

- Mods added or removed → minor bump, patch resets
- Mods updated only → patch bump
- Modloader version changes → major bump (automatic)
- `--major` flag → always bumps major

## README Auto-Update

Place the `__MODLOADER__` placeholder anywhere in your `README.md` to have modpackctl keep it up to date automatically:

```markdown
Modloader: __MODLOADER__
```

On the first `commit` that detects a modloader version, `__MODLOADER__` is replaced with the actual id (e.g. `neoforge-21.1.229`). On every subsequent `commit` where the modloader changes, the old id is replaced with the new one in-place. No action is needed beyond the initial placeholder — modpackctl handles it from there.

## Player Updater

`client-updater.py` is a standalone Tkinter GUI. Players can save it anywhere (Desktop, Downloads, etc.) and double-click it to update their modpack — the script asks for the modpack folder rather than needing to live inside it.

The flow:

1. **Folder picker** — autodetects a likely `.minecraft` folder and remembers the last choice between runs.
2. **Checking** — fetches `versions.json` from GitHub Pages.
3. **Version select** — dropdown defaulting to the latest version; includes a **Fresh install** checkbox to wipe existing mods and re-download everything clean. On confirm, the relevant snapshots are fetched from GitHub Pages.
4. **Changelog** — shows exactly what will be added, removed, and updated, by mod name.
5. **Confirm & Update** — explicit click before any files are touched.
6. **Atomic install** — all new files download to a temp folder first; if anything fails the install is left untouched.
7. **Outcome** — clear success/error summary.

A **⚙ gear button** in the header opens the colour settings dialog, where players can customise all UI colours with a colour picker. Settings are saved to prefs and persist between runs.

**Generating client-updater.py:** Running `release --client` (or `publish`) reads your `modpackctl.toml` and substitutes placeholders in `client-updater.py`, writing the result to `releases/client-updater.py`. This is the file players download — it is pre-configured for your repo and requires no setup on their end.

Use these placeholders as plain string literals anywhere in `client-updater.py`:

| Placeholder | Replaced with |
|---|---|
| `"__GITHUB_USER__"` | `github.user` from `modpackctl.toml` |
| `"__GITHUB_REPO__"` | `github.repo` from `modpackctl.toml` |
| `"__MODPACK_NAME__"` | `settings.modpack_name` from `modpackctl.toml` |
| `"__LOGO_URL__"` | `settings.logo_url`, or an empty string if not set |

Run `bake-updater` to bake without building a full release.

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

`publish` maintains a `gh-pages` branch with two things:

- `versions.json` — lists every released version and its commit hash
- `snapshots/{commit}.json` — an enriched mod list for each version (mod names, filenames, categories)

`client-updater.py` fetches `versions.json` to find the latest version, then fetches the two relevant snapshots to compute what changed since the player's current version.

The branch is created automatically as an orphan on first publish.