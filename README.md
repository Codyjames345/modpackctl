# modpackctl

A lightweight CLI for managing a CurseForge modpack across versions, with git-backed publishing to GitHub Releases and a self-updating GUI for players.

## Overview

`modpackctl.py` is the pack maintainer's tool. It tracks mod versions by importing CurseForge export zips, generates changelogs, builds client and server release zips, and publishes them to GitHub with a single command.

`{file_prefix}-updater.py` (and its compiled `.exe`) ships alongside the release zip. Players double-click it to check for updates and apply them without re-downloading the entire modpack.

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

# Modpack author shown in the CurseForge export manifest.json
# author = "YourName"

# RAM recommended to players in MB (shown in CurseForge launcher, optional)
# recommended_ram = 8192

# Optional: override the zip file prefix if it should differ from modpack_name
# file_prefix = "YourModpackName"

# URL to a logo image shown in the updater header and used as the modpack image in CurseForge exports (optional; PNG or GIF)
# logo_url = "https://example.com/logo.png"

# Whether to include the Konami code easter egg (optional; default: true)
# enable_secret = true

# YouTube URL for the secret easter egg video (optional; defaults to Never Gonna Give You Up)
# secret_video_url = "https://www.youtube.com/watch?v=..."

# Whether to show the rainbow effect in the easter egg (optional; default: false)
# enable_rainbow = true

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
| `changelog <v1> [--out output.md] [--server]` | Generate a client changelog for `v1` as an initial release. `--server` excludes client-only mods, shaderpacks, and resourcepacks. |
| `changelog <v1> <v2> [--out output.md] [--server]` | Generate a client changelog between two versions. `--server` excludes client-only mods, shaderpacks, and resourcepacks. |
| `release <version> [--server]` | Build a client release zip and CurseForge export zip, bake `releases/{file_prefix}-client-updater.py`, and build `releases/{file_prefix}-client-updater.exe` (if PyInstaller is available). `--server` builds a server release zip and bakes `releases/{file_prefix}-server-updater.py` instead (no exe, no CurseForge zip). |
| `publish <version> [--message "..."]` | Build a client release, create a GitHub Release with client-filtered changelog notes, and push `versions.json` and `snapshots/` to `gh-pages`. Uploads the zip, baked `.py`, and `.exe` (if built). `--message` overrides the message set at `commit` time. |
| `update <version> [--server]` | Rebuild the `build/` folder for a version without zipping. Defaults to client view; `--server` excludes client-only mods, shaderpacks, and resourcepacks. |
| `purge [--all]` | Remove stale files from the download cache. Without `--all`, only removes mods not in the latest snapshot. |
| `build-pages` | Write `versions.json` and `snapshots/` to a local `gh-pages/` folder. Useful for manually pushing to `gh-pages` if `publish` fails. |
| `bake-updater [--server]` | Bake `releases/{file_prefix}-client-updater.py` from the client updater template. `--server` bakes `releases/{file_prefix}-server-updater.py` instead (no exe). |
| `build-exe` | Build `releases/{file_prefix}-client-updater.exe` from the baked client updater using PyInstaller. When `enable_secret` is true, also downloads and bundles the easter egg video and audio. Requires `pip install pyinstaller yt-dlp imageio-ffmpeg Pillow`. Also runs automatically as part of `release`. |
| `export-cf <version>` | Build a CurseForge-format modpack zip for the given version, suitable for importing directly into the CurseForge launcher. Includes `manifest.json`, `modlist.html`, and the stored overrides with `bcc-common.toml` stamped with the correct version. |
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

**Generating the client updater:** Running `release` (or `publish`) reads your `modpackctl.toml` and substitutes placeholders in `client-updater-template.py`, writing the result to `releases/{file_prefix}-client-updater.py`. It then attempts to build `releases/{file_prefix}-client-updater.exe` via PyInstaller. Both files are pre-configured for your repo and require no setup on the player's end. Run `bake-updater` to produce just the script without building a release zip.

Use these placeholders as plain string literals anywhere in `client-updater-template.py`:

| Placeholder | Replaced with |
|---|---|
| `"__GITHUB_USER__"` | `github.user` from `modpackctl.toml` |
| `"__GITHUB_REPO__"` | `github.repo` from `modpackctl.toml` |
| `"__MODPACK_NAME__"` | `settings.modpack_name` from `modpackctl.toml` |
| `"__LOGO_URL__"` | `settings.logo_url`, or an empty string if not set |
| `"__ENABLE_SECRET__"` | `settings.enable_secret` as `True` or `False` (default: `True`) |
| `"__SECRET_VIDEO_URL__"` | `settings.secret_video_url`, or the default Never Gonna Give You Up URL |
| `"__ENABLE_RAINBOW__"` | `settings.enable_rainbow` as `True` or `False` (default: `False`) |

Run `build-exe` to compile the `.exe` from an already-baked script.

To enable exe building, install the build dependencies once: `pip install pyinstaller yt-dlp imageio-ffmpeg Pillow`

**Distribution:** `publish` uploads up to four assets to the GitHub Release: the modpack zip, the CurseForge export zip, `{file_prefix}-client-updater.py`, and `{file_prefix}-client-updater.exe` (if the PyInstaller build succeeded). `publish` also pushes enriched snapshots (with mod names) to the `gh-pages` branch so the changelog displays real names like "Sodium" instead of project IDs.

- **New players** — download and extract the modpack zip, then download either `{file_prefix}-client-updater.exe` (no Python needed) or `{file_prefix}-client-updater.py` (requires Python 3.8+). Save it anywhere — Desktop, Downloads, etc.
- **Existing players** — re-run their saved updater; the prefs file remembers the modpack folder.

**Player prefs:** the last-selected modpack folder is saved to `~/.modpack-updater/` (namespaced per modpack).

**Better Compatibility Checker integration:** If the [Better Compatibility Checker](https://www.curseforge.com/minecraft/mc-mods/better-compatibility-checker) mod is in the pack, the updater reads and writes the installed version via `config/bcc-common.toml` (`modpackVersion` and `modpackName` fields). If the file is absent it is created automatically on the first update. This keeps the in-game version display in sync with what the updater installs.

**Requirements for players (`.py` version):** Python 3.8+ and an internet connection. The updater itself uses only the standard library. When the easter egg is triggered for the first time, additional packages are installed automatically via pip in the background (`yt-dlp`, `Pillow`, `imageio`, `imageio-ffmpeg`) and the video is downloaded and cached in `~/.modpack-updater/`; players do not need to install anything manually.

**Requirements for players (`.exe` version):** An internet connection is required to check for modpack updates — no Python installation needed. The easter egg video (when enabled) is bundled directly in the exe and works offline.

## Server Updater

`server-updater-template.py` is a CLI script for keeping the server's mods folder in sync with published releases. It excludes client-only mods (as listed in `versions.json`) and non-mod categories (shaderpacks, resourcepacks).

```
python server-updater.py [server_dir] [--version VERSION] [--fresh] [--yes] [--workers N]
```

| Argument | Description |
|---|---|
| `server_dir` | Path to the server directory. Defaults to the current directory. |
| `--version VERSION` | Target version to install. Defaults to latest. |
| `--fresh` | Wipe the mods folder and re-download everything clean. |
| `--no-fresh` | Force an incremental update even if no version is detected. |
| `--yes` | Skip the confirmation prompt (useful for automated deployments). |
| `--workers N` | Number of parallel download threads (default: 4). |

The script detects and records the installed version via `config/bcc-common.toml` (`modpackVersion` field), matching the [Better Compatibility Checker](https://www.curseforge.com/minecraft/mc-mods/better-compatibility-checker) mod format. If the file is absent it is created automatically on the first update. The script defaults to a fresh install if no version is detected.

**Generating:** Run `bake-updater --server` to write `releases/{file_prefix}-server-updater.py` with your GitHub credentials baked in.

Use these placeholders as plain string literals anywhere in `server-updater-template.py`:

| Placeholder | Replaced with |
|---|---|
| `"__GITHUB_USER__"` | `github.user` from `modpackctl.toml` |
| `"__GITHUB_REPO__"` | `github.repo` from `modpackctl.toml` |
| `"__MODPACK_NAME__"` | `settings.modpack_name` from `modpackctl.toml` |

**Requirements:** Python 3.8+ and an internet connection. Uses only the standard library.

## Repository Layout

```
.modpackctl/
  log.json          — version history with diff stats
  mod_index.json    — current mod list (project_id -> file, category)
  mod_cache.json    — CurseForge API cache (mod names and file names)
  snapshots/        — per-commit mod state (used for diffs and updates)
  overrides/        — stored CurseForge overrides (configs, resource files)
  dl_cache/         — persistent jar store (avoids re-downloading on rebuild)
.pyinstaller/       — PyInstaller build cache (not committed)
build/              — current working build (mods/, shaderpacks/, resourcepacks/)
releases/           — output zips, {file_prefix}-updater.py, and {file_prefix}-updater.exe
modpackctl.toml     — your config (not committed)
```

## GitHub Pages Integration

`publish` maintains a `gh-pages` branch with two things:

- `versions.json` — lists every released version and its commit hash
- `snapshots/{commit}.json` — an enriched mod list for each version (mod names, filenames, categories)

The updater fetches `versions.json` to find the latest version, then fetches the two relevant snapshots to compute what changed since the player's current version.

The branch is created automatically as an orphan on first publish.