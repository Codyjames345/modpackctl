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

_HERE                 = Path(__file__).parent           # directory containing modpackctl.py
_TEMPLATES_DIR        = _HERE / "templates"             # bundled example/template files
_MODPACKCTL_RAW_BASE  = "https://raw.githubusercontent.com/Codyjames345/modpackctl/main"

REPO            = Path(".modpackctl")
SNAPSHOTS       = REPO / "snapshots"
LOG_FILE        = REPO / "log.json"
CACHE           = REPO / "mod_cache.json"    # project_id -> { name, files: { file_id: filename } }
DL_CACHE        = REPO / "dl_cache"          # permanent jar store keyed by (project_id, file_id)
OVERRIDES_BLOBS = REPO / "overrides_blobs"   # content-addressed store: sha256 hex -> file bytes
CONFIG_FILE     = Path("modpackctl.toml")
GITIGNORE       = Path(".gitignore")

BUILD            = Path("build")
RELEASES         = Path("releases")
README           = Path("README.md")
README_TEMPLATE  = Path("README.template.md")
PYINSTALLER      = Path(".pyinstaller")
PAGES_OUTPUT     = Path("gh-pages")
CONFIG_EXAMPLE   = Path("modpackctl.example.toml")
CLIENT_UPDATE_SCRIPT = Path("client-updater.py")   # working copy; customise for this modpack
SERVER_UPDATE_SCRIPT = Path("server-updater.py")   # working copy; customise for this modpack
UPDATER_COMMON       = Path("updater_common.py")   # shared helpers, inlined into both updaters at bake time
_DANCE_DEFAULT_URL  = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

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

def _check_config_placeholders(cfg: dict) -> None:
    """Exit with a clear message if any required config values still contain unedited placeholder text."""
    github   = cfg.get("github",   {})
    settings = cfg.get("settings", {})
    unfilled = [
        (label, value)
        for label, value in [
            ("[github] user",            github.get("user",           "")),
            ("[github] repo",            github.get("repo",           "")),
            ("[settings] modpack_name",  settings.get("modpack_name", "")),
        ]
        if isinstance(value, str) and "<" in value
    ]
    if unfilled:
        print(f"[ERROR] {CONFIG_FILE} still has unfilled placeholder values:")
        for label, value in unfilled:
            print(f"  {label} = \"{value}\"")
        print(f"\nEdit {CONFIG_FILE} and replace each placeholder before running modpackctl.")
        sys.exit(1)


def load_config() -> dict:
    """Load and return the TOML config. Exits with a clear error if the file is missing, malformed, or has unfilled placeholders."""
    if not CONFIG_FILE.exists():
        print(f"[ERROR] {CONFIG_FILE} not found. Run modpackctl from a working directory, or re-run to initialize one.")
        sys.exit(1)
    try:
        with open(CONFIG_FILE, "rb") as fh:
            cfg = tomllib.load(fh)
    except Exception as exc:
        print(f"[ERROR] Could not parse {CONFIG_FILE}: {exc}")
        sys.exit(1)
    _check_config_placeholders(cfg)
    return cfg


def _download_file_from_repo(filename: str) -> bool:
    """Download filename from templates/ in the modpackctl GitHub repo into _TEMPLATES_DIR. Returns True on success."""
    url  = f"{_MODPACKCTL_RAW_BASE}/templates/{filename}"
    dest = _TEMPLATES_DIR / filename
    _TEMPLATES_DIR.mkdir(exist_ok=True)
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        dest.write_bytes(response.content)
        print(f"[OK] Downloaded {filename} from modpackctl repo.")
        return True
    except Exception as exc:
        print(f"[WARN] Could not download {filename}: {exc}")
        return False


def _ensure_example(example_name: str) -> Path | None:
    """
    Return the path to the bundled example file in _TEMPLATES_DIR,
    downloading it from the modpackctl GitHub repo if it is not present locally.
    Returns None if the file could not be obtained.
    """
    src = _TEMPLATES_DIR / example_name
    if not src.exists():
        print(f"[INFO] {example_name} not found locally — downloading from modpackctl repo...")
        if not _download_file_from_repo(example_name):
            return None
    return src


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

    gitignore_example = _ensure_example("example.gitignore")

    if gitignore_example is not None:
        entries = gitignore_example.read_text(encoding="utf-8").splitlines()
        if not GITIGNORE.exists():
            shutil.copy2(gitignore_example, GITIGNORE)
            print("[OK] Created .gitignore.")
        else:
            existing_lines = set(GITIGNORE.read_text(encoding="utf-8").splitlines())
            missing = [e for e in entries if e not in existing_lines]
            if missing:
                with open(GITIGNORE, "a", encoding="utf-8") as fh:
                    fh.write("\n".join(missing) + "\n")
                print("[OK] Updated .gitignore with modpackctl entries.")
    else:
        print("[WARN] Could not obtain example.gitignore — skipping .gitignore setup.")

    if not already_git:
        print("[INFO] Once you've created the GitHub repo and edited the config, add a remote:")
        print("       git remote add origin https://github.com/<user>/<repo>.git")


def _init_working_dir() -> None:
    """
    Ensure the current working directory contains modpackctl.toml before any command runs.

    If the config is not in the CWD, checks whether a parent directory in the same git
    repo has one and silently changes to it. Falls back to prompting the user to
    initialise a new working directory if no config can be found.
    """
    if CONFIG_FILE.exists():
        return

    # Walk up through parent directories looking for modpackctl.toml.
    # Stop once we reach a directory that contains .git (the repo root),
    # or the filesystem root if there is no git repo.
    current = Path.cwd()
    while True:
        parent = current.parent
        if parent == current:   # filesystem root — give up
            break
        if (parent / CONFIG_FILE).exists():
            os.chdir(parent)
            print(f"[INFO] Working directory: {parent}")
            return
        current = parent
        if (current / ".git").is_dir():  # stop at repo root
            break

    print(f"No {CONFIG_FILE} found in the current directory.")
    answer = input("Initialize a working directory here? (This will also create a git repo.) [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("[INFO] Aborted.")
        sys.exit(0)
    _ensure_files(CONFIG_FILE, CLIENT_UPDATE_SCRIPT, SERVER_UPDATE_SCRIPT)
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


def get_custom_mods() -> dict[str, dict]:
    """
    Return metadata for custom override mods declared in [[settings.custom_mods]].

    These are files in the overrides/ tree (typically jars under mods/ that aren't on
    CurseForge) that need a human-readable name and/or a client/server-only side, since
    the CurseForge API can't resolve them. Returns {override_path: {"name", "side"}},
    where override_path is the posix path under overrides/ (e.g. 'mods/MyMod.jar') and
    side is "client", "server", or "" (both).
    """
    cfg     = load_config()
    entries = cfg.get("settings", {}).get("custom_mods", [])
    result: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("file"):
            continue
        path = Path(str(entry["file"])).as_posix()
        side = str(entry.get("side", "")).strip().lower()
        result[path] = {
            "name": str(entry.get("name", "")).strip(),
            "side": side if side in ("client", "server") else "",
        }
    return result


def get_custom_mod_names() -> dict[str, str]:
    """Return {override_path: display_name} for custom mods that declare a name."""
    return {path: meta["name"] for path, meta in get_custom_mods().items() if meta["name"]}


def get_side_only_overrides(side: str) -> set[str]:
    """Return the override paths declared for the given side only ('client' or 'server')."""
    return {path for path, meta in get_custom_mods().items() if meta["side"] == side}


def _side_filters(side: str) -> dict:
    """
    Return the build/changelog exclusion filters for a release side.

    'server' drops client-only mods and custom override mods plus shaderpacks and
    resourcepacks; 'client' (the default) drops server-only mods and custom override
    mods. Shared by release(), the changelog notes, and the update CLI so the three
    stay in sync.
    """
    if side == "server":
        return {
            "exclude":            get_filter_list("client_only"),
            "exclude_categories": {"shaderpacks", "resourcepacks"},
            "exclude_overrides":  get_side_only_overrides("client"),
        }
    return {
        "exclude":            get_filter_list("server_only"),
        "exclude_categories": None,
        "exclude_overrides":  get_side_only_overrides("server"),
    }


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


def _override_member_allowed(relative_path: str) -> bool:
    """
    Return True if an overrides/ member should be version-controlled.

    Inside shaderpacks/, only direct .zip files are kept — extracted shaderpack
    folders (which Euphoria Patcher creates when a player uses them) and other
    non-zip files are skipped so they don't bloat the overrides bundle.
    """
    if not relative_path:
        return False
    if relative_path.startswith("shaderpacks/"):
        sub_path = relative_path[len("shaderpacks/"):].rstrip("/")
        if not sub_path or "/" in sub_path or not sub_path.lower().endswith(".zip"):
            return False
    return True


def _store_override_blob(data: bytes) -> str:
    """Write data into the content-addressed override blob store and return its sha256 hex."""
    digest    = hashlib.sha256(data).hexdigest()
    OVERRIDES_BLOBS.mkdir(parents=True, exist_ok=True)
    blob_path = OVERRIDES_BLOBS / digest
    if not blob_path.exists():
        blob_path.write_bytes(data)
    return digest


def extract_override_manifest(zip_path: Path | str) -> dict[str, str]:
    """
    Extract the overrides/ tree from a CurseForge zip into the content-addressed
    blob store and return a manifest mapping each override's posix relative path
    to its sha256 hex. Used so override files are version-controlled per commit
    exactly like CurseForge mods are.
    """
    zip_path = Path(zip_path)
    manifest: dict[str, str] = {}
    if not zip_path.is_file() or zip_path.suffix != ".zip":
        return manifest

    prefix = "overrides/"
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member_name in zf.namelist():
            if not member_name.startswith(prefix) or member_name.endswith("/"):
                continue
            relative_path = member_name[len(prefix):]
            if not _override_member_allowed(relative_path):
                continue
            with zf.open(member_name) as src:
                data = src.read()
            manifest[Path(relative_path).as_posix()] = _store_override_blob(data)
    return manifest


def save_override_manifest(commit_id: str, manifest: dict[str, str]) -> None:
    """Persist the override path->hash manifest for a commit alongside its mod snapshot."""
    save_json(SNAPSHOTS / f"{commit_id}.overrides.json", manifest)


def load_override_manifest(commit_id: str) -> dict[str, str]:
    """Return the override path->hash manifest for a commit, or {} if it has none."""
    return load_json(SNAPSHOTS / f"{commit_id}.overrides.json", {})


def apply_overrides(dest: Path, commit_id: str, exclude_paths: set[str] | None = None) -> int:
    """
    Reconstruct a commit's exact override tree from the blob store into dest
    (the release build directory). Override paths in exclude_paths are skipped
    (used to drop client-only / server-only custom mods from a side's build).
    Returns the number of override files written.
    """
    exclude_paths = exclude_paths or set()
    manifest = load_override_manifest(commit_id)
    count = 0
    for relative_path, digest in manifest.items():
        if relative_path in exclude_paths:
            continue
        blob_path = OVERRIDES_BLOBS / digest
        if not blob_path.exists():
            print(f"  [WARN] Missing override blob for {relative_path} ({digest[:10]}) — skipping.")
            continue
        out_path = dest / relative_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(blob_path, out_path)
        count += 1
    return count


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


def diff_overrides(old: dict[str, str], new: dict[str, str]) -> dict:
    """
    Compute the difference between two override manifests (path -> sha256 hex).
    Returns a dict with keys 'added' (set of paths), 'removed' (set of paths), and
    'updated' (list of (path, old_hash, new_hash) for paths whose content changed).
    """
    added   = new.keys() - old.keys()
    removed = old.keys() - new.keys()
    updated = [
        (path, old[path], new[path])
        for path in old.keys() & new.keys()
        if old[path] != new[path]
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


def validate_manual_version(version: str, old_version: str) -> str:
    """
    Validate a maintainer-supplied version (from 'commit --version').

    The whole tool — version ordering, the updater's "is this newer?" check, and the
    bcc-common.toml comparison — assumes clean, strictly increasing x.y.z versions, so a
    manual version must match that and be greater than the latest committed version.
    Exits with a clear error otherwise. Returns the normalised version string.
    """
    version = version.strip().lstrip("vV").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        print(f"[ERROR] --version must be a plain x.y.z number (e.g. 2.0.0), got: {version!r}")
        sys.exit(1)
    if old_version and parse_version(version) <= parse_version(old_version):
        print(f"[ERROR] --version {version} must be greater than the latest committed version {old_version}.")
        sys.exit(1)
    return version


# -------------------------
# FILENAME GUESSING
# -------------------------


def _resolve_remote_filename(project_id: str, file_id: str) -> str:
    """
    Follow the CurseForge download redirect to learn the filename without downloading the file.
    Returns an empty string on any network or parsing error.
    """
    url = f"https://www.curseforge.com/api/v1/mods/{project_id}/files/{file_id}/download"
    try:
        response = requests.get(url, headers=HEADERS, allow_redirects=True, stream=True, timeout=10)
        response.close()
        return guess_filename(response, project_id, file_id)
    except Exception:
        return ""


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


def render_readme(version: str) -> bool:
    """
    Render README.md by substituting all known placeholders in README.template.md
    with their current values. The template is left untouched. Returns True if written.
    No-op (returns False) when README.template.md does not exist.
    """
    if not README_TEMPLATE.exists():
        return False
    log_entry = get_log_entry(version)
    if not log_entry:
        return False

    modloader         = log_entry.get("modloader", "")
    minecraft_version = log_entry.get("minecraft_version", "")
    loader_prefix     = modloader.split("-")[0] if modloader else ""
    loader_type       = (_LOADER_DISPLAY_NAMES.get(loader_prefix.lower()) or loader_prefix.capitalize()) if loader_prefix else ""

    cfg      = load_config()
    settings = cfg.get("settings", {})
    github   = cfg.get("github", {})
    gh_user  = github.get("user", "")
    gh_repo  = github.get("repo", "")

    replacements = {
        "__MODLOADER__":         modloader,
        "__MODLOADER_TYPE__":    loader_type,
        "__MINECRAFT_VERSION__": minecraft_version,
        "__LATEST_VERSION__":    version,
        "__MODPACK_NAME__":      settings.get("modpack_name", ""),
        "__FILE_PREFIX__":       settings.get("file_prefix") or settings.get("modpack_name", ""),
        "__RELEASES_URL__":      f"https://github.com/{gh_user}/{gh_repo}/releases" if gh_user and gh_repo else "",
        "__AUTHOR__":            settings.get("author", ""),
        "__SERVER_ADDRESS__":    settings.get("server_address", ""),
        "__DISCORD_URL__":       settings.get("discord_url", ""),
        "__MAP_URL__":           settings.get("map_url", ""),
    }

    content = README_TEMPLATE.read_text(encoding="utf-8")
    for placeholder, value in replacements.items():
        if value:
            content = content.replace(placeholder, value)
    README.write_text(content, encoding="utf-8")
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
        for directory in (OVERRIDES_BLOBS, SNAPSHOTS):
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


def commit(source: str, major: bool = False, message: str = "",
           version_override: str | None = None) -> tuple[str, str, int] | None:
    """
    Record a new version from an updated CurseForge export zip.

    By default the version is bumped automatically from the diff (added/removed →
    minor, updates → patch) and a modloader change forces a major bump. The --major
    flag forces a major bump regardless. version_override (from 'commit --version')
    sets an explicit x.y.z version instead, validated to be clean and increasing.
    Returns (version, commit_id, mod_count), or None if nothing changed.
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

    # Override files are version-controlled alongside the mods: their content is
    # folded into the commit id so editing a custom jar or config produces a new
    # version, exactly as changing a CurseForge file id would.
    override_manifest = extract_override_manifest(source)
    if override_manifest:
        commit_id = hash_state({"mods": bare_mods, "overrides": override_manifest})
    else:
        # Preserve the legacy hash for mod-only packs so existing repos stay stable.
        commit_id = hash_state(bare_mods)

    log = load_log()

    for existing_entry in log:
        if existing_entry["commit"] == commit_id:
            print(f"[INFO] This zip matches an already-committed version ({existing_entry['version']}) — nothing to commit.")
            return None

    old_commit            = log[-1]["commit"] if log else ""
    old_snapshot          = load_snapshot(old_commit) if old_commit else {}
    old_override_manifest = load_override_manifest(old_commit) if old_commit else {}
    old_version           = log[-1]["version"] if log else ""
    old_modloader         = log[-1].get("modloader", "") if log else ""

    changes          = diff(old_snapshot, bare_mods)
    override_changes = diff_overrides(old_override_manifest, override_manifest)

    # Version bumps consider mods and overrides together.
    combined_changes = {
        "added":   bool(changes["added"]   or override_changes["added"]),
        "removed": bool(changes["removed"] or override_changes["removed"]),
        "updated": bool(changes["updated"] or override_changes["updated"]),
    }

    # Only trigger auto-major if both old and new modloader strings are known;
    # avoids a false positive when the manifest lacks a modLoaders entry.
    modloader_changed = bool(
        old_version and old_modloader and new_modloader and old_modloader != new_modloader
    )

    if version_override is not None:
        # Maintainer takes manual control; their version wins over auto/major bumps.
        version = validate_manual_version(version_override, old_version)
    elif not old_version:
        version = "1.0.0"
    elif major or modloader_changed:
        version = bump_major(old_version)
    else:
        version = bump(old_version, combined_changes)

    _prefetch_names({str(mod["projectID"]) for mod in manifest.get("files", [])})
    snapshot = build_snapshot(manifest, load_json(CACHE, {}))
    save_snapshot(commit_id, snapshot)
    save_override_manifest(commit_id, override_manifest)
    add_version(
        commit_id, version,
        added=len(changes["added"])   + len(override_changes["added"]),
        removed=len(changes["removed"]) + len(override_changes["removed"]),
        updated=len(changes["updated"]) + len(override_changes["updated"]),
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
    if changes["added"]:
        print(f"  [+] {len(changes['added'])} mod(s) added")
    if changes["removed"]:
        print(f"  [-] {len(changes['removed'])} mod(s) removed")
    if changes["updated"]:
        print(f"  [~] {len(changes['updated'])} mod(s) updated")
    if override_changes["added"]:
        print(f"  [+] {len(override_changes['added'])} override file(s) added")
    if override_changes["removed"]:
        print(f"  [-] {len(override_changes['removed'])} override file(s) removed")
    if override_changes["updated"]:
        print(f"  [~] {len(override_changes['updated'])} override file(s) updated")
    if not any(combined_changes.values()) and not modloader_changed:
        print("  (no changes)")

    print(f"  {len(override_manifest)} override file(s) tracked.")

    return version, commit_id, len(snapshot)


# -------------------------
# CHANGELOG
# -------------------------


def changelog(
    v1: str | None,
    v2: str,
    out: str = "changelog.md",
    message: str = "",
    exclude: set[str] | None = None,
    exclude_categories: set[str] | None = None,
    exclude_overrides: set[str] | None = None,
) -> None:
    """
    Generate a Markdown changelog between two committed versions and write it to a file.
    When v1 is None, v2 is treated as an initial release and diffed against an empty state.
    Includes a Modloader section when the modloader id changed between the two versions.
    An optional message is inserted as a short paragraph below the heading.
    exclude filters out specific project IDs; exclude_categories filters by mod category
    (e.g. 'shaderpacks', 'resourcepacks') using the stored mod index; exclude_overrides
    filters out specific override paths (client-only / server-only custom mods).
    Custom override mods are shown by their configured display name where available.
    """
    excluded_override_paths = exclude_overrides or set()
    custom_mod_names        = get_custom_mod_names()

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

    # Override files are version-controlled too: their per-commit manifests are diffed
    # alongside the mods. exclude (project IDs) doesn't apply to overrides, but
    # exclude_categories does for files under a shaderpacks/ or resourcepacks/ top folder,
    # and exclude_overrides drops client-only / server-only custom mods by path.
    def apply_override_filter(manifest: dict) -> dict:
        if not exclude_categories and not excluded_override_paths:
            return manifest
        return {
            path: digest
            for path, digest in manifest.items()
            if path not in excluded_override_paths
            and (not exclude_categories or path.split("/", 1)[0] not in exclude_categories)
        }

    new_override_manifest = apply_override_filter(load_override_manifest(new_commit_id))

    if v1 is None:
        changes               = diff({}, apply_side_filter(load_snapshot(new_commit_id)))
        header_title          = f"# Changelog: {v2} (Initial Release)"
        old_modloader         = ""
        old_override_manifest: dict[str, str] = {}
    else:
        old_commit_id = get_commit(v1)
        if not old_commit_id:
            print(f"[ERROR] Version '{v1}' not found in log.")
            return
        changes               = diff(
            apply_side_filter(load_snapshot(old_commit_id)),
            apply_side_filter(load_snapshot(new_commit_id)),
        )
        header_title          = f"# Changelog: {v1} → {v2}"
        old_entry             = get_log_entry(v1)
        old_modloader         = old_entry.get("modloader", "") if old_entry else ""
        old_override_manifest = apply_override_filter(load_override_manifest(old_commit_id))

    override_changes = diff_overrides(old_override_manifest, new_override_manifest)

    modloader_changed = bool(
        old_modloader and new_modloader and old_modloader != new_modloader
    )
    has_changes = (
        changes["added"] or changes["removed"] or changes["updated"] or modloader_changed
        or override_changes["added"] or override_changes["removed"] or override_changes["updated"]
    )

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

    def append_override_lines(paths: list[str], marker: str) -> None:
        """
        Append override entries below the mods in a section. Custom mods with a
        configured display name render by name (the CF API can't resolve them);
        all other override files render as their `path` in code.
        """
        for index, path in enumerate(sorted(paths), 1):
            display = custom_mod_names.get(path)
            print(f"  [{marker}] {display or path} (override) ({index}/{len(paths)})")
            lines.append(f"- {display}" if display else f"- `{path}`")

    override_added   = list(override_changes["added"])
    override_removed = list(override_changes["removed"])
    override_updated = [path for path, _, _ in override_changes["updated"]]

    # --- Added ---
    lines.append("## ➕ Added")
    if changes["added"] or override_added:
        added_list = sorted(changes["added"])
        for index, project_id in enumerate(added_list, 1):
            mod_name = resolve_mod(project_id)
            print(f"  [+] {mod_name} ({index}/{len(added_list)})")
            lines.append(f"- {mod_name}")
        append_override_lines(override_added, "+")
    else:
        lines.append("_No mods added._")
    lines.append("")

    # --- Removed ---
    lines.append("## ➖ Removed")
    if changes["removed"] or override_removed:
        removed_list = sorted(changes["removed"])
        for index, project_id in enumerate(removed_list, 1):
            mod_name = resolve_mod(project_id)
            print(f"  [-] {mod_name} ({index}/{len(removed_list)})")
            lines.append(f"- {mod_name}")
        append_override_lines(override_removed, "-")
    else:
        lines.append("_No mods removed._")
    lines.append("")

    # --- Updated ---
    lines.append("## 🔄 Updated")
    if changes["updated"] or override_updated:
        updated_list = sorted(changes["updated"])
        for index, (project_id, *_) in enumerate(updated_list, 1):
            mod_name = resolve_mod(project_id)
            print(f"  [~] {mod_name} ({index}/{len(updated_list)})")
            lines.append(f"- {mod_name}")
        append_override_lines(override_updated, "~")
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
    exclude_overrides: set[str] | None = None,
) -> dict:
    """
    Clear the build directory and rebuild it cleanly for the given version, so that
    BUILD/ mirrors the final .minecraft folder structure (mods plus the merged
    override tree: configs, custom mods, etc.).

    Every mod in the snapshot is copied from DL_CACHE if present, otherwise
    downloaded from CurseForge and cached for future use. Excluded project IDs
    are skipped entirely (used to produce client-only or server-only builds).
    Mods whose category (from the existing index) is in exclude_categories are
    also skipped — used to drop shaderpacks and resourcepacks from server builds.
    exclude_overrides drops specific override paths (side-only custom mods).

    suffix is a display label ('client' or 'server') appended to output messages.
    Returns a stats dict: { downloaded, cached, failed, ok, overrides }.
    """
    excluded_ids = {str(project_id) for project_id in exclude} if exclude else set()
    commit_id    = get_commit(version)
    if not commit_id:
        print(f"[ERROR] Version '{version}' not found.")
        return {"downloaded": 0, "cached": 0, "failed": 0, "ok": 0, "overrides": 0}

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

    # Merge the version's override tree (configs, custom mods, etc.) on top of the
    # downloaded mods so BUILD/ mirrors the final .minecraft layout. Side-only custom
    # mods are dropped here for client/server builds.
    override_count = apply_overrides(BUILD, commit_id, exclude_paths=exclude_overrides)
    if override_count:
        print(f"  {override_count} override file(s) merged into build.")

    ok      = downloaded + cached
    summary = f"{ok} mods: {downloaded} downloaded, {cached} from cache"
    if failed:
        summary += f", {failed} failed"
    print(f"\n[OK] Updated to {label}  ({summary})")

    return {"downloaded": downloaded, "cached": cached, "failed": failed, "ok": ok,
            "overrides": override_count}


# -------------------------
# RELEASE  (delegates to update, then zips)
# -------------------------


def release(version: str, side: str = "client") -> Path | None:
    """
    Build a distributable release zip for the given side ('client' or 'server').

    Calls update() to produce a complete build/ (mods plus the merged override tree),
    zips that build into releases/, then refreshes the local gh-pages/ folder. The side
    decides which mods and custom override mods are excluded (see _side_filters).

    Returns the Path to the created zip, or None if the build failed.
    """
    commit_id = get_commit(version)
    if not commit_id:
        print(f"[ERROR] Version '{version}' not found in log.")
        return None

    filters    = _side_filters(side)
    other_side = "server" if side == "client" else "client"
    if filters["exclude"]:
        print(f"[INFO] Excluding {len(filters['exclude'])} {other_side}-only mod(s).")
    if filters["exclude_overrides"]:
        print(f"[INFO] Excluding {len(filters['exclude_overrides'])} {other_side}-only custom override mod(s).")

    stats = update(version, exclude=filters["exclude"], exclude_categories=filters["exclude_categories"],
                   suffix=side, exclude_overrides=filters["exclude_overrides"])

    if stats["failed"] != 0:
        print("[ERROR] Release aborted: not all mods could be fetched.")
        return None

    override_count = stats["overrides"]

    if not any(BUILD.rglob("*")):
        print("[ERROR] Release aborted: build folder is empty.")
        return None

    RELEASES.mkdir(parents=True, exist_ok=True)
    prefix      = get_file_prefix()
    zip_name    = f"{prefix}-{version}-{side}.zip"
    zip_path    = RELEASES / zip_name

    print(f"\nBuilding release zip at {zip_path}...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(BUILD):
            for filename in files:
                file_path = Path(root) / filename
                zf.write(file_path, file_path.relative_to(BUILD))

    snapshot       = load_snapshot(commit_id)
    excluded_count = len(filters["exclude"])
    print(f"\n{'=' * 36}")
    print(f" RELEASE REPORT — v{version}-{side}")
    print(f"{'=' * 36}")
    print(f"  Mods in snapshot : {len(snapshot)}")
    print(f"  Excluded         : {excluded_count}")
    print(f"  Downloaded       : {stats['downloaded']}")
    print(f"  From cache       : {stats['cached']}")
    print(f"  Failed           : {stats['failed']}")
    print(f"  Overrides        : {override_count}")
    print(f"  Output           : {zip_name}")
    print(f"{'=' * 36}\n")
    print(f"[OK] Built {zip_name}")

    # Render README.md from the template for client builds (server builds never touch it).
    if side == "client" and render_readme(version):
        print("  [i] README.md updated.")

    # Refresh the local gh-pages/ folder so versions.json, snapshots, and per-commit
    # override data reflect this version (publish() pushes this folder to the branch).
    build_pages(show_hint=False)

    return zip_path


def release_client(version: str) -> Path | None:
    """Build the client release zip and CurseForge export zip, then bake the client updater (.py + .exe)."""
    print(f"Building client release for v{version}...")
    zip_path = release(version, side="client")
    if zip_path:
        if bake_client_updater():
            _build_exe(_baked_client_updater_path())
        export_cf(version)
    return zip_path


def release_server(version: str) -> Path | None:
    """Build the server release zip, then bake the server updater (.py)."""
    print(f"Building server release for v{version}...")
    zip_path = release(version, side="server")
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
    # This is a client-side export, so drop server-only custom override mods.
    bcc_rel_posix     = "config/bcc-common.toml"
    server_only_overrides = get_side_only_overrides("server")
    override_manifest = {
        path: digest
        for path, digest in load_override_manifest(commit_id).items()
        if path not in server_only_overrides
    }
    bcc_v_re  = re.compile(r'^([ \t]*modpackVersion\s*=\s*)"[^"]*"', re.MULTILINE)
    bcc_n_re  = re.compile(r'^([ \t]*modpackName\s*=\s*)"[^"]*"',    re.MULTILINE)
    bcc_digest = override_manifest.get(bcc_rel_posix)
    bcc_blob   = (OVERRIDES_BLOBS / bcc_digest) if bcc_digest else None
    if bcc_blob and bcc_blob.exists():
        bcc_text = bcc_blob.read_text(encoding="utf-8")
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
            for rel_posix, digest in override_manifest.items():
                if rel_posix == bcc_rel_posix:
                    continue  # written separately below with updated version
                blob = OVERRIDES_BLOBS / digest
                if not blob.exists():
                    print(f"  [WARN] Missing override blob for {rel_posix} — skipping.")
                    continue
                zf.write(blob, f"overrides/{rel_posix}")
                override_file_count += 1
            zf.writestr(f"overrides/{bcc_rel_posix}", bcc_text)
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
    client_only_overrides = sorted(get_side_only_overrides("client"))
    server_only_overrides = sorted(get_side_only_overrides("server"))
    custom_mod_names      = get_custom_mod_names()
    payload: dict = {"latest": log[-1]["version"] if log else None, "versions": versions}
    if client_only_ids:
        payload["client_only_ids"] = client_only_ids
    if server_only_ids:
        payload["server_only_ids"] = server_only_ids
    if client_only_overrides:
        payload["client_only_overrides"] = client_only_overrides
    if server_only_overrides:
        payload["server_only_overrides"] = server_only_overrides
    if custom_mod_names:
        payload["custom_mod_names"] = custom_mod_names
    return payload


def _get_notes_file_for_release(version: str, message: str = "", side: str = "") -> Path:
    """
    Generate a temporary Markdown changelog file for the given version.
    Diffs against the previous version if one exists, otherwise treats it as an initial release.
    side='client' excludes server_only mods; side='server' excludes client_only mods and
    non-mod categories. The caller is responsible for deleting this file after use.
    """
    filters = _side_filters(side) if side in ("client", "server") else {
        "exclude": None, "exclude_categories": None, "exclude_overrides": None,
    }
    release_exclude            = filters["exclude"]
    release_exclude_categories = filters["exclude_categories"]
    release_exclude_overrides  = filters["exclude_overrides"]

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
                  exclude=release_exclude, exclude_categories=release_exclude_categories,
                  exclude_overrides=release_exclude_overrides)
    else:
        changelog(None, version, out=str(notes_path), message=message,
                  exclude=release_exclude, exclude_categories=release_exclude_categories,
                  exclude_overrides=release_exclude_overrides)

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
            snapshot_data = json.loads(snapshot_out.read_text(encoding="utf-8"))
            all_complete = all(
                not isinstance(entry, dict) or (entry.get("file") and entry.get("category"))
                for entry in snapshot_data.values()
            )
            if all_complete:
                continue
        else:
            snapshot_data = load_snapshot(commit_id)

        resolved_any = False
        for project_id, entry in snapshot_data.items():
            if not isinstance(entry, dict) or (entry.get("file") and entry.get("category")):
                continue
            file_id     = str(entry.get("file_id", ""))
            cached_path = _cached_jar_path(project_id, file_id)
            if cached_path:
                if not entry.get("category"):
                    entry["category"] = classify_mod_file(cached_path)
                if not entry.get("file"):
                    entry["file"] = cached_path.name.split("_", 2)[2]
            else:
                if not entry.get("category"):
                    entry["category"] = "mods"
                if not entry.get("file") and file_id:
                    resolved = _resolve_remote_filename(project_id, file_id)
                    if resolved:
                        entry["file"] = resolved
            resolved_any = True
        if resolved_any:
            save_snapshot(commit_id, snapshot_data)
        snapshot_out.write_text(json.dumps(snapshot_data, indent=2))

    # Publish per-commit override data so the client can fetch the exact override
    # tree for any version: a manifest (path -> hash) it diffs to compute changes,
    # and a zip of the file contents it applies. Both are immutable per commit.
    overrides_dir = dest / "overrides"
    for log_entry in load_log():
        commit_id         = log_entry["commit"]
        override_manifest = load_override_manifest(commit_id)
        # Always publish the manifest (even when empty) so the client can diff cleanly.
        (snapshots_dir / f"{commit_id}.overrides.json").write_text(
            json.dumps(override_manifest, indent=2)
        )
        if not override_manifest:
            continue
        overrides_dir.mkdir(parents=True, exist_ok=True)
        override_zip_out = overrides_dir / f"{commit_id}.zip"
        if override_zip_out.exists():
            continue  # already published — content is immutable per commit
        with zipfile.ZipFile(override_zip_out, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel_posix, digest in sorted(override_manifest.items()):
                blob = OVERRIDES_BLOBS / digest
                if blob.exists():
                    zf.write(blob, rel_posix)


_GH_PAGES_WORKTREE = Path(".gh-pages-worktree")
_GH_PAGES_TMP_BRANCH = "gh-pages-temp"


def _copy_pages_into(dest_dir: Path) -> list[str]:
    """
    Copy the built gh-pages assets from PAGES_OUTPUT into dest_dir and return the
    top-level paths to stage with 'git add'. Only versions.json is required; the
    snapshots/ and overrides/ trees are included when present.
    """
    shutil.copy2(PAGES_OUTPUT / "versions.json", dest_dir / "versions.json")
    add_paths = ["versions.json"]
    for name in ("snapshots", "overrides"):
        src = PAGES_OUTPUT / name
        if src.exists():
            shutil.copytree(src, dest_dir / name, dirs_exist_ok=True)
            add_paths.append(name)
    return add_paths


def _cleanup_pages_worktree() -> None:
    """Remove the temporary gh-pages worktree and temp branch, ignoring errors."""
    _run(["git", "worktree", "remove", "--force", str(_GH_PAGES_WORKTREE)], capture_output=True)
    if _GH_PAGES_WORKTREE.exists():
        shutil.rmtree(_GH_PAGES_WORKTREE, ignore_errors=True)
    _run(["git", "branch", "-D", _GH_PAGES_TMP_BRANCH], capture_output=True)
    _run(["git", "worktree", "prune"], capture_output=True)


def _push_pages_assets() -> None:
    """
    Push the local gh-pages/ folder (versions.json, snapshots, and per-commit override
    data) to the gh-pages branch, creating it as an orphan if it does not yet exist.

    All git work happens in a throwaway worktree, so the main checkout's branch and
    files are never touched — including on first-time branch creation. The caller is
    responsible for building gh-pages/ first (release() or build_pages() does this).
    """
    print("Pushing versions.json + snapshots to gh-pages...")
    try:
        ls_result = _run(
            ["git", "ls-remote", "--heads", "origin", "gh-pages"],
            capture_output=True, text=True, check=True,
        )
        branch_exists = "gh-pages" in ls_result.stdout

        # Clear any worktree/branch left behind by a previous failed run.
        _cleanup_pages_worktree()

        if branch_exists:
            _run(["git", "fetch", "origin", "gh-pages"], check=True)
            _run(
                ["git", "worktree", "add", "-b", _GH_PAGES_TMP_BRANCH,
                 str(_GH_PAGES_WORKTREE), "origin/gh-pages"],
                check=True, capture_output=True,
            )
        else:
            print("[INFO] Creating gh-pages branch...")
            # Detached worktree at the current HEAD, then re-point it to a fresh orphan
            # branch and clear the inherited files — all inside the worktree, so the main
            # checkout is untouched and the gh-pages history starts clean.
            _run(["git", "worktree", "add", "--detach", str(_GH_PAGES_WORKTREE)],
                 check=True, capture_output=True)
            _run(["git", "checkout", "--orphan", _GH_PAGES_TMP_BRANCH],
                 check=True, capture_output=True, cwd=_GH_PAGES_WORKTREE)
            _run(["git", "rm", "-rf", "--quiet", "--ignore-unmatch", "."],
                 capture_output=True, cwd=_GH_PAGES_WORKTREE)

        add_paths = _copy_pages_into(_GH_PAGES_WORKTREE)
        _run(["git", "add", *add_paths], check=True, cwd=_GH_PAGES_WORKTREE)

        commit_msg = ("init: versions.json + snapshots" if not branch_exists
                      else "chore: update versions.json + snapshots")
        try:
            _run(["git", "commit", "-m", commit_msg],
                 check=True, capture_output=True, text=True, cwd=_GH_PAGES_WORKTREE)
        except subprocess.CalledProcessError as exc:
            if "nothing to commit" in (exc.stdout or "") or "nothing to commit" in (exc.stderr or ""):
                print("[INFO] versions.json + snapshots are already up to date.")
                return
            raise

        _run(["git", "push", "origin", "HEAD:gh-pages"], check=True, cwd=_GH_PAGES_WORKTREE)
        print("[OK] versions.json + snapshots pushed to gh-pages.")
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] Git operation failed: {exc}")
        if exc.stderr:
            print(f"Details: {exc.stderr.strip()}")
        print("       Make sure git is installed and you have push access to the repo.")
        raise
    finally:
        _cleanup_pages_worktree()


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
    if changes["added"] or changes["removed"] or changes["updated"]:
        return True

    # Client-visible override changes (custom mods, configs, etc.), ignoring
    # overrides that are flagged server-only.
    server_only_overrides = get_side_only_overrides("server")

    def client_overrides(commit_id: str) -> dict:
        return {
            path: digest
            for path, digest in load_override_manifest(commit_id).items()
            if path not in server_only_overrides
        }

    override_changes = diff_overrides(client_overrides(old_commit_id), client_overrides(new_commit_id))
    return bool(override_changes["added"] or override_changes["removed"] or override_changes["updated"])


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
    answer = input("Clear Windows icon cache so the new icon appears immediately? (Explorer will restart) [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("[INFO] Skipped icon cache clear — the new icon may not appear.")
        return
    cache_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Explorer"
    db_files  = [str(p) for p in cache_dir.glob("iconcache_*.db")]
    subprocess.run(["taskkill", "/f", "/im", "explorer.exe"], capture_output=True)
    for db_file in db_files:
        try:
            os.remove(db_file)
        except OSError:
            pass
    subprocess.Popen(["explorer.exe"])
    print("[OK] Icon cache cleared — new icon will now appear.")


def build_exe() -> None:
    """Build the baked updater exe from releases/{file_prefix}-client-updater.py."""
    if not bake_client_updater():
        sys.exit(1)
    if not _build_exe(_baked_client_updater_path()):
        sys.exit(1)


def _push_working_dir(version: str) -> bool:
    """
    Ensure .gitignore and README.md are up to date, then commit and push any
    changes to the working directory's git repo.

    If README.template.md does not exist it is created from the bundled
    README.example.md (downloading from the modpackctl repo if needed).
    README.md is then rendered fresh from the template. README.template.md
    itself is not committed — it is local to the maintainer.

    Returns True on success, False if the push failed.
    """
    print("Updating working directory...")
    _init_git_repo()

    if not README_TEMPLATE.exists():
        src = _ensure_example("README.example.md")
        if src:
            shutil.copy2(src, README_TEMPLATE)
            print(f"[OK] Created README.template.md — edit it to customise your README.")

    if README_TEMPLATE.exists():
        if render_readme(version):
            print("[OK] README.md rendered from template.")
    elif not README.exists():
        print("[WARN] No README.template.md or README.md found — skipping README.")

    files_to_stage = [str(GITIGNORE)]
    if README.exists():
        files_to_stage.append(str(README))
    _run(["git", "add", *files_to_stage], capture_output=True)

    try:
        _run(
            ["git", "commit", "-m", f"chore: publish v{version}"],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        if "nothing to commit" in output:
            print("[INFO] Working directory already up to date — nothing to commit.")
        else:
            print(f"[WARN] git commit failed: {(exc.stderr or '').strip()}")
            return False

    try:
        _run(["git", "push"], check=True)
        print("[OK] Working directory pushed.")
        return True
    except subprocess.CalledProcessError:
        print("[WARN] git push failed — run 'git push' manually if needed.")
        return False


def _create_github_release(version: str, message: str, user: str, repo: str) -> bool:
    """
    Create the GitHub Release for an already-built client release: upload the client zip,
    CurseForge zip, baked updater .py and .exe, with the client changelog as release notes.
    Returns True on success.
    """
    tag        = f"v{version}"
    notes_path = _get_notes_file_for_release(version, message=message, side="client")

    baked_updater_path = _baked_client_updater_path()
    baked_exe_path     = baked_updater_path.with_suffix(".exe")
    cf_zip_path        = RELEASES / f"{get_file_prefix()}-{version}-curseforge.zip"
    client_zip_path    = RELEASES / f"{get_file_prefix()}-{version}-client.zip"

    release_assets = [str(client_zip_path)]
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
        return True
    except subprocess.CalledProcessError:
        print("[ERROR] 'gh release create' failed.")
        print("        Make sure the GitHub CLI is installed: https://cli.github.com")
        print("        And that you're authenticated: gh auth login")
        return False
    finally:
        notes_path.unlink(missing_ok=True)


def publish(version: str, message: str = "") -> None:
    """
    Publish a committed version.

    With client-visible changes: build the client release (zip, CurseForge zip, baked
    updater .py + .exe via release_client), create a GitHub Release with the client
    changelog as notes, push the gh-pages branch, and push README/.gitignore.

    With no client-visible changes (only server-side data moved): skip the GitHub
    Release and just refresh the gh-pages branch so the server updater sees the new
    version, then report a partial publish.
    """
    if not REPO.exists():
        print("[ERROR] Repository not initialized. Run 'init' first.")
        sys.exit(1)

    user, repo = get_github_info()

    log_entry = get_log_entry(version)
    if not log_entry:
        print(f"[ERROR] Version '{version}' not found in log.")
        sys.exit(1)

    if not message:
        message = log_entry.get("message", "")
    elif message != log_entry.get("message", ""):
        set_version_message(version, message)

    has_client_changes = _has_client_changes(version)
    release_ok = False
    repo_ok    = True

    if has_client_changes:
        # release_client() builds the zips, bakes the updater, and refreshes gh-pages/.
        zip_path = release_client(version)
        if not zip_path or not zip_path.exists():
            print("[ERROR] Release build failed — cannot publish.")
            sys.exit(1)
        release_ok = _create_github_release(version, message, user, repo)
        repo_ok    = _push_working_dir(version)
    else:
        print(f"[INFO] v{version} has no client-visible changes (server-side only).")
        print("       Skipping the GitHub Release; refreshing gh-pages so the server updater sees it.")
        build_pages(show_hint=False)  # release_client didn't run, so build gh-pages/ here

    pages_ok = True
    try:
        _push_pages_assets()
    except Exception:
        pages_ok = False
        print("[WARN] Could not update gh-pages. Players won't see this version in the updater.")
        print("       Run 'python modpackctl.py build-pages' to generate the files locally,")
        print("       then push them to the gh-pages branch manually.")

    pages_url   = f"https://{user}.github.io/{repo}/"
    release_url = f"https://github.com/{user}/{repo}/releases/tag/v{version}"
    file_prefix = get_file_prefix()

    print(f"\n{'=' * 42}")
    if has_client_changes and release_ok and pages_ok and repo_ok:
        print(f" PUBLISH COMPLETE — v{version}")
    elif not has_client_changes and pages_ok:
        print(f" PUBLISH PARTIAL — v{version} (gh-pages only, no client release)")
    else:
        print(f" PUBLISH PARTIAL — v{version} (see errors above)")
    print(f"{'=' * 42}")
    if release_ok:
        print(f"  Release URL : {release_url}")
    print(f"  gh-pages    : {pages_url}")
    print(f"{'=' * 42}\n")
    if has_client_changes:
        print(f"  New players    : download {file_prefix}-{version}-client.zip (or -curseforge.zip) from the release page.")
        print(f"  Existing players: run {file_prefix}-client-updater.py (or .exe) from their current install.")
    else:
        print("  No client release was created — only server-side data changed.")
        print(f"  Server admins  : run {file_prefix}-server-updater.py to pull v{version}.")


def build_pages(show_hint: bool = True) -> None:
    """Write versions.json and snapshots/ to a local gh-pages/ folder for manual publishing."""
    if not REPO.exists():
        print("[ERROR] Repository not initialized. Run 'init' first.")
        sys.exit(1)
    dest = PAGES_OUTPUT
    dest.mkdir(parents=True, exist_ok=True)
    _write_pages_assets(dest)
    print(f"[OK] Built gh-pages assets → {dest}/")
    if show_hint:
        print("     Push the contents of this folder to your gh-pages branch.")


def _example_name_for(target: Path) -> str:
    """
    Return the bundled template filename for a working-copy target. Most targets follow
    the `name.example.ext` convention; the shared updater_common.py is shipped verbatim
    (no `.example`) so its `from updater_common import *` import resolves in templates/.
    """
    if target == UPDATER_COMMON:
        return target.name
    return f"{target.stem}.example{target.suffix}"


def _ensure_files(*targets: Path) -> None:
    """
    For each target, copy its bundled template to the CWD if the working copy does not
    already exist. Works for the updater scripts, the shared updater_common.py, and the
    toml config.
    """
    for target in targets:
        if target.exists():
            print(f"[INFO] Using existing {target.name}")
        else:
            example_name = _example_name_for(target)
            source = _ensure_example(example_name)
            if source is None:
                print(f"[ERROR] Could not obtain {example_name} — skipping {target.name}.")
                continue
            shutil.copy2(source, target)
            print(f"[INFO] Copied {example_name} → {target.name} — you can customise it for this modpack.")


def reset_file(client: bool = False, server: bool = False, config: bool = False,
               common: bool = False, all_files: bool = False) -> None:
    if all_files:
        client = server = config = common = True

    tasks: list[tuple[Path, str, bool]] = []
    if client:
        tasks.append((CLIENT_UPDATE_SCRIPT, _example_name_for(CLIENT_UPDATE_SCRIPT), False))
    if server:
        tasks.append((SERVER_UPDATE_SCRIPT, _example_name_for(SERVER_UPDATE_SCRIPT), False))
    if common:
        tasks.append((UPDATER_COMMON, _example_name_for(UPDATER_COMMON), False))
    if config:
        tasks.append((CONFIG_FILE, CONFIG_EXAMPLE.name, True))

    for target, example_name, is_config in tasks:
        if target.exists():
            if is_config:
                prompt = f"Overwrite {target.name} with {example_name}? THIS WILL ERASE YOUR CURRENT CONFIG. [y/N] "
            else:
                prompt = f"Delete {target.name} and replace with {example_name} from repo? [y/N] "
            answer = input(prompt).strip().lower()
            if answer not in ("y", "yes"):
                print(f"[INFO] Skipped {target.name}.")
                continue
        else:
            # Nothing to overwrite — just create it from the template (no prompt).
            print(f"[INFO] {target.name} does not exist — creating it from {example_name}.")

        src = _ensure_example(example_name)
        if src is None:
            print(f"[ERROR] Could not obtain {example_name}.")
            sys.exit(1)
        shutil.copy2(src, target)
        print(f"[OK] Reset {target.name} from {example_name}.")


_HEX_COLOUR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_COLOUR_KEY_DEFAULTS: dict[str, str] = {
    "DARK_BG":   "#1e1e2e",
    "PANEL_BG":  "#2a2b3c",
    "ACCENT":    "#89b4fa",
    "ACCENT_HV": "#74a0e8",
    "ACCENT2":   "#3b3d52",
    "TEXT":      "#cdd6f4",
    "TEXT_DIM":  "#8b92b0",
    "RED":       "#f38ba8",
    "GREEN":     "#a6e3a1",
    "YELLOW":    "#f9e2af",
    "KONAMI":    "#cba6f7",
}


def _validate_and_serialize_colours(settings: dict) -> str:
    """
    Validate [settings.colours] from modpackctl.toml and return the JSON string
    that gets baked into the client updater's __COLOUR_DEFAULTS_JSON__ placeholder.

    The section is optional — when absent, every colour falls back to its hardcoded
    default. When present, any key that's specified must be a known colour name with
    a valid '#rrggbb' value; unknown keys or malformed values exit the program.
    """
    colours_section = settings.get("colours")
    result = dict(_COLOUR_KEY_DEFAULTS)
    if colours_section is None:
        return json.dumps(result)
    if not isinstance(colours_section, dict):
        print("[ERROR] [settings.colours] must be a TOML table, got: "
              f"{type(colours_section).__name__}")
        sys.exit(1)
    unknown = sorted(set(colours_section) - set(_COLOUR_KEY_DEFAULTS))
    if unknown:
        print(f"[ERROR] Unknown colour key(s) in [settings.colours]: {unknown}")
        print(f"        Valid keys: {sorted(_COLOUR_KEY_DEFAULTS)}")
        sys.exit(1)
    for key, value in colours_section.items():
        if not isinstance(value, str) or not _HEX_COLOUR_RE.match(value):
            print(f"[ERROR] [settings.colours].{key} must be a '#rrggbb' hex string, "
                  f"got: {value!r}")
            sys.exit(1)
        result[key] = value
    return json.dumps(result)


def _validate_beat_drop(settings: dict) -> float:
    """Read settings.beat_drop, validating it's a non-negative number. Defaults to 44.0."""
    value = settings.get("beat_drop", 44.0)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        print(f"[ERROR] settings.beat_drop must be a non-negative number, got: {value!r}")
        sys.exit(1)
    return float(value)


def _validate_rainbow_bpm(settings: dict) -> float:
    """Read settings.rainbow_bpm, validating it's a positive number. Defaults to 113.0."""
    value = settings.get("rainbow_bpm", 113.0)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        print(f"[ERROR] settings.rainbow_bpm must be a positive number, got: {value!r}")
        sys.exit(1)
    return float(value)


_INLINE_MARKER_RE = re.compile(r'^from updater_common import \*.*$', re.MULTILINE)


def _inline_common(specific_src: str) -> str:
    """
    Inline the working updater_common.py into an updater script by replacing its
    `from updater_common import *` line with the shared module's body, so the baked
    output is one self-contained file. The shared module's leading docstring and its
    `from __future__` import are stripped — the host script already provides
    `from __future__ import annotations` as its first statement.
    """
    common_src = UPDATER_COMMON.read_text(encoding="utf-8")
    common_src = re.sub(r'\A\s*""".*?"""\s*', "", common_src, count=1, flags=re.DOTALL)
    common_src = re.sub(r'^from __future__ import annotations[^\n]*\n', "", common_src,
                        count=1, flags=re.MULTILINE)
    common_body = common_src.strip("\n")
    if not _INLINE_MARKER_RE.search(specific_src):
        return specific_src  # no marker (e.g. a legacy single-file template) — leave as-is
    # Function replacement avoids backslash interpretation of the common body.
    return _INLINE_MARKER_RE.sub(lambda _match: common_body, specific_src, count=1)


def bake_client_updater() -> bool:
    """
    Inline updater_common.py and substitute config placeholders in client-updater.py,
    writing the result to releases/{file_prefix}-client-updater.py. Returns False if the
    template is not present.

    Supported placeholders (written as bare Python string literals in client-updater.example.py):
      __GITHUB_USER__          — GitHub username from modpackctl.toml
      __GITHUB_REPO__          — GitHub repo name from modpackctl.toml
      __MODPACK_NAME__         — settings.modpack_name from modpackctl.toml
      __LOGO_URL__             — logo URL from modpackctl.toml (empty string if unset)
      __SECRET_VIDEO_URL__     — easter egg video URL (defaults to Never Gonna Give You Up)
      __ENABLE_SECRET__        — True/False; settings.enable_secret (default: True)
      __ENABLE_RAINBOW__       — True/False; settings.enable_rainbow (default: False)
      __RAINBOW_BPM__          — float BPM; settings.rainbow_bpm (default: 113.0)
      __BEAT_DROP_SECONDS__    — float seconds; settings.beat_drop (default: 44.0)
      __COLOUR_DEFAULTS_JSON__ — JSON dict of the 11 theme colours, validated against
                                 [settings.colours] in modpackctl.toml
    """
    _ensure_files(UPDATER_COMMON, CLIENT_UPDATE_SCRIPT)
    if not CLIENT_UPDATE_SCRIPT.exists():
        print(f"[WARN] {CLIENT_UPDATE_SCRIPT} not found — skipping updater bake.")
        return False
    if not UPDATER_COMMON.exists():
        print(f"[WARN] {UPDATER_COMMON} not found — skipping updater bake.")
        return False
    user, repo        = get_github_info()
    cfg               = load_config()
    settings          = cfg.get("settings", {})
    modpack_name      = settings.get("modpack_name", "")
    logo_url          = settings.get("logo_url", "")
    secret_video_url  = settings.get("secret_video_url", _DANCE_DEFAULT_URL)
    enable_secret     = settings.get("enable_secret",  True)
    enable_rainbow    = settings.get("enable_rainbow",  False)
    rainbow_bpm       = _validate_rainbow_bpm(settings)
    beat_drop         = _validate_beat_drop(settings)
    colour_json       = _validate_and_serialize_colours(settings)
    content = _inline_common(CLIENT_UPDATE_SCRIPT.read_text(encoding="utf-8"))
    content = content.replace('"__GITHUB_USER__"',          f'"{user}"')
    content = content.replace('"__GITHUB_REPO__"',          f'"{repo}"')
    content = content.replace('"__MODPACK_NAME__"',         f'"{modpack_name}"')
    content = content.replace('"__LOGO_URL__"',             f'"{logo_url}"')
    content = content.replace('"__SECRET_VIDEO_URL__"',     f'"{secret_video_url}"')
    content = content.replace('"__ENABLE_SECRET__"',        str(bool(enable_secret)))
    content = content.replace('"__ENABLE_RAINBOW__"',       str(bool(enable_rainbow)))
    content = content.replace('"__RAINBOW_BPM__"',          f'"{rainbow_bpm}"')
    content = content.replace('"__BEAT_DROP_SECONDS__"',    f'"{beat_drop}"')
    content = content.replace('"__COLOUR_DEFAULTS_JSON__"', repr(colour_json))
    dest_path = _baked_client_updater_path()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(content, encoding="utf-8")
    print(f"[OK] Baked {CLIENT_UPDATE_SCRIPT.name} → {dest_path}")
    return True


def bake_server_updater() -> bool:
    """
    Substitute config placeholders in server-updater.example.py and write the result to
    releases/{file_prefix}-server-updater.py. Returns False if the template is not present.

    Supported placeholders:
      __GITHUB_USER__  — GitHub username from modpackctl.toml
      __GITHUB_REPO__  — GitHub repo name from modpackctl.toml
      __MODPACK_NAME__ — settings.modpack_name from modpackctl.toml
    """
    _ensure_files(UPDATER_COMMON, SERVER_UPDATE_SCRIPT)
    if not SERVER_UPDATE_SCRIPT.exists():
        print(f"[WARN] {SERVER_UPDATE_SCRIPT} not found — skipping server updater bake.")
        return False
    if not UPDATER_COMMON.exists():
        print(f"[WARN] {UPDATER_COMMON} not found — skipping server updater bake.")
        return False
    user, repo   = get_github_info()
    cfg          = load_config()
    modpack_name = cfg.get("settings", {}).get("modpack_name", "")
    content = _inline_common(SERVER_UPDATE_SCRIPT.read_text(encoding="utf-8"))
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
    # Remove the override manifest too; blobs are content-addressed and may be
    # shared with other commits, so they are intentionally left in place.
    (SNAPSHOTS / f"{commit_id}.overrides.json").unlink(missing_ok=True)

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
    commit_version_group = parser_commit.add_mutually_exclusive_group()
    commit_version_group.add_argument("--major", action="store_true", help="Force a major version bump")
    commit_version_group.add_argument("--version", metavar="X.Y.Z", default=None, help="Set an explicit version (clean x.y.z, greater than the latest) instead of auto-bumping")
    parser_commit.add_argument("--message", metavar="MESSAGE", default="", help="Release note shown to players in the updater changelog")

    # log
    subparsers.add_parser("log", help="List all committed versions with diff stats")

    # remove-commit
    parser_remove = subparsers.add_parser("remove-commit", help="Permanently remove a committed version from history (irreversible)")
    parser_remove.add_argument("version", nargs="?", default=None, help="Version to remove (default: latest)")

    # set-message
    parser_set_message = subparsers.add_parser("set-message", help="Set or clear the release note for any committed version")
    parser_set_message.add_argument("version", nargs="?", default=None, help="Version to update (default: latest)")
    parser_set_message.add_argument("message", nargs="?", default="", help="New release note (omit to clear the existing message)")

    # changelog
    parser_changelog = subparsers.add_parser("changelog", help="Generate a Markdown changelog between two versions")
    parser_changelog.add_argument("v1", nargs="?", default=None, help="Starting version (default: latest)")
    parser_changelog.add_argument("v2", nargs="?", default=None, help="Ending version (omit to treat v1 as an initial release)")
    parser_changelog.add_argument("--out", default="changelog.md", metavar="FILE", help="Output file (default: changelog.md)")
    changelog_side = parser_changelog.add_mutually_exclusive_group()
    changelog_side.add_argument("--client", action="store_true", help="Client view (default): exclude server-only mods")
    changelog_side.add_argument("--server", action="store_true", help="Server view: exclude client-only mods, shaderpacks, and resourcepacks")
    parser_changelog.add_argument("--message", metavar="MESSAGE", default="", help="Optional note inserted below the heading")

    # release
    parser_release = subparsers.add_parser("release", help="Build a release zip (client by default)")
    parser_release.add_argument("version", nargs="?", default=None, help="Version to release (default: latest)")
    release_side = parser_release.add_mutually_exclusive_group()
    release_side.add_argument("--client", action="store_true", help="Build a client release (default)")
    release_side.add_argument("--server", action="store_true", help="Build a server release and bake server-updater (no exe)")

    # publish
    parser_publish = subparsers.add_parser("publish", help="Build a client release and publish to GitHub")
    parser_publish.add_argument("version", nargs="?", default=None, help="Version to publish (default: latest)")
    parser_publish.add_argument("--message", metavar="MESSAGE", default="", help="Release note (overrides the message set at commit time)")

    # update
    parser_update = subparsers.add_parser("update", help="Rebuild the build folder for a version without zipping")
    parser_update.add_argument("version", nargs="?", default=None, help="Version to build (default: latest)")
    update_side = parser_update.add_mutually_exclusive_group()
    update_side.add_argument("--client", action="store_true", help="Client view (default): exclude server-only mods")
    update_side.add_argument("--server", action="store_true", help="Server view: exclude client-only mods, shaderpacks, and resourcepacks")

    # purge
    parser_purge = subparsers.add_parser("purge", help="Remove old files from the download cache")
    parser_purge.add_argument("--all", action="store_true", help="Clear the entire cache instead of just stale files")

    # build-pages
    subparsers.add_parser("build-pages", help="Build versions.json + snapshots/ locally to gh-pages/")

    # push-pages
    subparsers.add_parser("push-pages", help="Push the existing gh-pages/ folder to the gh-pages branch")

    # render-readme
    parser_render_readme = subparsers.add_parser("render-readme", help="Render README.md from README.template.md for a version")
    parser_render_readme.add_argument("version", nargs="?", default=None, help="Version to render for (default: latest)")

    # bake-updater
    parser_bake = subparsers.add_parser("bake-updater", help="Bake the updater script with config values")
    bake_side = parser_bake.add_mutually_exclusive_group()
    bake_side.add_argument("--client", action="store_true", help="Bake client-updater.py (default)")
    bake_side.add_argument("--server", action="store_true", help="Bake server-updater.py instead")

    # reset-file
    parser_reset_tmpl = subparsers.add_parser("reset-file", help="Reset a template file in the current directory")
    parser_reset_file_group = parser_reset_tmpl.add_mutually_exclusive_group(required=True)
    parser_reset_file_group.add_argument("--client", action="store_true", help="Overwrite client-updater.py from the bundled example")
    parser_reset_file_group.add_argument("--server", action="store_true", help="Overwrite server-updater.py from the bundled example")
    parser_reset_file_group.add_argument("--common", action="store_true", help="Overwrite updater_common.py (shared updater helpers) from the bundled copy")
    parser_reset_file_group.add_argument("--config", action="store_true", help="Overwrite modpackctl.toml with modpackctl.example.toml")
    parser_reset_file_group.add_argument("--all", dest="all_files", action="store_true", help="Overwrite all updater files and the config")

    # build-exe
    subparsers.add_parser("build-exe", help="Build releases/client-updater.exe from the baked client updater")

    # export-cf
    parser_export_cf = subparsers.add_parser("export-cf", help="Build a CurseForge-format modpack zip for a version")
    parser_export_cf.add_argument("version", help="Version to export")

    if argcomplete:
        argcomplete.autocomplete(parser)
    args = parser.parse_args()

    if args.command == "init":
        init(args.zip, args.force)

    elif args.command == "commit":
        commit(args.zip, args.major, message=args.message, version_override=args.version)

    elif args.command == "log":
        show_log()

    elif args.command == "remove-commit":
        version = args.version
        if version is None:
            log = load_log()
            if not log:
                print("[ERROR] No committed versions found.")
                sys.exit(1)
            version = log[-1]["version"]
        remove_commit(version)

    elif args.command == "set-message":
        version = args.version
        message = args.message
        if version is not None and not args.message and not re.fullmatch(r"\d+\.\d+\.\d+", version):
            # Single positional arg that isn't a version number — treat it as the message
            message = version
            version = None
        if version is None:
            log = load_log()
            if not log:
                print("[ERROR] No committed versions found.")
                sys.exit(1)
            version = log[-1]["version"]
        if not get_log_entry(version):
            print(f"[ERROR] Version '{version}' not found in log.")
            sys.exit(1)
        set_version_message(version, message)
        if message:
            print(f"[OK] Message set for v{version}.")
        else:
            print(f"[OK] Message cleared for v{version}.")

    elif args.command == "changelog":
        v1 = args.v1
        v2 = args.v2
        if v1 is None:
            log = load_log()
            if not log:
                print("[ERROR] No committed versions found.")
                sys.exit(1)
            else:
                try:
                    v1 = log[-2]["version"]
                except IndexError:
                    v1 = None

            v2 = log[-1]["version"]
        elif v2 is None:
            v2 = v1
            v1 = None
        if args.server:
            cl_exclude            = get_filter_list("client_only")
            cl_exclude_categories = {"shaderpacks", "resourcepacks"}
        else:
            cl_exclude            = get_filter_list("server_only")
            cl_exclude_categories = None
        changelog(v1, v2, out=args.out, message=args.message,
                  exclude=cl_exclude, exclude_categories=cl_exclude_categories)

    elif args.command == "release":
        version = args.version
        if version is None:
            log = load_log()
            if not log:
                print("[ERROR] No committed versions found.")
                sys.exit(1)
            version = log[-1]["version"]
        if args.server:
            release_server(version)
        else:
            release_client(version)

    elif args.command == "publish":
        version = args.version
        if version is None:
            log = load_log()
            if not log:
                print("[ERROR] No committed versions found.")
                sys.exit(1)
            version = log[-1]["version"]
        publish(version, message=args.message)

    elif args.command == "update":
        version = args.version
        if version is None:
            log = load_log()
            if not log:
                print("[ERROR] No committed versions found.")
                sys.exit(1)
            version = log[-1]["version"]
        side    = "server" if args.server else "client"
        filters = _side_filters(side)
        update(version, exclude=filters["exclude"], exclude_categories=filters["exclude_categories"],
               suffix=side, exclude_overrides=filters["exclude_overrides"])

    elif args.command == "purge":
        purge_cache(args.all)

    elif args.command == "build-pages":
        build_pages()

    elif args.command == "push-pages":
        if not REPO.exists():
            print("[ERROR] Repository not initialized. Run 'init' first.")
            sys.exit(1)
        if not (PAGES_OUTPUT / "versions.json").exists():
            print(f"[ERROR] No built gh-pages assets in {PAGES_OUTPUT}/. Run 'build-pages' first.")
            sys.exit(1)
        _push_pages_assets()

    elif args.command == "render-readme":
        version = args.version
        if version is None:
            log = load_log()
            if not log:
                print("[ERROR] No committed versions found.")
                sys.exit(1)
            version = log[-1]["version"]
        if render_readme(version):
            print("[OK] README.md rendered from template.")
        else:
            print("[INFO] No README.template.md found — nothing to render.")

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

    elif args.command == "reset-file":
        reset_file(client=args.client, server=args.server, config=args.config,
                   common=args.common, all_files=args.all_files)

