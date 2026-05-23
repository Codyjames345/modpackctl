import argparse
import tempfile
try:
    import argcomplete
except ModuleNotFoundError:
    argcomplete = None  # type: ignore[assignment]
import json
import re
import zipfile
import shutil
import requests
import hashlib
import time
import os
import sys
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TypeVar, cast
try:
    import tomllib          # stdlib from Python 3.11+
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[import]
    except ModuleNotFoundError:
        print("[ERROR] Python 3.11+ is required, or install tomli: pip install tomli")
        sys.exit(1)
from pathlib import Path
from urllib.parse import unquote, urlparse

_JsonT = TypeVar("_JsonT", list, dict)

# -------------------------
# STORAGE
# -------------------------

_HERE           = Path(__file__).parent      # directory containing modpackctl.py

REPO            = Path(".modpackctl")
SNAPSHOTS       = REPO / "snapshots"
LOG_FILE        = REPO / "log.json"
CACHE           = REPO / "mod_cache.json"    # project_id -> { name, files: { file_id: filename } }
DL_CACHE        = REPO / "dl_cache"          # permanent jar store keyed by (project_id, file_id)
OVERRIDES_STORE = REPO / "overrides"
CONFIG_FILE     = Path("modpackctl.toml")

BUILD            = Path("build")
RELEASES         = Path("releases")
README           = Path("README.md")
PYINSTALLER      = Path(".pyinstaller")
PAGES_OUTPUT     = Path("gh-pages")
CONFIG_EXAMPLE   = Path("modpackctl.toml.example")
CLIENT_UPDATE_SCRIPT = Path("client-updater-template.py")   # client updater source template
SERVER_UPDATE_SCRIPT = Path("server-updater-template.py")   # server updater source template
_DANCE_DEFAULT_URL  = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

_GITIGNORE_ENTRIES  = [
    "build/",
    "releases/",
    ".modpackctl/dl_cache/",
    ".pyinstaller/",
    "gh-pages/"
]


def _baked_client_updater_path() -> Path:
    """Return releases/{file_prefix}-client-updater.py — the baked client updater output."""
    return RELEASES / f"{get_file_prefix()}-client-updater.py"


def _baked_server_updater_path() -> Path:
    """Return releases/{file_prefix}-server-updater.py — the baked server updater output."""
    return RELEASES / f"{get_file_prefix()}-server-updater.py"

CF_URL  = "https://api.cfwidget.com/{}"
HEADERS = {"User-Agent": "modpackctl/1.0"}

_LOADER_DISPLAY_NAMES: dict[str, str] = {
    "neoforge": "NeoForge",
    "forge":    "Forge",
    "fabric":   "Fabric",
    "quilt":    "Quilt",
}

# -------------------------
# CONFIG
# -------------------------

DEFAULT_CONFIG = """\
[github]
user = "<yourName>"
repo = "<yourRepo>"

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

# List project IDs to exclude from client releases (server-side only mods)
server_only = []

# List project IDs to exclude from server releases (client-side only mods)
client_only = []
"""


def load_config() -> dict:
    """Load and return the TOML config. Exits with an error if the file does not exist."""
    if not CONFIG_FILE.exists():
        print(f"[ERROR] {CONFIG_FILE} not found. Run modpackctl from a working directory, or re-run to initialize one.")
        sys.exit(1)
    with open(CONFIG_FILE, "rb") as fh:
        return tomllib.load(fh)


def _init_git_repo() -> None:
    """
    Ensure the current directory is a git repository with a suitable .gitignore.
    Skips git init if already inside a repo. Appends any missing entries to an
    existing .gitignore rather than overwriting it.
    """
    already_git = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
    ).returncode == 0

    if not already_git:
        subprocess.run(["git", "init"], check=True)
        print("[OK] Initialized git repository.")

    gitignore = Path(".gitignore")
    if not gitignore.exists():
        gitignore.write_text("\n".join(_GITIGNORE_ENTRIES) + "\n", encoding="utf-8")
        print("[OK] Created .gitignore.")
    else:
        existing_lines = set(gitignore.read_text(encoding="utf-8").splitlines())
        missing = [e for e in _GITIGNORE_ENTRIES if e not in existing_lines]
        if missing:
            with open(gitignore, "a", encoding="utf-8") as fh:
                fh.write("\n".join(missing) + "\n")
            print("[OK] Updated .gitignore with modpackctl entries.")

    if not already_git:
        print("[INFO] Once you've created the GitHub repo and edited the config, add a remote:")
        print("       git remote add origin https://github.com/<user>/<repo>.git")


def _init_working_dir() -> None:
    """
    If no modpackctl.toml exists in the current directory, prompt the user to initialize
    a working directory here. On confirmation, copies modpackctl.toml.example and both
    updater templates from the script directory, initializes a git repo, then exits so
    the user can edit the config.
    """
    if CONFIG_FILE.exists():
        return
    print(f"No {CONFIG_FILE} found in the current directory.")
    answer = input("Initialize a working directory here? (This will also create a git repo.) [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("[INFO] Aborted.")
        sys.exit(0)
    src_config = _HERE / CONFIG_EXAMPLE.name
    if src_config.exists():
        shutil.copy2(src_config, CONFIG_FILE)
        print(f"[OK] Copied {CONFIG_EXAMPLE.name} → {CONFIG_FILE}")
    else:
        CONFIG_FILE.write_text(DEFAULT_CONFIG, encoding="utf-8")
        print(f"[OK] Created {CONFIG_FILE} with defaults.")
    _ensure_template(CLIENT_UPDATE_SCRIPT)
    _ensure_template(SERVER_UPDATE_SCRIPT)
    _init_git_repo()
    print(f"\nWorking directory initialized. Edit {CONFIG_FILE} then re-run your command.")
    sys.exit(0)


def get_github_info() -> tuple[str, str]:
    """Return (user, repo) from the [github] section of the config."""
    cfg = load_config()
    try:
        return cfg["github"]["user"], cfg["github"]["repo"]
    except KeyError:
        print("[ERROR] Missing [github] config. Expected modpackctl.toml with:")
        print("  [github]")
        print('  user = "yourName"')
        print('  repo = "yourRepo"')
        sys.exit(1)


def get_file_prefix() -> str:
    """Return the prefix used when naming release zips (file_prefix if set, else modpack_name)."""
    cfg      = load_config()
    settings = cfg.get("settings", {})
    prefix   = settings.get("file_prefix") or settings.get("modpack_name")
    if not prefix:
        print("[ERROR] Missing modpack_name in [settings]. Expected modpackctl.toml with:")
        print("  [settings]")
        print('  modpack_name = "YourModpackName"')
        sys.exit(1)
    return prefix


def get_filter_list(key: str) -> set[str]:
    """Return the set of project ID strings for the given settings key (e.g. 'server_only')."""
    cfg = load_config()
    try:
        return {str(project_id) for project_id in cfg["settings"][key]}
    except KeyError:
        return set()


# -------------------------
# HELPERS
# -------------------------


def _run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    """Print a command then run it via subprocess.run."""
    print(f"$ {' '.join(str(arg) for arg in cmd)}")
    return subprocess.run(cmd, **kwargs)


def load_json(path: Path, default: _JsonT) -> _JsonT:
    """Return parsed JSON from path, or default if the file does not exist."""
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, data: _JsonT) -> None:
    """Write data as indented JSON to path, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


# -------------------------
# MANIFEST
# -------------------------


def load_manifest(path: Path | str) -> dict:
    """Load manifest.json from either a CurseForge .zip export or an unpacked directory."""
    path = Path(path)
    if path.is_file() and path.suffix == ".zip":
        with zipfile.ZipFile(path, "r") as zf:
            with zf.open("manifest.json") as fh:
                return json.load(fh)
    return json.loads((path / "manifest.json").read_text())


def validate_source(source: str) -> None:
    """Exit with an error if source is not a readable CurseForge export with a valid manifest.json."""
    path = Path(source)
    if not path.exists():
        print(f"[ERROR] Source '{source}' not found.")
        sys.exit(1)
    if path.is_file():
        if path.suffix.lower() != ".zip":
            print(f"[ERROR] Source must be a .zip file, got: {path.name}")
            sys.exit(1)
        try:
            with zipfile.ZipFile(path, "r") as zf:
                if "manifest.json" not in zf.namelist():
                    print(f"[ERROR] '{source}' does not contain manifest.json.")
                    sys.exit(1)
                manifest = json.load(zf.open("manifest.json"))
        except zipfile.BadZipFile:
            print(f"[ERROR] '{source}' is not a valid zip file.")
            sys.exit(1)
        except json.JSONDecodeError as exc:
            print(f"[ERROR] manifest.json in '{source}' is not valid JSON: {exc}")
            sys.exit(1)
    elif path.is_dir():
        manifest_path = path / "manifest.json"
        if not manifest_path.exists():
            print(f"[ERROR] '{source}' does not contain manifest.json.")
            sys.exit(1)
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError as exc:
            print(f"[ERROR] manifest.json in '{source}' is not valid JSON: {exc}")
            sys.exit(1)
    else:
        print(f"[ERROR] '{source}' is not a file or directory.")
        sys.exit(1)
    if "files" not in manifest:
        print(f"[ERROR] manifest.json is missing 'files' — is this a CurseForge export?")
        sys.exit(1)


def build_snapshot(manifest: dict, cache: dict) -> dict:
    """Build an enriched snapshot dict from a manifest, resolving names from cache where available."""
    return {
        str(mod["projectID"]): {
            "file_id":  str(mod["fileID"]),
            "name":     cache.get(str(mod["projectID"]), {}).get("name") or str(mod["projectID"]),
            "file":     "",
            "category": "",
        }
        for mod in manifest.get("files", [])
    }


def get_modloader_version(manifest: dict) -> str:
    """Return the primary modloader id string (e.g. 'neoforge-21.1.229'), or '' if absent."""
    loaders = manifest.get("minecraft", {}).get("modLoaders", [])
    for loader in loaders:
        if loader.get("primary", False):
            return loader.get("id", "")
    # Fall back to the first loader if none is marked primary
    return loaders[0].get("id", "") if loaders else ""


def _modloader_display(modloader_id: str) -> str:
    """Convert a manifest modloader id (e.g. 'neoforge-21.1.229') to 'NeoForge 21.1.229'."""
    if "-" in modloader_id:
        prefix, version = modloader_id.split("-", 1)
        name = _LOADER_DISPLAY_NAMES.get(prefix.lower(), prefix.capitalize())
        return f"{name} {version}"
    return modloader_id


def store_overrides(zip_path: Path | str) -> int:
    """
    Extract the overrides/ tree from a CurseForge zip into OVERRIDES_STORE,
    replacing any previously stored overrides. Returns the number of files stored.
    """
    zip_path = Path(zip_path)
    if not zip_path.is_file() or zip_path.suffix != ".zip":
        return 0

    if OVERRIDES_STORE.exists():
        shutil.rmtree(OVERRIDES_STORE)
    OVERRIDES_STORE.mkdir(parents=True, exist_ok=True)

    file_count = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        prefix = "overrides/"
        for member_name in zf.namelist():
            if not member_name.startswith(prefix):
                continue
            relative_path = member_name[len(prefix):]
            if not relative_path:
                continue
            out_path = OVERRIDES_STORE / relative_path
            if member_name.endswith("/"):
                out_path.mkdir(parents=True, exist_ok=True)
            else:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member_name) as src, open(out_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                file_count += 1
    return file_count


def apply_overrides(dest: Path) -> bool:
    """
    Copy the stored overrides tree into dest (the release build directory).
    Returns True if any files were copied, False if no overrides are stored.
    """
    if not OVERRIDES_STORE.exists() or not any(OVERRIDES_STORE.rglob("*")):
        return False
    shutil.copytree(OVERRIDES_STORE, dest, dirs_exist_ok=True)
    return True


# -------------------------
# PACK DETECTION
# -------------------------


def classify_mod_file(path: Path) -> str:
    """
    Determine whether a file belongs in 'mods', 'shaderpacks', or 'resourcepacks'.
    Only .zip files are inspected; all other extensions go to mods.
    Shaderpacks are identified by a shaders/ folder at the root of the zip.
    """
    if path.suffix.lower() != ".zip":
        return "mods"
    try:
        with zipfile.ZipFile(path, "r") as zf:
            member_names = zf.namelist()
        if any(name == "shaders/" or name.startswith("shaders/") for name in member_names):
            return "shaderpacks"
        return "resourcepacks"
    except zipfile.BadZipFile:
        return "mods"


def resolve_pack_dir(category: str) -> Path:
    """Return (and create if needed) the BUILD subdirectory for the given category."""
    pack_dir = BUILD / category
    pack_dir.mkdir(parents=True, exist_ok=True)
    return pack_dir


# -------------------------
# MOD INFO RESOLUTION (UNIFIED CACHE)
# -------------------------


def _update_file_data(entry: dict, project_id: str) -> None:
    """
    Fetch mod name and file names from the CF API and update entry in-place.
    Only resolves file_ids already present as keys in entry['files'] — ignores
    all other historical files in the response to keep the cache minimal.
    Silently returns on network failure, leaving the existing entry unchanged.
    """
    try:
        response = requests.get(CF_URL.format(project_id), headers=HEADERS, timeout=10)
        response.raise_for_status()
        data = response.json()
    except Exception:
        return  # Callers are responsible for handling missing data

    entry["name"] = data.get("title") or project_id

    if entry["files"]:
        # Build an id→name lookup then fill only the file_ids we pre-seeded
        api_names = {
            str(f["id"]): f.get("name", "")
            for f in data.get("files", [])
            if f.get("id")
        }
        for file_id in entry["files"]:
            if not entry["files"][file_id] and file_id in api_names:
                entry["files"][file_id] = api_names[file_id]


def _fetch_mod_data(project_id: str) -> dict:
    """
    Return cached CF data for a project, fetching from the network only if
    the mod name has not been resolved yet. Only writes the cache when a fetch
    was needed.
    """
    cache = load_json(CACHE, {})
    entry = cache.get(project_id) or {"name": project_id, "files": {}}

    if entry["name"] == project_id:
        _update_file_data(entry, project_id)
        cache[project_id] = entry
        save_json(CACHE, cache)

    return entry


def resolve_mod(project_id: str) -> str:
    """Return the human-readable mod name for a given project ID."""
    return _fetch_mod_data(project_id)["name"]


def resolve_file_name(project_id: str, file_id: str) -> str:
    """
    Return the filename string for a given project and file ID.
    Pre-seeds the file_id in the cache so only this specific file is fetched
    from the API rather than the full history. Falls back to the raw file_id
    string if the name cannot be determined.
    """
    file_id = str(file_id)
    cache   = load_json(CACHE, {})
    entry   = cache.get(project_id) or {"name": project_id, "files": {}}

    if not entry["files"].get(file_id):
        entry["files"][file_id] = ""  # pre-seed so _update_file_data resolves only this id
        _update_file_data(entry, project_id)
        cache[project_id] = entry
        save_json(CACHE, cache)

    return entry["files"].get(file_id) or file_id


def _prefetch_names(
    project_ids: set[str],
    file_lookups: dict[str, set[str]] | None = None,
) -> None:
    """
    Resolve mod names and file names for all given IDs in parallel, writing the
    cache exactly once. Call this before a loop of resolve_mod / resolve_file_name
    calls to replace N sequential HTTP requests with one parallel batch.
    """
    file_lookups = file_lookups or {}
    cache        = load_json(CACHE, {})
    to_fetch: list[tuple[dict, str]] = []

    for project_id in project_ids | set(file_lookups.keys()):
        entry = cache.get(project_id) or {"name": project_id, "files": {}}

        for file_id in {str(file_id) for file_id in file_lookups.get(project_id, set())}:
            if not entry["files"].get(file_id):
                entry["files"][file_id] = ""

        cache[project_id] = entry

        if entry["name"] == project_id or any(not v for v in entry["files"].values()):
            to_fetch.append((entry, project_id))

    if not to_fetch:
        return

    total_count = len(to_fetch)
    print(f"Resolving {total_count} mod(s) from API...")
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_project_id = {
            executor.submit(_update_file_data, entry, project_id): project_id
            for entry, project_id in to_fetch
        }
        for completed_count, future in enumerate(as_completed(future_to_project_id), 1):
            project_id = future_to_project_id[future]
            mod_name   = cache[project_id]["name"]
            print(f"  [{completed_count}/{total_count}] {mod_name}")

    save_json(CACHE, cache)


# -------------------------
# SNAPSHOTS
# -------------------------


def hash_state(mods: dict) -> str:
    """Return a 10-character SHA-1 hash of the sorted mod state, used as a commit ID."""
    return hashlib.sha1(
        json.dumps(mods, sort_keys=True).encode()
    ).hexdigest()[:10]


def save_snapshot(commit_id: str, mods: dict) -> None:
    """Persist the mod state dict for a given commit ID to disk."""
    save_json(SNAPSHOTS / f"{commit_id}.json", mods)


def load_snapshot(commit_id: str) -> dict:
    """Return the mod state dict for a given commit ID, or {} if not found."""
    return load_json(SNAPSHOTS / f"{commit_id}.json", {})


# -------------------------
# VERSION LOG
# -------------------------


def load_log() -> list[dict]:
    """Return the full version log as a list of entry dicts, oldest first."""
    return cast(list[dict], load_json(LOG_FILE, []))


def add_version(
    commit_id: str,
    version: str,
    added: int = 0,
    removed: int = 0,
    updated: int = 0,
    modloader: str = "",
    minecraft_version: str = "",
    message: str = "",
) -> None:
    """Append a new version entry to the log, including diff stats and optional modloader id."""
    log = load_log()
    entry: dict = {
        "commit":  commit_id,
        "version": version,
        "time":    time.time(),
        "added":   added,
        "removed": removed,
        "updated": updated,
    }
    if modloader:
        entry["modloader"] = modloader
    if minecraft_version:
        entry["minecraft_version"] = minecraft_version
    if message:
        entry["message"] = message
    log.append(entry)
    save_json(LOG_FILE, log)


def get_commit(version: str) -> str | None:
    """Return the commit ID for the given version string, or None if not found."""
    log_entry = get_log_entry(version)
    return log_entry["commit"] if log_entry else None


def get_log_entry(version: str) -> dict | None:
    """Return the full log entry dict for the given version string, or None if not found."""
    for entry in load_log():
        if str(entry["version"]) == str(version):
            return entry
    return None


# -------------------------
# DIFF
# -------------------------


def diff(old: dict, new: dict) -> dict:
    """
    Compute the difference between two mod state dicts.
    Returns a dict with keys 'added' (set), 'removed' (set), and
    'updated' (list of (project_id, old_file_id, new_file_id) tuples).
    Accepts both enriched {project_id: {file_id, ...}} dicts and legacy {project_id: file_id_str} dicts.
    """
    def file_id_of(value: dict | str) -> str:
        return str(value["file_id"] if isinstance(value, dict) else value)

    old = {str(k): file_id_of(v) for k, v in old.items()}
    new = {str(k): file_id_of(v) for k, v in new.items()}

    added   = new.keys() - old.keys()
    removed = old.keys() - new.keys()
    updated = [
        (project_id, old[project_id], new[project_id])
        for project_id in old.keys() & new.keys()
        if old[project_id] != new[project_id]
    ]
    return {"added": added, "removed": removed, "updated": updated}


# -------------------------
# VERSIONING
# -------------------------


def parse_version(version: str) -> list[int]:
    """Parse a version string into a [major, minor, patch] integer list."""
    parts = [int(segment) for segment in str(version).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return parts


def bump(version: str, changes: dict) -> str:
    """
    Increment version based on the nature of changes:
    - added or removed mods → bump minor, reset patch
    - updated mods only     → bump patch
    - no changes            → version unchanged
    """
    major, minor, patch = parse_version(version)
    if changes["added"] or changes["removed"]:
        minor += 1
        patch = 0
    elif changes["updated"]:
        patch += 1
    return f"{major}.{minor}.{patch}"


def bump_major(version: str) -> str:
    """Increment the major component and reset minor and patch to zero."""
    major, *_ = parse_version(version)
    return f"{major + 1}.0.0"


# -------------------------
# FILENAME GUESSING
# -------------------------


def guess_filename(response: requests.Response, project_id: str, file_id: str) -> str:
    """
    Derive a filename from the response's final URL after redirects.
    Falls back to '{project_id}-{file_id}.{ext}' if the URL path has no usable name.
    """
    url_path = urlparse(response.url).path
    filename = os.path.basename(unquote(url_path))

    if not filename or "." not in filename:
        content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
        ext = {
            "application/zip": ".zip",
            "application/java-archive": ".jar",
        }.get(content_type, ".jar")
        filename = f"{project_id}-{file_id}{ext}"

    return filename


# -------------------------
# DOWNLOAD  (with persistent cache)
# -------------------------


def _cached_jar_path(project_id: str, file_id: str) -> Path | None:
    """Return the DL_CACHE path for a (project_id, file_id) pair, or None if not cached."""
    DL_CACHE.mkdir(parents=True, exist_ok=True)
    for cached_path in DL_CACHE.glob(f"{project_id}_{file_id}_*"):
        return cached_path
    return None


def download_mod(project_id: str, file_id: str, force: bool = False) -> dict:
    """
    Ensure the given mod file is present in BUILD, using DL_CACHE to avoid
    redundant network requests. Only downloads when force=True or the file is
    not already in the persistent cache.

    Routes .zip files to shaderpacks/ or resourcepacks/ as appropriate.
    Returns a metadata dict with keys: project_id, file_id, file, cached, category.
    """
    cached_path = _cached_jar_path(project_id, file_id)

    if cached_path and not force:
        # Strip the '{project_id}_{file_id}_' prefix to recover the original filename
        filename = cached_path.name.split("_", 2)[2]
        category = classify_mod_file(cached_path)
        out_path = resolve_pack_dir(category) / filename
        if not out_path.exists():
            shutil.copy2(cached_path, out_path)
        return {
            "project_id": project_id, "file_id": file_id,
            "file": filename, "cached": True, "category": category,
        }

    url = f"https://www.curseforge.com/api/v1/mods/{project_id}/files/{file_id}/download"
    response = requests.get(url, stream=True, headers=HEADERS, allow_redirects=True)
    response.raise_for_status()

    filename   = guess_filename(response, project_id, file_id)
    cache_path = DL_CACHE / f"{project_id}_{file_id}_{filename}"

    with open(cache_path, "wb") as fh:
        for chunk in response.iter_content(8192):
            fh.write(chunk)

    category = classify_mod_file(cache_path)
    out_path = resolve_pack_dir(category) / filename
    shutil.copy2(cache_path, out_path)

    return {
        "project_id": project_id, "file_id": file_id,
        "file": filename, "cached": False, "category": category,
    }


# -------------------------
# README AUTO-UPDATE
# -------------------------


def _update_readme_modloader(old_modloader: str, new_modloader: str) -> bool:
    """
    Replace the modloader version string in README.md.
    On first use, replaces the __MODLOADER__ placeholder.
    On subsequent modloader changes, replaces the previous modloader string.
    Returns True if README.md was modified.
    """
    if not README.exists() or not new_modloader:
        return False
    content = README.read_text(encoding="utf-8")
    if "__MODLOADER__" in content:
        updated = content.replace("__MODLOADER__", new_modloader)
    elif old_modloader and old_modloader in content:
        updated = content.replace(old_modloader, new_modloader)
    else:
        return False
    README.write_text(updated, encoding="utf-8")
    return True


def init(source: str, force: bool = False) -> None:
    """
    Initialise a new repository from a CurseForge export zip, recording the
    first commit at version 1.0.0. With --force, wipes existing history while
    preserving the mod download cache.
    """
    validate_source(source)

    if REPO.exists() and not force:
        print("[ERROR] Repository already exists.")
        print("To reset it, run:  init <source> --force")
        return

    if REPO.exists() and force:
        print("[WARNING] --force: resetting repository (download cache is preserved).")
        for directory in (OVERRIDES_STORE, SNAPSHOTS):
            if directory.exists():
                shutil.rmtree(directory, ignore_errors=True)
        LOG_FILE.unlink(missing_ok=True)

    REPO.mkdir(parents=True, exist_ok=True)
    result = commit(source)
    if result:
        _, _, num_mods = result
        print(f"[OK] Repository initialized — {num_mods} mods tracked.")


# -------------------------
# COMMIT
# -------------------------


def commit(source: str, major: bool = False, message: str = "") -> tuple[str, str, int] | None:
    """
    Record a new version from an updated CurseForge export zip.

    Automatically bumps the major version when the modloader id changes between
    commits (e.g. a NeoForge update). The --major flag forces a major bump
    regardless. Returns (version, commit_id, mod_count), or None if nothing changed.
    """
    validate_source(source)

    if not REPO.exists():
        print("[ERROR] Repository not initialized. Run 'init' first.")
        sys.exit(1)

    manifest          = load_manifest(source)
    new_modloader     = get_modloader_version(manifest)
    minecraft_version = manifest.get("minecraft", {}).get("version", "")
    bare_mods = {
        str(mod["projectID"]): str(mod["fileID"])
        for mod in manifest.get("files", [])
    }
    commit_id = hash_state(bare_mods)

    log = load_log()

    for existing_entry in log:
        if existing_entry["commit"] == commit_id:
            print(f"[INFO] This zip matches an already-committed version ({existing_entry['version']}) — nothing to commit.")
            return None

    old_snapshot  = load_snapshot(log[-1]["commit"]) if log else {}
    old_version   = log[-1]["version"] if log else ""
    old_modloader = log[-1].get("modloader", "") if log else ""

    changes = diff(old_snapshot, bare_mods)

    # Only trigger auto-major if both old and new modloader strings are known;
    # avoids a false positive when the manifest lacks a modLoaders entry.
    modloader_changed = bool(
        old_version and old_modloader and new_modloader and old_modloader != new_modloader
    )

    if not old_version:
        version = "1.0.0"
    elif major or modloader_changed:
        version = bump_major(old_version)
    else:
        version = bump(old_version, changes)

    _prefetch_names({str(mod["projectID"]) for mod in manifest.get("files", [])})
    snapshot = build_snapshot(manifest, load_json(CACHE, {}))
    save_snapshot(commit_id, snapshot)
    add_version(
        commit_id, version,
        added=len(changes["added"]),
        removed=len(changes["removed"]),
        updated=len(changes["updated"]),
        modloader=new_modloader,
        minecraft_version=minecraft_version,
        message=message,
    )

    if not old_version:
        print(f"[OK] Committed {version} — initial release ({commit_id})")
    else:
        print(f"[OK] Committed {old_version} → {version} ({commit_id})")

    if modloader_changed:
        print(f"  [!] Modloader updated: {old_modloader} → {new_modloader}")
    if new_modloader and _update_readme_modloader(old_modloader, new_modloader):
        print(f"  [i] README.md modloader updated.")
    if changes["added"]:
        print(f"  [+] {len(changes['added'])} mod(s) added")
    if changes["removed"]:
        print(f"  [-] {len(changes['removed'])} mod(s) removed")
    if changes["updated"]:
        print(f"  [~] {len(changes['updated'])} mod(s) updated")
    if not changes["added"] and not changes["removed"] and not changes["updated"] and not modloader_changed:
        print("  (no mod changes)")

    override_count = store_overrides(source)
    if override_count:
        print(f"  {override_count} override file(s) stored.")

    return version, commit_id, len(snapshot)


# -------------------------
# CHANGELOG
# -------------------------


def changelog(
    v1: str,
    v2: str | None,
    out: str = "changelog.md",
    message: str = "",
    exclude: set[str] | None = None,
    exclude_categories: set[str] | None = None,
) -> None:
    """
    Generate a Markdown changelog between two committed versions and write it to a file.
    When v2 is None, v1 is treated as an initial release and diffed against an empty state.
    Includes a Modloader section when the modloader id changed between the two versions.
    An optional message is inserted as a short paragraph below the heading.
    exclude filters out specific project IDs; exclude_categories filters by mod category
    (e.g. 'shaderpacks', 'resourcepacks') using the stored mod index.
    """
    if v2 is None:
        v2 = v1
        v1 = "EMPTY"

    new_commit_id = get_commit(v2)
    if not new_commit_id:
        print(f"[ERROR] Version '{v2}' not found in log.")
        return

    new_entry     = get_log_entry(v2)
    new_modloader = new_entry.get("modloader", "") if new_entry else ""

    excluded_ids = {str(project_id) for project_id in exclude} if exclude else set()

    def apply_side_filter(snapshot: dict) -> dict:
        if not excluded_ids and not exclude_categories:
            return snapshot
        return {
            project_id: entry
            for project_id, entry in snapshot.items()
            if project_id not in excluded_ids
            and (
                not exclude_categories
                or ((entry.get("category", "") if isinstance(entry, dict) else "") or "mods")
                not in exclude_categories
            )
        }

    if v1 == "EMPTY":
        changes       = diff({}, apply_side_filter(load_snapshot(new_commit_id)))
        header_title  = f"# Changelog: {v2} (Initial Release)"
        old_modloader = ""
    else:
        old_commit_id = get_commit(v1)
        if not old_commit_id:
            print(f"[ERROR] Version '{v1}' not found in log.")
            return
        changes       = diff(
            apply_side_filter(load_snapshot(old_commit_id)),
            apply_side_filter(load_snapshot(new_commit_id)),
        )
        header_title  = f"# Changelog: {v1} → {v2}"
        old_entry     = get_log_entry(v1)
        old_modloader = old_entry.get("modloader", "") if old_entry else ""

    modloader_changed = bool(
        old_modloader and new_modloader and old_modloader != new_modloader
    )
    has_changes = changes["added"] or changes["removed"] or changes["updated"] or modloader_changed

    _prefetch_names(
        project_ids={str(project_id) for project_id in changes["added"] | changes["removed"]}
                   | {project_id for project_id, _, _ in changes["updated"]},
        file_lookups={
            project_id: {old_file_id, new_file_id}
            for project_id, old_file_id, new_file_id in changes["updated"]
        },
    )

    if changes["added"] or changes["removed"] or changes["updated"]:
        print("Reading from mod cache...")

    lines = [header_title, ""]
    if message:
        lines += [message, ""]

    # --- Modloader ---
    if modloader_changed:
        lines.append("## 🔧 Modloader")
        lines.append(f"- Updated: _{old_modloader}_ → _{new_modloader}_")
        lines.append("")
    elif v1 == "EMPTY" and new_modloader:
        # Show the starting modloader on initial release for reference
        lines.append("## 🔧 Modloader")
        lines.append(f"- {new_modloader}")
        lines.append("")

    # --- Added ---
    lines.append("## ➕ Added")
    if changes["added"]:
        added_list = sorted(changes["added"])
        for index, project_id in enumerate(added_list, 1):
            mod_name = resolve_mod(project_id)
            print(f"  [+] {mod_name} ({index}/{len(added_list)})")
            lines.append(f"- {mod_name}")
    else:
        lines.append("_No mods added._")
    lines.append("")

    # --- Removed ---
    lines.append("## ➖ Removed")
    if changes["removed"]:
        removed_list = sorted(changes["removed"])
        for index, project_id in enumerate(removed_list, 1):
            mod_name = resolve_mod(project_id)
            print(f"  [-] {mod_name} ({index}/{len(removed_list)})")
            lines.append(f"- {mod_name}")
    else:
        lines.append("_No mods removed._")
    lines.append("")

    # --- Updated ---
    lines.append("## 🔄 Updated")
    if changes["updated"]:
        updated_list = sorted(changes["updated"])
        for index, (project_id, *_) in enumerate(updated_list, 1):
            mod_name = resolve_mod(project_id)
            print(f"  [~] {mod_name} ({index}/{len(updated_list)})")
            lines.append(f"- {mod_name}")
    else:
        lines.append("_No mods updated._")

    if not has_changes:
        lines.append("")
        lines.append("> ⚠️ No differences found between these two versions.")

    text = "\n".join(lines)
    print("\n" + text)

    out_path = Path(out)
    out_path.write_text(text, encoding="utf-8")
    print(f"\n[OK] Changelog written to {out_path}")


# -------------------------
# UPDATE  (clear build, rebuild from cache)
# -------------------------


def update(
    version: str,
    exclude: set[str] | None = None,
    exclude_categories: set[str] | None = None,
    suffix: str = "",
) -> dict:
    """
    Clear the build directory and rebuild it cleanly for the given version.

    Every mod in the snapshot is copied from DL_CACHE if present, otherwise
    downloaded from CurseForge and cached for future use. Excluded project IDs
    are skipped entirely (used to produce client-only or server-only builds).
    Mods whose category (from the existing index) is in exclude_categories are
    also skipped — used to drop shaderpacks and resourcepacks from server builds.

    suffix is a display label ('client' or 'server') appended to output messages.
    Returns a stats dict: { downloaded, cached, failed, ok }.
    """
    excluded_ids = {str(project_id) for project_id in exclude} if exclude else set()
    commit_id    = get_commit(version)
    if not commit_id:
        print(f"[ERROR] Version '{version}' not found.")
        return {"downloaded": 0, "cached": 0, "failed": 0, "ok": 0}

    snapshot = load_snapshot(commit_id)

    def entry_file_id(entry: dict | str) -> str:
        return str(entry["file_id"] if isinstance(entry, dict) else entry)

    def entry_category(entry: dict | str) -> str:
        return (entry.get("category", "") if isinstance(entry, dict) else "") or "mods"

    mods_to_build = {
        project_id: entry_file_id(entry)
        for project_id, entry in snapshot.items()
        if project_id not in excluded_ids
        and (
            not exclude_categories
            or entry_category(entry) not in exclude_categories
        )
    }
    label = f"v{version}-{suffix}" if suffix else f"v{version}"

    print(f"Building {label} ({len(mods_to_build)} mods)...\n")

    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True, exist_ok=True)

    downloaded = cached = failed = 0
    total_count = len(mods_to_build)
    completed_count = 0
    successful_results: list[dict] = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_project_id = {
            executor.submit(download_mod, project_id, file_id): project_id
            for project_id, file_id in mods_to_build.items()
        }
        for future in as_completed(future_to_project_id):
            project_id = future_to_project_id[future]
            completed_count += 1
            try:
                result = future.result()
                if exclude_categories and result["category"] in exclude_categories:
                    (BUILD / result["category"] / result["file"]).unlink(missing_ok=True)
                    print(f"  [~] [skip] [{completed_count}/{total_count}] {result['file']} (not needed for server)")
                    continue
                source_tag = "cache" if result["cached"] else "fetch"
                if result["cached"]:
                    cached += 1
                else:
                    downloaded += 1
                successful_results.append(result)
                print(f"  [+] [{source_tag}] [{completed_count}/{total_count}] {result['file']}")
            except Exception as exc:
                failed += 1
                print(f"  [WARN] [{completed_count}/{total_count}] Failed to get {project_id}: {exc}")

    # Fill file and category into the snapshot for each successfully built mod.
    # Excluded and failed mods keep their existing snapshot entry unchanged.
    if successful_results:
        cache_data = load_json(CACHE, {})
        for result in successful_results:
            project_id = result["project_id"]
            existing = snapshot.get(project_id)
            if isinstance(existing, dict):
                existing["file"]     = result["file"]
                existing["category"] = result["category"]
            else:
                snapshot[project_id] = {
                    "file_id":  result["file_id"],
                    "name":     cache_data.get(project_id, {}).get("name") or project_id,
                    "file":     result["file"],
                    "category": result["category"],
                }
        save_snapshot(commit_id, snapshot)

    (BUILD / "modpack_version.txt").write_text(version)

    ok      = downloaded + cached
    summary = f"{ok} mods: {downloaded} downloaded, {cached} from cache"
    if failed:
        summary += f", {failed} failed"
    print(f"\n[OK] Updated to {label}  ({summary})")

    return {"downloaded": downloaded, "cached": cached, "failed": failed, "ok": ok}


# -------------------------
# RELEASE  (delegates to update, then zips)
# -------------------------


def release(
    version: str,
    exclude: set[str] | None = None,
    exclude_categories: set[str] | None = None,
    suffix: str = "",
) -> Path | None:
    """
    Build a distributable release zip for the given version.

    Calls update() to produce a clean build directory, applies any stored
    overrides on top, then zips the result into releases/. Overrides are applied
    after update() because update() clears BUILD first.

    Returns the Path to the created zip, or None if the build failed.
    """
    commit_id = get_commit(version)
    if not commit_id:
        print(f"[ERROR] Version '{version}' not found in log.")
        return None

    stats = update(version, exclude=exclude, exclude_categories=exclude_categories, suffix=suffix)

    if stats["failed"] != 0:
        print("[ERROR] Release aborted: not all mods could be fetched.")
        return None

    overrides_included = apply_overrides(BUILD)
    if overrides_included:
        print("Overrides applied from repo.")
    else:
        print("[INFO] No stored overrides found — skipping.")

    if not any(BUILD.rglob("*")):
        print("[ERROR] Release aborted: build folder is empty.")
        return None

    RELEASES.mkdir(parents=True, exist_ok=True)
    prefix      = get_file_prefix()
    zip_name    = f"{prefix}-{version}-{suffix}.zip" if suffix else f"{prefix}-{version}.zip"
    zip_path    = RELEASES / zip_name

    print(f"\nBuilding release zip at {zip_path}...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(BUILD):
            for filename in files:
                file_path = Path(root) / filename
                zf.write(file_path, file_path.relative_to(BUILD))

    snapshot       = load_snapshot(commit_id)
    excluded_count = len(exclude) if exclude else 0
    label          = f"v{version}-{suffix}" if suffix else f"v{version}"
    print(f"\n{'=' * 36}")
    print(f" RELEASE REPORT — {label}")
    print(f"{'=' * 36}")
    print(f"  Mods in snapshot : {len(snapshot)}")
    print(f"  Excluded         : {excluded_count}")
    print(f"  Downloaded       : {stats['downloaded']}")
    print(f"  From cache       : {stats['cached']}")
    print(f"  Failed           : {stats['failed']}")
    print(f"  Overrides        : {'yes' if overrides_included else 'no'}")
    print(f"  Output           : {zip_name}")
    print(f"{'=' * 36}\n")
    print(f"[OK] Built {zip_name}")

    return zip_path


def release_client(version: str) -> Path | None:
    """Build a client release zip and CurseForge export zip, excluding server_only mods, and bake client-updater-template.py."""
    print(f"Building client release for v{version}...")
    excluded = get_filter_list("server_only")
    if not excluded:
        print("[WARN] No server_only list found in config — building full release.")
    else:
        print(f"[INFO] Excluding {len(excluded)} server-only mod(s).")
    zip_path = release(version, exclude=excluded, suffix="client")
    if zip_path:
        if bake_client_updater():
            _build_exe(_baked_client_updater_path())
        export_cf(version)
    return zip_path


def release_server(version: str) -> Path | None:
    """Build a server release zip, excluding client-only mods, shaderpacks, and resourcepacks, and bake server-updater-template.py."""
    excluded = get_filter_list("client_only")
    if not excluded:
        print("[WARN] No client_only list found in config — building full release.")
    else:
        print(f"[INFO] Excluding {len(excluded)} client-only mod(s).")
    zip_path = release(version, exclude=excluded, exclude_categories={"shaderpacks", "resourcepacks"}, suffix="server")
    if zip_path:
        bake_server_updater()
    return zip_path


def export_cf(version: str) -> Path | None:
    """Build a CurseForge-format modpack zip for the given committed version."""
    log_entry = get_log_entry(version)
    if not log_entry:
        print(f"[ERROR] Version {version} not found in log.")
        return None

    commit_id         = log_entry["commit"]
    snapshot          = load_snapshot(commit_id)
    cfg               = load_config()
    settings          = cfg.get("settings", {})

    modpack_name      = settings.get("modpack_name", "")
    author            = settings.get("author", "")
    logo_url          = settings.get("logo_url", "")
    recommended_ram   = settings.get("recommended_ram", None)
    modloader_id      = log_entry.get("modloader", "")
    minecraft_version = log_entry.get("minecraft_version", "")

    server_only_ids = get_filter_list("server_only")
    client_snapshot = {
        project_id: entry
        for project_id, entry in snapshot.items()
        if project_id not in server_only_ids
    }
    excluded_count = len(snapshot) - len(client_snapshot)

    print(f"Building CurseForge export for v{version}...")
    if not minecraft_version:
        print("[WARN] No minecraft_version recorded for this version — manifest minecraft.version will be empty.")
    if not modloader_id:
        print("[WARN] No modloader recorded for this version — manifest will have no modLoaders entry.")
    if excluded_count:
        print(f"  Excluding {excluded_count} server-only mod(s).")

    # Build manifest.json
    modloaders: list[dict] = [{"id": modloader_id, "primary": True}] if modloader_id else []
    minecraft_block: dict  = {"version": minecraft_version, "modLoaders": modloaders}
    if recommended_ram is not None:
        minecraft_block["recommendedRam"] = int(recommended_ram)

    files = [
        {"projectID": int(project_id), "fileID": int(entry["file_id"]), "required": True, "isLocked": False}
        for project_id, entry in client_snapshot.items()
    ]

    manifest: dict = {
        "minecraft":       minecraft_block,
        "manifestType":    "minecraftModpack",
        "manifestVersion": 1,
        "name":            modpack_name,
        "version":         version,
        "author":          author,
        "files":           files,
        "overrides":       "overrides",
    }
    if logo_url:
        manifest["image"] = logo_url

    # Build modlist.html
    modlist_rows = []
    for project_id, entry in sorted(client_snapshot.items(), key=lambda item: item[1].get("name", item[0]).lower()):
        name = entry.get("name") or f"Project {project_id}"
        url  = f"https://www.curseforge.com/projects/{project_id}"
        modlist_rows.append(f'  <li><a href="{url}">{name}</a></li>')
    modlist_html = "<ul>\n" + "\n".join(modlist_rows) + "\n</ul>\n"

    # Prepare bcc-common.toml content (update version/name in-place if stored, else create)
    bcc_rel   = Path("config") / "bcc-common.toml"
    bcc_src   = OVERRIDES_STORE / bcc_rel
    bcc_v_re  = re.compile(r'^([ \t]*modpackVersion\s*=\s*)"[^"]*"', re.MULTILINE)
    bcc_n_re  = re.compile(r'^([ \t]*modpackName\s*=\s*)"[^"]*"',    re.MULTILINE)
    if bcc_src.exists():
        bcc_text = bcc_src.read_text(encoding="utf-8")
        bcc_text = bcc_v_re.sub(rf'\g<1>"{version}"',      bcc_text)
        bcc_text = bcc_n_re.sub(rf'\g<1>"{modpack_name}"', bcc_text)
        print(f"  bcc-common.toml version stamped → {version}")
    else:
        bcc_text = (
            "#General settings\n[general]\n"
            f'\tmodpackName = "{modpack_name}"\n'
            f'\tmodpackVersion = "{version}"\n'
            "\tuseMetadata = false\n"
        )
        print("  bcc-common.toml not found in overrides — creating default.")

    # Write zip
    RELEASES.mkdir(parents=True, exist_ok=True)
    zip_path = RELEASES / f"{get_file_prefix()}-{version}-curseforge.zip"
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            zf.writestr("modlist.html",  modlist_html)
            override_file_count = 0
            if OVERRIDES_STORE.is_dir():
                for src_file in OVERRIDES_STORE.rglob("*"):
                    if not src_file.is_file():
                        continue
                    rel = src_file.relative_to(OVERRIDES_STORE)
                    if rel == bcc_rel:
                        continue  # written separately below with updated version
                    zf.write(src_file, f"overrides/{rel.as_posix()}")
                    override_file_count += 1
            zf.writestr(f"overrides/{bcc_rel.as_posix()}", bcc_text)
    except Exception as exc:
        print(f"[ERROR] Failed to write CurseForge zip: {exc}")
        return None

    print(f"  {len(files)} mods, {override_file_count} override file(s)")
    print(f"[OK] Exported {zip_path}")
    return zip_path


# -------------------------
# PUBLISH
# -------------------------


def set_version_message(version: str, message: str) -> None:
    """Store or clear a release message on the log entry for the given version."""
    log = load_log()
    for entry in log:
        if str(entry["version"]) == str(version):
            if message:
                entry["message"] = message
            else:
                entry.pop("message", None)
            break
    save_json(LOG_FILE, log)


def _build_versions_json() -> dict:
    """Build the versions.json payload served from gh-pages for the player updater."""
    log = load_log()
    versions = []
    for entry in log:
        version_entry: dict = {"version": entry["version"], "commit": entry["commit"], "time": entry["time"]}
        if entry.get("message"):
            version_entry["message"] = entry["message"]
        if entry.get("modloader"):
            version_entry["modloader"] = entry["modloader"]
        versions.append(version_entry)
    client_only_ids = sorted(get_filter_list("client_only"))
    server_only_ids = sorted(get_filter_list("server_only"))
    payload: dict = {"latest": log[-1]["version"] if log else None, "versions": versions}
    if client_only_ids:
        payload["client_only_ids"] = client_only_ids
    if server_only_ids:
        payload["server_only_ids"] = server_only_ids
    return payload


def _get_notes_file_for_release(version: str, message: str = "", side: str = "") -> Path:
    """
    Generate a temporary Markdown changelog file for the given version.
    Diffs against the previous version if one exists, otherwise treats it as an initial release.
    side='client' excludes server_only mods; side='server' excludes client_only mods and
    non-mod categories. The caller is responsible for deleting this file after use.
    """
    release_exclude: set[str] | None = None
    release_exclude_categories: set[str] | None = None
    if side == "client":
        release_exclude = get_filter_list("server_only")
    elif side == "server":
        release_exclude = get_filter_list("client_only")
        release_exclude_categories = {"shaderpacks", "resourcepacks"}

    log          = load_log()
    prev_version = None
    for index in range(len(log) - 1, -1, -1):
        if str(log[index]["version"]) == str(version):
            prev_version = log[index - 1]["version"] if index > 0 else None
            break

    notes_fd, notes_str = tempfile.mkstemp(suffix=".md", prefix=f"modpackctl_notes_{version}_")
    os.close(notes_fd)
    notes_path = Path(notes_str)
    if prev_version:
        print(f"Generating notes comparing {prev_version} → {version}...")
        changelog(prev_version, version, out=str(notes_path), message=message,
                  exclude=release_exclude, exclude_categories=release_exclude_categories)
    else:
        changelog(version, None, out=str(notes_path), message=message,
                  exclude=release_exclude, exclude_categories=release_exclude_categories)

    return notes_path


def _write_pages_assets(dest: Path) -> None:
    """
    Write versions.json and the snapshots/ tree into dest.
    Skips snapshots that already exist in dest (snapshots are immutable per commit).
    """
    versions_payload = _build_versions_json()
    (dest / "versions.json").write_text(json.dumps(versions_payload, indent=2))

    snapshots_dir = dest / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    for log_entry in load_log():
        commit_id    = log_entry["commit"]
        snapshot_out = snapshots_dir / f"{commit_id}.json"
        if snapshot_out.exists():
            continue
        snapshot_data = load_snapshot(commit_id)
        snapshot_out.write_text(json.dumps(snapshot_data, indent=2))


def _push_pages_assets() -> None:
    """
    Push versions.json and snapshots to the gh-pages branch, creating
    the branch as an orphan if it does not yet exist. Uses a temporary git
    worktree to avoid switching the working branch.
    """
    print("Pushing versions.json + snapshots to gh-pages...")
    try:
        ls_result     = _run(
            ["git", "ls-remote", "--heads", "origin", "gh-pages"],
            capture_output=True, text=True, check=True,
        )
        branch_exists = "gh-pages" in ls_result.stdout

        if not branch_exists:
            print("[INFO] Creating gh-pages branch...")
            _run(["git", "checkout", "--orphan", "gh-pages"], check=True)
            _run(["git", "rm", "-rf", "--cached", "."], check=True, capture_output=True)
            _write_pages_assets(Path("."))
            _run(["git", "add", "versions.json", "snapshots"], check=True)
            _run(["git", "commit", "-m", "init: versions.json + snapshots"], check=True)
            _run(["git", "push", "-u", "origin", "gh-pages"], check=True)
            _run(["git", "checkout", "-"], check=True)
        else:
            _run(["git", "worktree", "prune"], capture_output=True)

            worktree_path = Path(".gh-pages-worktree")
            if worktree_path.exists():
                try:
                    _run(
                        ["git", "worktree", "remove", "--force", str(worktree_path)],
                        capture_output=True,
                    )
                except Exception:
                    pass
                if worktree_path.exists():
                    shutil.rmtree(worktree_path, ignore_errors=True)

            # Clean up any leftover temp branch from a previous failed run
            _run(["git", "branch", "-D", "gh-pages-temp"], capture_output=True)
            _run(["git", "fetch", "origin", "gh-pages"], check=True)
            _run(
                ["git", "worktree", "add", "-b", "gh-pages-temp",
                 str(worktree_path), "origin/gh-pages"],
                check=True, capture_output=True,
            )

            _write_pages_assets(worktree_path)
            _run(["git", "add", "versions.json", "snapshots"], check=True, cwd=worktree_path)

            try:
                _run(
                    ["git", "commit", "-m", "chore: update versions.json + snapshots"],
                    check=True, capture_output=True, text=True, cwd=worktree_path,
                )
                _run(
                    ["git", "push", "origin", "HEAD:gh-pages"],
                    check=True, cwd=worktree_path,
                )
                print("[INFO] versions.json + snapshots updated on gh-pages.")
            except subprocess.CalledProcessError as exc:
                if "nothing to commit" in (exc.stdout or "") or "nothing to commit" in (exc.stderr or ""):
                    print("[INFO] versions.json + snapshots are already up to date.")
                else:
                    raise

            _run(
                ["git", "worktree", "remove", "--force", str(worktree_path)], check=True
            )
            _run(["git", "branch", "-D", "gh-pages-temp"], capture_output=True)

        print("[OK] versions.json + snapshots pushed to gh-pages.")
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] Git operation failed: {exc}")
        if exc.stderr:
            print(f"Details: {exc.stderr.strip()}")
        print("       Make sure git is installed and you have push access to the repo.")
        raise


def _has_client_changes(version: str) -> bool:
    """
    Return True if the given version has any client-visible changes compared to
    the previous version. Server-only mods (from the server_only config list) are
    excluded from the comparison. An initial release (no previous version) always
    returns True.
    """
    log = load_log()
    new_entry    = None
    prev_version = None
    for index in range(len(log) - 1, -1, -1):
        if str(log[index]["version"]) == str(version):
            new_entry    = log[index]
            prev_version = log[index - 1]["version"] if index > 0 else None
            break

    if new_entry is None or prev_version is None:
        return True  # initial release always counts as a change

    prev_entry    = get_log_entry(prev_version)
    old_modloader = prev_entry.get("modloader", "") if prev_entry else ""
    new_modloader = new_entry.get("modloader", "")
    modloader_changed = bool(old_modloader and new_modloader and old_modloader != new_modloader)
    if modloader_changed:
        return True

    server_only_ids = get_filter_list("server_only")

    def apply_client_filter(snapshot: dict) -> dict:
        if not server_only_ids:
            return snapshot
        return {
            project_id: value
            for project_id, value in snapshot.items()
            if project_id not in server_only_ids
        }

    old_commit_id = get_commit(prev_version)
    new_commit_id = new_entry["commit"]
    if not old_commit_id:
        return True

    changes = diff(
        apply_client_filter(load_snapshot(old_commit_id)),
        apply_client_filter(load_snapshot(new_commit_id)),
    )
    return bool(changes["added"] or changes["removed"] or changes["updated"])


def _prepare_icon() -> Path | None:
    """
    Download the modpack logo from logo_url and convert it to a .ico file.
    Returns the .ico path on success, or None if logo_url is unset or conversion fails.
    """
    import io
    import urllib.request
    cfg      = load_config()
    logo_url = cfg.get("settings", {}).get("logo_url", "")
    if not logo_url:
        return None
    try:
        from PIL import Image  # type: ignore[import]
        ico_path = PYINSTALLER / "icon.ico"
        ico_path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(logo_url, timeout=10) as response:
            image_data = response.read()
        img = Image.open(io.BytesIO(image_data)).convert("RGBA")
        # Pad to square then upscale to 256×256 so all ICO sizes are clean downscales
        side = max(img.width, img.height, 256)
        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        square.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
        square = square.resize((256, 256), Image.Resampling.LANCZOS)
        square.save(str(ico_path), format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        print(f"[OK] Icon prepared from {logo_url}")
        return ico_path
    except Exception as exc:
        print(f"[WARN] Could not prepare icon — exe will use the default: {exc}")
        return None


def _prepare_dance_assets() -> tuple[Path, Path] | None:
    """
    Download the dance video and extract its audio into .pyinstaller/dance/ for bundling.
    Returns (video_path, audio_path) on success, or None if unavailable.
    """
    cfg        = load_config()
    dance_url  = cfg.get("settings", {}).get("secret_video_url", _DANCE_DEFAULT_URL)
    dance_dir  = PYINSTALLER / "dance"
    dance_dir.mkdir(parents=True, exist_ok=True)
    video_path = dance_dir / "dance_video.mp4"
    audio_path = dance_dir / "dance_audio.wav"
    url_record = dance_dir / "url.txt"

    try:
        import yt_dlp as ydl_module
    except ImportError:
        print("[WARN] yt-dlp not installed — dance assets will not be bundled (players will download at runtime).")
        return None

    cached_url = url_record.read_text(encoding="utf-8").strip() if url_record.exists() else ""
    if cached_url != dance_url and video_path.exists():
        video_path.unlink()
        if audio_path.exists():
            audio_path.unlink()

    if not video_path.exists():
        print("Downloading dance video for bundling...")
        ydl_opts = {
            "format": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]/mp4/best",
            "outtmpl": str(video_path),
            "merge_output_format": "mp4",
            "noplaylist": True,
            "no_warnings": True,
        }
        with ydl_module.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
            ydl.download([dance_url])
        if video_path.exists():
            url_record.write_text(dance_url, encoding="utf-8")

    if not video_path.exists():
        print("[WARN] Dance video download failed — not bundling.")
        return None

    if not audio_path.exists():
        print("Extracting dance audio for bundling...")
        try:
            import imageio_ffmpeg  # type: ignore[import-untyped]
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            subprocess.run(
                [ffmpeg_exe, "-i", str(video_path),
                 "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
                 str(audio_path), "-y"],
                check=True, capture_output=True,
            )
        except Exception as exc:
            print(f"[WARN] Could not extract dance audio — not bundling: {exc}")
            return None

    print("[OK] Dance assets ready for bundling.")
    return video_path, audio_path


def _build_exe(source_py: Path) -> Path | None:
    """
    Build a standalone Windows exe from source_py using PyInstaller.
    Prints its own progress, success, and warning messages.
    Returns the exe path on success, or None if PyInstaller is unavailable or fails.
    """
    exe_path = source_py.parent / (source_py.stem + ".exe")
    print(f"Building {exe_path.name}...")
    icon_path   = _prepare_icon()
    icon_args   = ["--icon", str(icon_path.resolve())] if icon_path else []
    cfg         = load_config()
    enable_secret = cfg.get("settings", {}).get("enable_secret", True)
    dance_paths = _prepare_dance_assets() if enable_secret else None
    dance_args: list[str] = []
    if dance_paths:
        video_file, audio_file = dance_paths
        dance_args = [
            "--add-data", f"{video_file.resolve()};dance",
            "--add-data", f"{audio_file.resolve()};dance",
        ]
    try:
        _run(
            [
                sys.executable, "-m", "PyInstaller",
                "--onefile", "--windowed",
                "--name", source_py.stem,
                *icon_args,
                *dance_args,
                "--collect-all", "yt_dlp",
                "--collect-all", "imageio",
                "--collect-all", "imageio_ffmpeg",
                "--collect-all", "PIL",
                "--distpath", str(source_py.parent),
                "--workpath", str(PYINSTALLER / "work"),
                "--specpath", str(PYINSTALLER),
                str(source_py),
            ],
            check=True,
        )
    except FileNotFoundError:
        print("[WARN] PyInstaller not found — exe not built.")
        print("       Install build deps: pip install pyinstaller yt-dlp imageio-ffmpeg Pillow")
        return None
    except subprocess.CalledProcessError:
        print("[WARN] PyInstaller build failed — exe not built.")
        print("       Install build deps: pip install pyinstaller yt-dlp imageio-ffmpeg Pillow")
        return None
    if not exe_path.exists():
        print("[WARN] PyInstaller finished but exe was not produced.")
        return None
    print(f"[OK] Built {exe_path}")
    if icon_path:
        _clear_icon_cache()
    return exe_path


def _clear_icon_cache() -> None:
    """Delete Windows icon cache DB files and restart Explorer so the new icon shows immediately."""
    import glob
    answer = input("Clear Windows icon cache so the new icon appears immediately? (Explorer will restart) [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("[INFO] Skipped icon cache clear — the new icon may not appear.")
        return
    cache_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Explorer"
    db_files  = glob.glob(str(cache_dir / "iconcache_*.db"))
    subprocess.run(["taskkill", "/f", "/im", "explorer.exe"], capture_output=True)
    for db_file in db_files:
        try:
            os.remove(db_file)
        except OSError:
            pass
    subprocess.Popen(["explorer.exe"])
    print("[OK] Icon cache cleared — new icon will now appear.")


def build_exe() -> None:
    """Build the baked updater exe from releases/{file_prefix}-updater.py."""
    if not bake_client_updater():
        sys.exit(1)
    if not _build_exe(_baked_client_updater_path()):
        sys.exit(1)


def publish(version: str, message: str = "") -> None:
    """
    Build a fresh client release zip and CurseForge export zip, create a GitHub Release
    with the generated changelog as release notes, and push updated versions.json to gh-pages.
    An optional message is included at the top of the release notes.
    Aborts if the version has no client-visible changes (e.g. only server-only mods changed).
    """
    if not REPO.exists():
        print("[ERROR] Repository not initialized. Run 'init' first.")
        sys.exit(1)

    user, repo = get_github_info()

    log_entry = get_log_entry(version)
    if not log_entry:
        print(f"[ERROR] Version '{version}' not found in log.")
        sys.exit(1)

    if not _has_client_changes(version):
        print(f"[ERROR] v{version} has no client-visible changes — nothing to publish.")
        print("        All changes in this version are server-only mods.")
        print("        Use 'release --server' if you need a server-side release.")
        sys.exit(1)

    if not message:
        message = log_entry.get("message", "")
    elif message != log_entry.get("message", ""):
        set_version_message(version, message)

    zip_path = release_client(version)
    if not zip_path or not zip_path.exists():
        print("[ERROR] Release build failed — cannot publish.")
        sys.exit(1)

    notes_path = _get_notes_file_for_release(version, message=message, side="client")
    tag        = f"v{version}"

    baked_updater_path = _baked_client_updater_path()
    baked_exe_path     = baked_updater_path.with_suffix(".exe")

    cf_zip_path = RELEASES / f"{get_file_prefix()}-{version}-curseforge.zip"

    release_assets = [str(zip_path)]
    if cf_zip_path.exists():
        release_assets.append(str(cf_zip_path))
    else:
        print(f"[WARN] {cf_zip_path.name} not found — not uploading CurseForge zip.")
    if baked_updater_path.exists():
        release_assets.append(str(baked_updater_path))
    else:
        print(f"[WARN] {baked_updater_path.name} not found — not uploading updater.")
    if baked_exe_path.exists():
        release_assets.append(str(baked_exe_path))

    print(f"Creating GitHub Release {tag}...")
    release_ok = False
    try:
        _run(
            [
                "gh", "release", "create", tag,
                *release_assets,
                "--title", f"v{version}",
                "--notes-file", str(notes_path),
                "--repo", f"{user}/{repo}",
            ],
            check=True,
        )
        print(f"[OK] GitHub Release {tag} created.")
        release_ok = True
    except subprocess.CalledProcessError:
        print("[ERROR] 'gh release create' failed.")
        print("        Make sure the GitHub CLI is installed: https://cli.github.com")
        print("        And that you're authenticated: gh auth login")
    finally:
        notes_path.unlink(missing_ok=True)

    pages_ok = True
    try:
        _push_pages_assets()
    except Exception:
        pages_ok = False
        print("[WARN] Could not update gh-pages. Players won't see this version in the updater.")
        print("       Run 'python modpackctl.py build-pages' to generate the files locally,")
        print("       then push them to the gh-pages branch manually.")

    pages_url   = f"https://{user}.github.io/{repo}/"
    release_url = f"https://github.com/{user}/{repo}/releases/tag/{tag}"

    print(f"\n{'=' * 42}")
    if release_ok and pages_ok:
        print(f" PUBLISH COMPLETE — v{version}")
    else:
        print(f" PUBLISH PARTIAL — v{version} (see errors above)")
    print(f"{'=' * 42}")
    if release_ok:
        print(f"  Release URL : {release_url}")
    print(f"  gh-pages    : {pages_url}")
    print(f"{'=' * 42}\n")
    print("     New players: download the zip from the release page.")
    print("     Existing players: run client-updater.py from their current install.")


def build_pages() -> None:
    """Write versions.json and snapshots/ to a local gh-pages/ folder for manual publishing."""
    if not REPO.exists():
        print("[ERROR] Repository not initialized. Run 'init' first.")
        sys.exit(1)
    dest = PAGES_OUTPUT
    dest.mkdir(parents=True, exist_ok=True)
    _write_pages_assets(dest)
    print(f"[OK] Built gh-pages assets → {dest}/")
    print("     Push the contents of this folder to your gh-pages branch.")


def _ensure_template(template_path: Path) -> None:
    """Copy a bundled template to the CWD if it does not already exist there."""
    if template_path.exists():
        print(f"[INFO] Using existing {template_path.name}")
    else:
        source = _HERE / template_path.name
        shutil.copy2(source, template_path)
        print(f"[INFO] Copied {template_path.name} to current directory — you can customise it for this modpack.")


def bake_client_updater() -> bool:
    """
    Substitute config placeholders in client-updater-template.py and write the result to
    releases/{file_prefix}-updater.py. Returns False if client-updater-template.py is not present.

    Supported placeholders (written as bare Python string literals in client-updater-template.py):
      __GITHUB_USER__      — GitHub username from modpackctl.toml
      __GITHUB_REPO__      — GitHub repo name from modpackctl.toml
      __MODPACK_NAME__     — settings.modpack_name from modpackctl.toml
      __LOGO_URL__         — logo URL from modpackctl.toml (empty string if unset)
      __SECRET_VIDEO_URL__ — easter egg video URL (defaults to Never Gonna Give You Up)
      __ENABLE_RAINBOW__   — True/False; settings.enable_rainbow from modpackctl.toml (default: True)
    """
    _ensure_template(CLIENT_UPDATE_SCRIPT)
    if not CLIENT_UPDATE_SCRIPT.exists():
        print(f"[WARN] {CLIENT_UPDATE_SCRIPT} not found — skipping updater bake.")
        return False
    user, repo        = get_github_info()
    cfg               = load_config()
    settings          = cfg.get("settings", {})
    modpack_name      = settings.get("modpack_name", "")
    logo_url          = settings.get("logo_url", "")
    secret_video_url  = settings.get("secret_video_url", _DANCE_DEFAULT_URL)
    enable_secret     = settings.get("enable_secret",  True)
    enable_rainbow    = settings.get("enable_rainbow",  False)
    content = CLIENT_UPDATE_SCRIPT.read_text(encoding="utf-8")
    content = content.replace('"__GITHUB_USER__"',      f'"{user}"')
    content = content.replace('"__GITHUB_REPO__"',      f'"{repo}"')
    content = content.replace('"__MODPACK_NAME__"',     f'"{modpack_name}"')
    content = content.replace('"__LOGO_URL__"',         f'"{logo_url}"')
    content = content.replace('"__SECRET_VIDEO_URL__"', f'"{secret_video_url}"')
    content = content.replace('"__ENABLE_SECRET__"',    str(bool(enable_secret)))
    content = content.replace('"__ENABLE_RAINBOW__"',   str(bool(enable_rainbow)))
    dest_path = _baked_client_updater_path()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(content, encoding="utf-8")
    print(f"[OK] Baked {CLIENT_UPDATE_SCRIPT.name} → {dest_path}")
    return True


def bake_server_updater() -> bool:
    """
    Substitute config placeholders in server-updater-template.py and write the result to
    releases/{file_prefix}-server-updater.py. Returns False if the template is not present.

    Supported placeholders:
      __GITHUB_USER__  — GitHub username from modpackctl.toml
      __GITHUB_REPO__  — GitHub repo name from modpackctl.toml
      __MODPACK_NAME__ — settings.modpack_name from modpackctl.toml
    """
    _ensure_template(SERVER_UPDATE_SCRIPT)
    if not SERVER_UPDATE_SCRIPT.exists():
        print(f"[WARN] {SERVER_UPDATE_SCRIPT} not found — skipping server updater bake.")
        return False
    user, repo   = get_github_info()
    cfg          = load_config()
    modpack_name = cfg.get("settings", {}).get("modpack_name", "")
    content = SERVER_UPDATE_SCRIPT.read_text(encoding="utf-8")
    content = content.replace('"__GITHUB_USER__"',  f'"{user}"')
    content = content.replace('"__GITHUB_REPO__"',  f'"{repo}"')
    content = content.replace('"__MODPACK_NAME__"', f'"{modpack_name}"')
    dest_path = _baked_server_updater_path()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(content, encoding="utf-8")
    print(f"[OK] Baked {SERVER_UPDATE_SCRIPT.name} → {dest_path}")
    return True


# -------------------------
# CACHE MAINTENANCE
# -------------------------


def purge_cache(all_files: bool = False) -> None:
    """
    Remove files from the persistent download cache to reclaim disk space.

    By default, removes files whose (project_id, file_id) pair is not present
    in the latest committed snapshot — i.e. mods that have since been removed
    from the modpack. With all_files=True, clears the entire cache.
    """
    if not DL_CACHE.exists() or not any(DL_CACHE.iterdir()):
        print("[INFO] Download cache is already empty.")
        return

    if all_files:
        shutil.rmtree(DL_CACHE)
        DL_CACHE.mkdir(parents=True, exist_ok=True)
        print("[OK] Download cache cleared.")
        return

    log = load_log()
    if not log:
        print("[ERROR] No committed versions — nothing to compare against.")
        return

    latest_commit_id = log[-1]["commit"]
    latest_snapshot  = load_snapshot(latest_commit_id)
    kept_pairs: set[str] = {
        f"{project_id}_{entry['file_id'] if isinstance(entry, dict) else entry}"
        for project_id, entry in latest_snapshot.items()
    }

    removed_count = 0
    removed_bytes = 0
    for cached_file in DL_CACHE.iterdir():
        if not cached_file.is_file():
            continue
        parts = cached_file.name.split("_", 2)
        if len(parts) < 2:
            continue
        if f"{parts[0]}_{parts[1]}" not in kept_pairs:
            removed_bytes += cached_file.stat().st_size
            cached_file.unlink()
            removed_count += 1
            print(f"  [-] {cached_file.name}")

    if removed_count == 0:
        print("[INFO] Cache only contains files from the latest version — nothing to remove.")
    else:
        size_mb = removed_bytes / (1024 * 1024)
        print(f"\n[OK] Removed {removed_count} file(s), {size_mb:.1f} MB freed.")


# -------------------------
# LOG DISPLAY
# -------------------------


def remove_commit(version: str) -> None:
    """
    Permanently remove a committed version from the log and delete its snapshot.
    Prompts for confirmation — this operation cannot be undone.
    """
    log = load_log()
    if not log:
        print("[ERROR] No committed versions found.")
        return

    target_index = None
    for index, entry in enumerate(log):
        if str(entry["version"]) == str(version):
            target_index = index
            break

    if target_index is None:
        print(f"[ERROR] Version '{version}' not found in log.")
        return

    target_entry = log[target_index]
    commit_id    = target_entry["commit"]
    is_latest    = target_index == len(log) - 1
    is_only      = len(log) == 1

    print(f"[WARN] You are about to permanently remove version {version} ({commit_id}) from history.")
    print("       This deletes the log entry and snapshot file. It cannot be undone.")
    if is_only:
        print("       This is the only committed version — the repository will be left empty.")
    elif not is_latest:
        prev_version = log[target_index - 1]["version"] if target_index > 0 else None
        next_version = log[target_index + 1]["version"]
        context      = f"{prev_version} → {next_version}" if prev_version else f"initial → {next_version}"
        print(f"       Removing an intermediate commit will leave a gap in version history ({context}).")
    print()

    answer = input(f"Type '{version}' to confirm: ").strip()
    if answer != str(version):
        print("[INFO] Aborted.")
        return

    log.pop(target_index)
    save_json(LOG_FILE, log)

    snapshot_path = SNAPSHOTS / f"{commit_id}.json"
    if snapshot_path.exists():
        snapshot_path.unlink()

    print(f"[OK] Version {version} removed from history.")


def show_log() -> None:
    """Print all committed versions in reverse chronological order with diff statistics."""
    log = load_log()
    if not log:
        print("No versions committed yet.")
        return

    print(f"{'Version':<12} {'Commit':<12} {'Date':<20} {'Added':>6} {'Removed':>8} {'Updated':>8}")
    print("-" * 72)
    for entry in reversed(log):
        timestamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry["time"]))
        message = entry.get("message", "")
        message_preview = ""
        if message:
            first_line = message.splitlines()[0]
            truncated  = first_line[:40] + "…" if len(first_line) > 40 else first_line
            message_preview = f'  "{truncated}"'
        print(
            f"{entry['version']:<12} {entry['commit']:<12} {timestamp:<20}"
            f" {entry['added']:>6} {entry['removed']:>8} {entry['updated']:>8}"
            f"{message_preview}"
        )


# -------------------------
# CLI
# -------------------------

if __name__ == "__main__":
    _init_working_dir()

    parser = argparse.ArgumentParser(
        prog="modpackctl",
        description="modpackctl — Minecraft modpack version control",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="command")
    subparsers.required = True

    # init
    parser_init = subparsers.add_parser("init", help="Initialise repo from a CurseForge export zip")
    parser_init.add_argument("zip", help="Path to the CurseForge export zip")
    parser_init.add_argument("--force", action="store_true", help="Reset history, keeping the download cache")

    # commit
    parser_commit = subparsers.add_parser("commit", help="Record a new version from an updated export")
    parser_commit.add_argument("zip", help="Path to the CurseForge export zip")
    parser_commit.add_argument("--major", action="store_true", help="Force a major version bump")
    parser_commit.add_argument("--message", metavar="MESSAGE", default="", help="Release note shown to players in the updater changelog")

    # log
    subparsers.add_parser("log", help="List all committed versions with diff stats")

    # remove-commit
    parser_remove = subparsers.add_parser("remove-commit", help="Permanently remove a committed version from history (irreversible)")
    parser_remove.add_argument("version", help="Version to remove")

    # set-message
    parser_set_message = subparsers.add_parser("set-message", help="Set or clear the release note for any committed version")
    parser_set_message.add_argument("version", help="Version to update")
    parser_set_message.add_argument("message", nargs="?", default="", help="New release note (omit to clear the existing message)")

    # changelog
    parser_changelog = subparsers.add_parser("changelog", help="Generate a Markdown changelog between two versions")
    parser_changelog.add_argument("v1", help="Starting version (or the only version for an initial-release changelog)")
    parser_changelog.add_argument("v2", nargs="?", default=None, help="Ending version (omit to treat v1 as an initial release)")
    parser_changelog.add_argument("--out", default="changelog.md", metavar="FILE", help="Output file (default: changelog.md)")
    parser_changelog.add_argument("--server", action="store_true", help="Server view: exclude client-only mods, shaderpacks, and resourcepacks")
    parser_changelog.add_argument("--message", metavar="MESSAGE", default="", help="Optional note inserted below the heading")

    # release
    parser_release = subparsers.add_parser("release", help="Build a release zip (client by default)")
    parser_release.add_argument("version", help="Version to release")
    parser_release.add_argument("--server", action="store_true", help="Build a server release and bake server-updater (no exe)")

    # publish
    parser_publish = subparsers.add_parser("publish", help="Build a client release and publish to GitHub")
    parser_publish.add_argument("version", help="Version to publish")
    parser_publish.add_argument("--message", metavar="MESSAGE", default="", help="Release note (overrides the message set at commit time)")

    # update
    parser_update = subparsers.add_parser("update", help="Rebuild the build folder for a version without zipping")
    parser_update.add_argument("version", help="Version to build")
    parser_update.add_argument("--server", action="store_true", help="Server view: exclude client-only mods, shaderpacks, and resourcepacks")

    # purge
    parser_purge = subparsers.add_parser("purge", help="Remove old files from the download cache")
    parser_purge.add_argument("--all", action="store_true", help="Clear the entire cache instead of just stale files")

    # build-pages
    subparsers.add_parser("build-pages", help="Build versions.json + snapshots/ locally to gh-pages/")

    # bake-updater
    parser_bake = subparsers.add_parser("bake-updater", help="Bake the updater script with config values")
    parser_bake.add_argument("--server", action="store_true", help="Bake server-updater.py instead of client-updater.py (no exe)")

    # build-exe
    subparsers.add_parser("build-exe", help="Build releases/client-updater.exe from the baked client updater")

    # export-cf
    parser_export_cf = subparsers.add_parser("export-cf", help="Build a CurseForge-format modpack zip for a version")
    parser_export_cf.add_argument("version", help="Version to export")

    # export-example
    subparsers.add_parser("export-example", help="Write modpackctl.toml.example from the built-in defaults")

    if argcomplete:
        argcomplete.autocomplete(parser)
    args = parser.parse_args()

    if args.command == "init":
        init(args.zip, args.force)

    elif args.command == "commit":
        commit(args.zip, args.major, message=args.message)

    elif args.command == "log":
        show_log()

    elif args.command == "remove-commit":
        remove_commit(args.version)

    elif args.command == "set-message":
        if not get_log_entry(args.version):
            print(f"[ERROR] Version '{args.version}' not found in log.")
            sys.exit(1)
        set_version_message(args.version, args.message)
        if args.message:
            print(f"[OK] Message set for v{args.version}.")
        else:
            print(f"[OK] Message cleared for v{args.version}.")

    elif args.command == "changelog":
        if args.server:
            cl_exclude            = get_filter_list("client_only")
            cl_exclude_categories = {"shaderpacks", "resourcepacks"}
        else:
            cl_exclude            = get_filter_list("server_only")
            cl_exclude_categories = None
        changelog(args.v1, args.v2, out=args.out, message=args.message,
                  exclude=cl_exclude, exclude_categories=cl_exclude_categories)

    elif args.command == "release":
        if args.server:
            release_server(args.version)
        else:
            release_client(args.version)

    elif args.command == "publish":
        publish(args.version, message=args.message)

    elif args.command == "update":
        if args.server:
            excluded = get_filter_list("client_only")
            update(args.version, exclude=excluded, exclude_categories={"shaderpacks", "resourcepacks"}, suffix="server")
        else:
            excluded = get_filter_list("server_only")
            update(args.version, exclude=excluded, suffix="client")

    elif args.command == "purge":
        purge_cache(args.all)

    elif args.command == "build-pages":
        build_pages()

    elif args.command == "bake-updater":
        if args.server:
            if not bake_server_updater():
                print(f"[ERROR] Bake failed — is {SERVER_UPDATE_SCRIPT} present in the project root?")
                sys.exit(1)
        else:
            if not bake_client_updater():
                print(f"[ERROR] Bake failed — is {CLIENT_UPDATE_SCRIPT} present in the project root?")
                sys.exit(1)

    elif args.command == "build-exe":
        build_exe()

    elif args.command == "export-cf":
        if not export_cf(args.version):
            sys.exit(1)

    elif args.command == "export-example":
        CONFIG_EXAMPLE.write_text(DEFAULT_CONFIG, encoding="utf-8")
        print(f"[OK] Written to {CONFIG_EXAMPLE}")
