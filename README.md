# modpackctl

A lightweight CLI for managing a CurseForge modpack across versions, with git-backed publishing to GitHub Releases and a self-updating GUI for players.

## Overview

`modpackctl.py` is the pack maintainer's tool. It tracks mod versions by importing CurseForge export zips, generates changelogs, builds client and server release zips, and publishes them to GitHub with a single command.

`{file_prefix}-client-updater.py` (and its compiled `.exe`) ships alongside the release zip. Players double-click it to check for updates and apply them without re-downloading the entire modpack.

## Prerequisites

- Python 3.11+
- `pip install requests`
- [GitHub CLI](https://cli.github.com) (`gh`) — required only for `publish`

## Global Setup (optional)

To run `modpackctl` from any directory instead of `python /path/to/modpackctl.py`:

**Windows (CMD or PowerShell):**

Add the `commands/` subdirectory to your `PATH`:

1. Open Start → search **"Edit environment variables for your account"**
2. Select `Path` under User variables → click **Edit** → **New**
3. Paste the full path to the `commands` subdirectory (e.g. `C:\Tools\modpackctl\commands`)
4. Click OK and restart your terminal

The included `modpackctl.cmd` is picked up automatically once the directory is in PATH.

**Linux / macOS:**

Make the shell script executable, then either add `commands/` to your `PATH` or symlink the script:

```bash
chmod +x /path/to/modpackctl/commands/modpackctl

# Option A — add the commands directory to PATH (edit to match your shell's rc file)
echo 'export PATH="/path/to/modpackctl/commands:$PATH"' >> ~/.bashrc
source ~/.bashrc

# Option B — symlink into an existing bin directory
ln -s /path/to/modpackctl/commands/modpackctl ~/.local/bin/modpackctl
```

After either option you can run from any modpack directory:

```
modpackctl commit MyModpack.zip
```

## Shell Autocomplete (optional)

Tab-completion for subcommands and flags is available via [argcomplete](https://github.com/kislyuk/argcomplete). Complete [Global Setup](#global-setup-optional) first.

1. Install:
   ```
   pip install argcomplete
   ```

2. Register for your shell:

   **PowerShell (Windows):**

   Open your profile for editing:
   ```powershell
   New-Item -ItemType File -Path $PROFILE -Force
   notepad $PROFILE
   ```

   Paste the following block and save:
   ```powershell
   Register-ArgumentCompleter -Native -CommandName modpackctl -ScriptBlock {
       param($wordToComplete, $commandAst, $cursorPosition)
       $tmpFile = [System.IO.Path]::GetTempFileName()
       $env:_ARGCOMPLETE = '1'
       $env:COMP_LINE = $commandAst.ToString()
       $env:COMP_POINT = "$cursorPosition"
       $env:_ARGCOMPLETE_STDOUT_FILENAME = $tmpFile
       modpackctl.cmd 2>$null
       Remove-Item Env:\_ARGCOMPLETE, Env:\COMP_LINE, Env:\COMP_POINT, Env:\_ARGCOMPLETE_STDOUT_FILENAME -ErrorAction SilentlyContinue
       if (Test-Path $tmpFile) {
           (Get-Content $tmpFile -Raw) -split [char]11 | Where-Object { $_ } |
               ForEach-Object { [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_) }
           Remove-Item $tmpFile -Force
       }
   }
   ```

   Then reload your profile:
   ```powershell
   . $PROFILE
   ```

   **Bash (Linux/macOS):**
   ```bash
   echo 'eval "$(register-python-argcomplete modpackctl)"' >> ~/.bashrc
   source ~/.bashrc
   ```

   **Zsh (Linux/macOS):**
   ```zsh
   echo 'autoload -U bashcompinit && bashcompinit' >> ~/.zshrc
   echo 'eval "$(register-python-argcomplete modpackctl)"' >> ~/.zshrc
   source ~/.zshrc
   ```

After this, `modpackctl <Tab>` completes subcommands and `modpackctl <command> <Tab>` completes flags for that subcommand.

## Setup

1. Create an empty folder for your modpack project and open a terminal there.

2. Export your modpack from CurseForge (the `.zip` file) and note its path.

3. Run the `init` command. Because no `modpackctl.toml` exists yet, modpackctl will prompt you to initialize the working directory first — this copies `modpackctl.example.toml` → `modpackctl.toml`, copies the updater templates, and creates a git repo, then exits. If any of the example files are missing from the modpackctl install directory they are downloaded from the modpackctl GitHub repo automatically:

```
modpackctl init MyModpack-1.0.0.zip
```

```
No modpackctl.toml found in the current directory.
Initialize a working directory here? (This will also create a git repo.) [y/N] y
[INFO] Copied modpackctl.example.toml → modpackctl.toml — you can customise it for this modpack.
...
Working directory initialized. Edit modpackctl.toml then re-run your command.
```

4. Edit `modpackctl.toml` and fill in your GitHub details:

```toml
[github]
user = "YourGitHubUsername"
repo = "YourRepoName"
```

5. Change any of the other options to personalise your modpack:

```
[settings]
# Display name shown in the updater GUI and used as the release zip prefix
modpack_name = "<YourModpackName>"

# Optional: override the zip file prefix if it should differ from modpack_name
# file_prefix = "<YourModpackName>"

# Modpack author shown in the CurseForge export manifest.json
# author = "YourName"

# RAM recommended to players in MB (shown in CurseForge launcher, optional)
# recommended_ram = 8192

# URL to a logo image shown in the updater header and used as the modpack image in CurseForge exports (optional; PNG or GIF)
# logo_url = "https://example.com/logo.png"

# Whether to include the Konami code easter egg (optional; default: true)
# enable_secret = false

# YouTube URL for the secret easter egg video (optional; defaults to Never Gonna Give You Up)
# secret_video_url = "https://www.youtube.com/watch?v=..."

# Whether to show the rainbow effect in the easter egg (optional; default: false)
# enable_rainbow = true

# CurseForge project IDs to exclude from client releases
server_only = [123456, 789012]

# CurseForge project IDs to exclude from server releases
client_only = [345678]
```

5. Re-run `init` to import your first modpack version and initialize the version history:

```
modpackctl init MyModpack-1.0.0.zip
```

6. Once modpackctl prints the remote setup reminder, add your GitHub remote and push:

```
git remote add origin https://github.com/<user>/<repo>.git
git push -u origin main
```

## Typical Workflow

```
# Export updated modpack from CurseForge, then:
python modpackctl.py commit MyModpack-updated.zip

# Review what changed and view version numbers
python modpackctl.py log

# View more detailed changelog between any two versions
python modpackctl.py changelog 1.0.0 1.1.0

# Generate a changelog for just the latest version
python modpackctl.py changelog

# Publish the latest client version to GitHub (builds zip + creates release + updates versions.json)
python modpackctl.py publish

# Publish a specific client version
python modpackctl.py publish 1.2.0

# Optionally include a message in the release notes
python modpackctl.py publish --message "Improved performance and fixed crashes."
```

## Commands

| Command | Description |
|---|---|
| `init <zip> [--force]` | Initialize from a CurseForge export zip. `--force` resets history but keeps the download cache. |
| `commit <zip> [--major] [--message "..."]` | Record a new version from an updated export. Version is bumped automatically. `--major` forces a major bump. `--message` sets the release note shown to players in the updater changelog. |
| `log` | List all committed versions with diff stats. |
| `remove-commit [version]` | Permanently remove a committed version from history. Prompts for confirmation. Irreversible. `version` defaults to the latest committed version if omitted. |
| `set-message [version] [message]` | Set the release note for any committed version. `version` defaults to the latest committed version if omitted. Omit the message to clear it. |
| `changelog [v1] [v2] [--out output.md] [--server]` | Generate a changelog. With no versions, generates a single-version changelog for the latest version. With one version, treats it as an initial release. With two versions, diffs between them. `--server` excludes client-only mods, shaderpacks, and resourcepacks. |
| `release [version] [--server]` | Build a release zip and update `gh-pages/` locally. Client (default): also builds a CurseForge export zip, bakes `releases/{file_prefix}-client-updater.py`, and compiles `releases/{file_prefix}-client-updater.exe` (if PyInstaller is available). `--server`: bakes `releases/{file_prefix}-server-updater.py` instead (no exe, no CurseForge zip). `version` defaults to the latest committed version if omitted. |
| `publish [version] [--message "..."]` | Build a client release (calls `release` internally), create a GitHub Release with client-filtered changelog notes, push `versions.json` and `snapshots/` to `gh-pages`, and push an updated `README.md` and `.gitignore` to the working repo. Uploads the zip, baked `.py`, and `.exe` (if built). `version` defaults to the latest committed version if omitted. `--message` overrides the message set at `commit` time. |
| `update [version] [--server]` | Rebuild the `build/` folder for a version without zipping or producing any release artifacts. Defaults to client view; `--server` excludes client-only mods, shaderpacks, and resourcepacks. `version` defaults to the latest committed version if omitted. |
| `purge [--all]` | Remove stale files from the download cache. Without `--all`, only removes cached files not in the latest snapshot. |
| `build-pages` | Write `versions.json`, `snapshots/`, and `overrides.zip` to a local `gh-pages/` folder. Also runs automatically as part of `release`. Useful for a standalone refresh or manually pushing to `gh-pages` if `publish` fails. |
| `bake-updater [--server]` | Bake `releases/{file_prefix}-client-updater.py` from the client updater template. `--server` bakes `releases/{file_prefix}-server-updater.py` instead (no exe). |
| `reset-file --client\|--server\|--config\|--all` | Reset a working copy in the current directory from its example template. `--client` overwrites `client-updater.py`, `--server` overwrites `server-updater.py`, `--config` overwrites `modpackctl.toml` with `modpackctl.example.toml`, `--all` resets all three. A flag is required. If an example template is missing from the modpackctl install directory it is downloaded from the modpackctl GitHub repo automatically. |
| `build-exe` | Build `releases/{file_prefix}-client-updater.exe` from the baked client updater using PyInstaller. When `enable_secret` is true, also downloads and bundles the easter egg video and audio. Requires `pip install pyinstaller yt-dlp imageio-ffmpeg Pillow`. Also runs automatically as part of `release`. |
| `export-cf <version>` | Build a CurseForge-format modpack zip for the given version, suitable for importing directly into the CurseForge launcher. Includes `manifest.json`, `modlist.html`, and the stored overrides with `bcc-common.toml` stamped with the correct version. |

## Version Bumping

Versions follow `major.minor.patch`. The next version is calculated automatically when you `commit`:

- Files added or removed → minor bump, patch resets
- Files updated only → patch bump
- Modloader version changes → major bump (automatic)
- `--major` flag → always bumps major

## README Auto-Update

Place any of the following placeholders in `README.template.md`. Each time `publish` or `release` runs it renders a fresh `README.md` from the template with all current values substituted. `README.template.md` is never overwritten by modpackctl and is not committed to the repo — only the rendered `README.md` is pushed. Server builds (`--server`) never touch the README.

If `README.template.md` does not exist when `publish` runs, it is created automatically from the bundled `README.example.md` (downloading it from the modpackctl GitHub repo if needed).

Supported placeholders:

| Placeholder | Replaced with |
|---|---|
| `__MODLOADER__` | Full modloader id, e.g. `neoforge-21.1.229`. Kept in sync whenever the modloader changes. |
| `__MODLOADER_TYPE__` | Loader name only, e.g. `NeoForge`. Kept in sync whenever the modloader changes. |
| `__MINECRAFT_VERSION__` | Minecraft version, e.g. `1.21.1`. Kept in sync whenever the MC version changes. |
| `__LATEST_VERSION__` | Modpack version, e.g. `1.2.0`. Kept in sync on every build. |
| `__MODPACK_NAME__` | `settings.modpack_name` from `modpackctl.toml` |
| `__FILE_PREFIX__` | `settings.file_prefix`, falling back to `settings.modpack_name` |
| `__RELEASES_URL__` | `https://github.com/<user>/<repo>/releases`, constructed from `modpackctl.toml` |
| `__AUTHOR__` | `settings.author` from `modpackctl.toml` |
| `__SERVER_ADDRESS__` | `settings.server_address` from `modpackctl.toml` |
| `__DISCORD_URL__` | `settings.discord_url` from `modpackctl.toml` |
| `__MAP_URL__` | `settings.map_url` from `modpackctl.toml` |

`README.example.md` in the modpackctl directory is the default starting template.

## Player Updater

`client-updater.py` is a standalone Tkinter GUI. Players can save it anywhere (Desktop, Downloads, etc.) and double-click it to update their modpack — the script asks for the modpack folder rather than needing to live inside it.

The flow:

1. **Folder picker** — autodetects a likely `.minecraft` folder and remembers the last choice between runs.
2. **Checking** — fetches `versions.json` from GitHub Pages.
3. **Version select** — dropdown defaulting to the latest version; includes a **Fresh install** checkbox to wipe existing files and re-download everything clean. On confirm, the relevant snapshots are fetched from GitHub Pages.
4. **Changelog** — shows exactly what will be added, removed, and updated, by name.
5. **Confirm & Update** — explicit click before any files are touched.
6. **Atomic install** — all new files download to a temp folder first; if anything fails the install is left untouched.
7. **Outcome** — clear success/error summary.

A **⚙ gear button** in the header opens the colour settings dialog, where players can customise all UI colours with a colour picker. Settings are saved to prefs and persist between runs.

**Generating the client updater:** Running `release` (or `publish`, which calls `release` internally) reads your `modpackctl.toml` and substitutes placeholders in `client-updater.example.py`, writing the result to `releases/{file_prefix}-client-updater.py`. It then attempts to build `releases/{file_prefix}-client-updater.exe` via PyInstaller. Both files are pre-configured for your repo and require no setup on the player's end. Run `bake-updater` to produce just the script without building a release zip.

Use these placeholders as plain string literals anywhere in `client-updater.example.py`:

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

**Distribution:** `publish` uploads up to four assets to the GitHub Release: the modpack zip, the CurseForge export zip, `{file_prefix}-client-updater.py`, and `{file_prefix}-client-updater.exe` (if the PyInstaller build succeeded). `publish` also pushes snapshots to the `gh-pages` branch so the changelog displays real names like "Sodium" instead of project IDs.

- **New players** — download and extract the modpack zip, then download either `{file_prefix}-client-updater.exe` (no Python needed) or `{file_prefix}-client-updater.py` (requires Python 3.8+). Save it anywhere — Desktop, Downloads, etc.
- **Existing players** — re-run their saved updater; the prefs file remembers the modpack folder.

**Player prefs:** the last-selected modpack folder is saved to `~/.modpack-updater/` (namespaced per modpack).

**Better Compatibility Checker integration:** If the [Better Compatibility Checker](https://www.curseforge.com/minecraft/mc-mods/better-compatibility-checker) mod is in the pack, the updater reads and writes the installed version via `config/bcc-common.toml` (`modpackVersion` and `modpackName` fields). If the file is absent it is created automatically on the first update. This keeps the in-game version display in sync with what the updater installs.

**Requirements for players (`.py` version):** Python 3.8+ and an internet connection. The updater itself uses only the standard library. When the easter egg is triggered for the first time, additional packages are installed automatically via pip in the background (`yt-dlp`, `Pillow`, `imageio`, `imageio-ffmpeg`) and the video is downloaded and cached in `~/.modpack-updater/`; players do not need to install anything manually.

**Requirements for players (`.exe` version):** An internet connection is required to check for modpack updates — no Python installation needed. The easter egg video (when enabled) is bundled directly in the exe and works offline.

## Server Updater

`server-updater.example.py` is a CLI script for keeping the server's mods folder in sync with published releases. It excludes client-only mods (as listed in `versions.json`) and non-mod categories (shaderpacks, resourcepacks).

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
| `--workers N` | Number of parallel download workers (default: 10). |

The script detects and records the installed version via `config/bcc-common.toml` (`modpackVersion` field), matching the [Better Compatibility Checker](https://www.curseforge.com/minecraft/mc-mods/better-compatibility-checker) mod format. If the file is absent it is created automatically on the first update. The script defaults to a fresh install if no version is detected.

**Generating:** Running `release --server` (or `bake-updater --server` for just the script) writes `releases/{file_prefix}-server-updater.py` with all placeholders baked in.

Use these placeholders as plain string literals anywhere in `server-updater.example.py`:

| Placeholder | Replaced with |
|---|---|
| `"__GITHUB_USER__"` | `github.user` from `modpackctl.toml` |
| `"__GITHUB_REPO__"` | `github.repo` from `modpackctl.toml` |
| `"__MODPACK_NAME__"` | `settings.modpack_name` from `modpackctl.toml` |

**Requirements:** Python 3.8+ and an internet connection. Uses only the standard library.

## Overrides

Files stored in `.modpackctl/overrides/` (configs, player models, KubeJS scripts, etc.) are automatically packaged into `overrides.zip` and published to `gh-pages` as part of every `release`. Both updaters download and apply this zip after installing mods.

Both updaters always apply overrides after installing mods, but the behaviour depends on the **Reset config files to defaults** option (default: No in all cases):

- **Reset unchecked (default):** only files that do not already exist in the install directory are written. Existing configs are never overwritten, so players keep any customisations they have made.
- **Reset checked:** all override files are extracted, overwriting any existing versions and resetting them to the pack defaults.

The `--yes` flag on the server updater skips the prompt and defaults to No (new files only).

## Repository Layout

```
.modpackctl/
  log.json          — version history with diff stats
  mod_cache.json    — CurseForge API cache (mod names and file names)
  snapshots/        — per-commit file state (used for diffs and updates)
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
- `snapshots/{commit}.json` — the file list for each version (names, filenames, categories)

The updater fetches `versions.json` to find the latest version, then fetches the two relevant snapshots to compute what changed since the player's current version.

The branch is created automatically as an orphan on first publish.