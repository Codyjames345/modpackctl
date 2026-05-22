import json
import zipfile
import shutil
import requests
import hashlib
import time
import os
import sys
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
try:
    import tomllib          # stdlib from Python 3.11+
except ModuleNotFoundError:
    try:
        import importlib
        tomllib = importlib.import_module("tomli")
    except ModuleNotFoundError:
        print("[ERROR] Python 3.11+ is required, or install tomli: pip install tomli")
        sys.exit(1)
from pathlib import Path
from urllib.parse import unquote, urlparse

# -------------------------
# STORAGE
# -------------------------

REPO            = Path(".modpackctl")
SNAPSHOTS       = REPO / "snapshots"
LOG_FILE        = REPO / "log.json"
CACHE           = REPO / "mod_cache.json"    # project_id -> { name, files: { file_id: filename } }
INDEX_FILE      = REPO / "mod_index.json"    # project_id -> { file_id, file, category }
DL_CACHE        = REPO / "dl_cache"          # permanent jar store keyed by (project_id, file_id)
OVERRIDES_STORE = REPO / "overrides"
CONFIG_FILE     = Path("modpackctl.toml")

BUILD    = Path("build")
RELEASES = Path("releases")

CF_URL  = "https://api.cfwidget.com/{}"
HEADERS = {"User-Agent": "modpackctl/1.0"}

# -------------------------
# CONFIG
# -------------------------

DEFAULT_CONFIG = """\
[github]
user = "<yourName>"
repo = "<yourRepo>"

[settings]
modpack_prefix = "<YourModpackName>"

# List project IDs to exclude from client releases (server-side only mods)
server_only = []

# List project IDs to exclude from server releases (client-side only mods)
client_only = []
"""


def load_config() -> dict:
    """Load and return the TOML config, creating a default file if none exists."""
    if not CONFIG_FILE.exists():
        print(f"[WARN] Config file not found. Creating {CONFIG_FILE} with defaults...")
        CONFIG_FILE.write_text(DEFAULT_CONFIG, encoding="utf-8")
    with open(CONFIG_FILE, "rb") as fh:
        return tomllib.load(fh)


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


def get_modpack_prefix() -> str:
    """Return the modpack_prefix string used when naming release zips."""
    cfg = load_config()
    try:
        return cfg["settings"]["modpack_prefix"]
    except KeyError:
        print("[ERROR] Missing modpack_prefix in [settings]. Expected modpackctl.toml with:")
        print("  [settings]")
        print('  modpack_prefix = "YourModpackName"')
        sys.exit(1)


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


def load_json(path: Path, default: list | dict) -> list | dict:
    """Return parsed JSON from path, or default if the file does not exist."""
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path: Path, data: list | dict) -> None:
    """Write data as indented JSON to path, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def load_index() -> dict:
    """Return the mod index mapping project_id -> { file_id, file, category }."""
    return load_json(INDEX_FILE, {})


def save_index(data: dict) -> None:
    """Persist the mod index to disk."""
    save_json(INDEX_FILE, data)


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


def normalize(manifest: dict) -> dict:
    """Return a flat {project_id: file_id} mapping (both as strings) from a manifest."""
    return {
        str(mod_entry["projectID"]): str(mod_entry["fileID"])
        for mod_entry in manifest.get("files", [])
    }


def get_modloader_version(manifest: dict) -> str:
    """Return the primary modloader id string (e.g. 'neoforge-21.1.229'), or '' if absent."""
    loaders = manifest.get("minecraft", {}).get("modLoaders", [])
    for loader in loaders:
        if loader.get("primary", False):
            return loader.get("id", "")
    # Fall back to the first loader if none is marked primary
    return loaders[0].get("id", "") if loaders else ""


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
    return load_json(LOG_FILE, [])


def add_version(
    commit_id: str,
    version: str,
    added: int = 0,
    removed: int = 0,
    updated: int = 0,
    modloader: str = "",
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
    """
    old = {str(k): str(v) for k, v in old.items()}
    new = {str(k): str(v) for k, v in new.items()}

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
# INIT
# -------------------------


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
        for file_path in (LOG_FILE, INDEX_FILE):
            file_path.unlink(missing_ok=True)

    REPO.mkdir(parents=True, exist_ok=True)
    result = commit(source)
    if result:
        _, _, num_mods = result
        print(f"[OK] Repository initialized — {num_mods} mods tracked.")


# -------------------------
# COMMIT
# -------------------------


def commit(source: str, major: bool = False) -> tuple[str, str, int] | None:
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

    manifest      = load_manifest(source)
    mods          = normalize(manifest)
    new_modloader = get_modloader_version(manifest)
    commit_id     = hash_state(mods)

    log = load_log()

    if log and log[-1]["commit"] == commit_id:
        print("[INFO] No changes detected — nothing to commit.")
        return None

    old_snapshot  = load_snapshot(log[-1]["commit"]) if log else {}
    old_version   = log[-1]["version"] if log else ""
    old_modloader = log[-1].get("modloader", "") if log else ""

    changes = diff(old_snapshot, mods)

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

    save_snapshot(commit_id, mods)
    add_version(
        commit_id, version,
        added=len(changes["added"]),
        removed=len(changes["removed"]),
        updated=len(changes["updated"]),
        modloader=new_modloader,
    )

    if not old_version:
        print(f"[OK] Committed 1.0.0 — initial release ({commit_id})")
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
    if not changes["added"] and not changes["removed"] and not changes["updated"] and not modloader_changed:
        print("  (no mod changes)")

    override_count = store_overrides(source)
    if override_count:
        print(f"  {override_count} override file(s) stored.")

    return version, commit_id, len(mods)


# -------------------------
# CHANGELOG
# -------------------------


def changelog(v1: str, v2: str | None, out: str = "changelog.md", message: str = "") -> None:
    """
    Generate a Markdown changelog between two committed versions and write it to a file.
    When v2 is None, v1 is treated as an initial release and diffed against an empty state.
    Includes a Modloader section when the modloader id changed between the two versions.
    An optional message is inserted as a short paragraph below the heading.
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

    if v1 == "EMPTY":
        changes       = diff({}, load_snapshot(new_commit_id))
        header_title  = f"# Changelog: {v2} (Initial Release)"
        old_modloader = ""
    else:
        old_commit_id = get_commit(v1)
        if not old_commit_id:
            print(f"[ERROR] Version '{v1}' not found in log.")
            return
        changes       = diff(load_snapshot(old_commit_id), load_snapshot(new_commit_id))
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
        for index, (project_id, old_file_id, new_file_id) in enumerate(updated_list, 1):
            mod_name      = resolve_mod(project_id)
            old_file_name = resolve_file_name(project_id, old_file_id)
            new_file_name = resolve_file_name(project_id, new_file_id)
            print(f"  [~] {mod_name}: {old_file_name} → {new_file_name} ({index}/{len(updated_list)})")
            lines.append(f"- {mod_name}  _({old_file_name} → {new_file_name})_")
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
    exclude: set | None = None,
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

    snapshot      = load_snapshot(commit_id)
    existing_index = load_index()
    mods_to_build = {
        project_id: file_id
        for project_id, file_id in snapshot.items()
        if project_id not in excluded_ids
        and (
            not exclude_categories
            or existing_index.get(project_id, {}).get("category", "mods") not in exclude_categories
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

    with ThreadPoolExecutor(max_workers=5) as executor:
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

    index = load_index()
    for result in successful_results:
        index[result["project_id"]] = {
            "file_id":  result["file_id"],
            "file":     result["file"],
            "category": result["category"],
        }
    save_index(index)

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
    exclude: set | None = None,
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
    if not get_commit(version):
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
    pack_prefix = get_modpack_prefix()
    zip_name    = f"{pack_prefix}-{version}-{suffix}.zip" if suffix else f"{pack_prefix}-{version}.zip"
    zip_path    = RELEASES / zip_name

    print(f"\nBuilding release zip at {zip_path}...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(BUILD):
            for filename in files:
                file_path = Path(root) / filename
                zf.write(file_path, file_path.relative_to(BUILD))

    snapshot       = load_snapshot(get_commit(version))
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
    """Build a client release zip, excluding any mods in the server_only list."""
    excluded = get_filter_list("server_only")
    if not excluded:
        print("[WARN] No server_only list found in config — building full release.")
    else:
        print(f"[INFO] Excluding {len(excluded)} server-only mod(s).")
    return release(version, exclude=excluded, suffix="client")


def release_server(version: str) -> Path | None:
    """Build a server release zip, excluding client-only mods, shaderpacks, and resourcepacks."""
    excluded = get_filter_list("client_only")
    if not excluded:
        print("[WARN] No client_only list found in config — building full release.")
    else:
        print(f"[INFO] Excluding {len(excluded)} client-only mod(s).")
    return release(version, exclude=excluded, exclude_categories={"shaderpacks", "resourcepacks"}, suffix="server")


# -------------------------
# PUBLISH
# -------------------------


def _build_versions_json() -> dict:
    """Build the versions.json payload served from gh-pages for the player updater."""
    log = load_log()
    return {
        "latest": log[-1]["version"] if log else None,
        "versions": [
            {"version": entry["version"], "commit": entry["commit"], "time": entry["time"]}
            for entry in log
        ],
    }


def _get_notes_file_for_release(version: str, message: str = "") -> Path:
    """
    Generate a temporary Markdown changelog file for the given version.
    Diffs against the previous version if one exists, otherwise treats it as an initial release.
    The caller is responsible for deleting this file after use.
    """
    log          = load_log()
    prev_version = None
    for index in range(len(log) - 1, -1, -1):
        if str(log[index]["version"]) == str(version):
            prev_version = log[index - 1]["version"] if index > 0 else None
            break

    notes_path = Path(f".modpackctl_notes_{version}.md")
    if prev_version:
        print(f"Generating notes comparing {prev_version} → {version}...")
        changelog(prev_version, version, out=str(notes_path), message=message)
    else:
        changelog(version, None, out=str(notes_path), message=message)

    return notes_path


def _ensure_gh_pages_has_versions_json() -> None:
    """
    Push an updated versions.json to the gh-pages branch, creating the branch
    as an orphan if it does not yet exist. Uses a temporary git worktree to
    avoid switching the working branch.
    """
    tmp_path = Path(".modpackctl_versions_tmp.json")
    tmp_path.write_text(json.dumps(_build_versions_json(), indent=2))

    try:
        ls_result     = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", "gh-pages"],
            capture_output=True, text=True, check=True,
        )
        branch_exists = "gh-pages" in ls_result.stdout

        if not branch_exists:
            print("[INFO] Creating gh-pages branch...")
            subprocess.run(["git", "checkout", "--orphan", "gh-pages"], check=True)
            subprocess.run(["git", "rm", "-rf", "--cached", "."], check=True, capture_output=True)
            shutil.copy(tmp_path, "versions.json")
            subprocess.run(["git", "add", "versions.json"], check=True)
            subprocess.run(["git", "commit", "-m", "init: versions.json"], check=True)
            subprocess.run(["git", "push", "-u", "origin", "gh-pages"], check=True)
            subprocess.run(["git", "checkout", "-"], check=True)
        else:
            subprocess.run(["git", "worktree", "prune"], capture_output=True)

            worktree_path = Path(".gh-pages-worktree")
            if worktree_path.exists():
                try:
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(worktree_path)],
                        capture_output=True,
                    )
                except Exception:
                    pass
                if worktree_path.exists():
                    shutil.rmtree(worktree_path, ignore_errors=True)

            # Clean up any leftover temp branch from a previous failed run
            subprocess.run(["git", "branch", "-D", "gh-pages-temp"], capture_output=True)
            subprocess.run(["git", "fetch", "origin", "gh-pages"], check=True)
            subprocess.run(
                ["git", "worktree", "add", "-b", "gh-pages-temp",
                 str(worktree_path), "origin/gh-pages"],
                check=True, capture_output=True,
            )

            shutil.copy(tmp_path, worktree_path / "versions.json")
            subprocess.run(["git", "add", "versions.json"], check=True, cwd=worktree_path)

            try:
                subprocess.run(
                    ["git", "commit", "-m", "chore: update versions.json"],
                    check=True, capture_output=True, text=True, cwd=worktree_path,
                )
                subprocess.run(
                    ["git", "push", "origin", "HEAD:gh-pages"],
                    check=True, cwd=worktree_path,
                )
                print("[INFO] versions.json updated on gh-pages.")
            except subprocess.CalledProcessError as exc:
                if "nothing to commit" in (exc.stdout or "") or "nothing to commit" in (exc.stderr or ""):
                    print("[INFO] versions.json is already up to date.")
                else:
                    raise

            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree_path)], check=True
            )
            subprocess.run(["git", "branch", "-D", "gh-pages-temp"], capture_output=True)

        print("[OK] versions.json pushed to gh-pages.")
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] Git operation failed: {exc}")
        if exc.stderr:
            print(f"Details: {exc.stderr.strip()}")
        print("       Make sure git is installed and you have push access to the repo.")
        raise
    finally:
        tmp_path.unlink(missing_ok=True)


def publish(version: str, message: str = "") -> None:
    """
    Build a fresh client release zip, create a GitHub Release with the generated
    changelog as release notes, and push updated versions.json to gh-pages.
    An optional message is included at the top of the release notes.
    """
    if not REPO.exists():
        print("[ERROR] Repository not initialized. Run 'init' first.")
        sys.exit(1)

    user, repo = get_github_info()

    if not get_log_entry(version):
        print(f"[ERROR] Version '{version}' not found in log.")
        sys.exit(1)

    print(f"Building client release for v{version}...")
    zip_path = release_client(version)
    if not zip_path or not zip_path.exists():
        print("[ERROR] Release build failed — cannot publish.")
        sys.exit(1)

    notes_path = _get_notes_file_for_release(version, message=message)
    tag        = f"v{version}"

    print(f"Creating GitHub Release {tag}...")
    try:
        subprocess.run(
            [
                "gh", "release", "create", tag,
                str(zip_path),
                "--title", f"v{version}",
                "--notes-file", str(notes_path),
                "--repo", f"{user}/{repo}",
            ],
            check=True,
        )
        print(f"[OK] GitHub Release {tag} created.")
    except subprocess.CalledProcessError:
        print("[ERROR] 'gh release create' failed.")
        print("       Make sure the GitHub CLI is installed: https://cli.github.com")
        print("       And that you're authenticated: gh auth login")
        sys.exit(1)
    finally:
        notes_path.unlink(missing_ok=True)

    print("Updating versions.json on gh-pages...")
    try:
        _ensure_gh_pages_has_versions_json()
    except Exception:
        print("[WARN] Could not update gh-pages. Players won't see this version in the updater.")
        print("       You may need to push versions.json manually.")

    pages_url   = f"https://{user}.github.io/{repo}/versions.json"
    release_url = f"https://github.com/{user}/{repo}/releases/tag/{tag}"

    print(f"\n{'=' * 42}")
    print(f" PUBLISH COMPLETE — v{version}")
    print(f"{'=' * 42}")
    print(f"  Release URL  : {release_url}")
    print(f"  versions.json: {pages_url}")
    print(f"{'=' * 42}\n")
    print("[OK] Done. Share the release URL with your players.")
    print("     New players: download the zip from the release page.")
    print("     Existing players: run update.py from their current install.")


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
        f"{project_id}_{file_id}"
        for project_id, file_id in latest_snapshot.items()
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
        print(
            f"{entry['version']:<12} {entry['commit']:<12} {timestamp:<20}"
            f" {entry['added']:>6} {entry['removed']:>8} {entry['updated']:>8}"
        )


# -------------------------
# CLI
# -------------------------

USAGE = """
modpackctl — Minecraft modpack version control

Commands:
  init          <zip> [--force]                    Initialise repo from a CurseForge export zip
  commit        <zip> [--major]                    Record a new version from an updated zip
  log                                              List all committed versions
  changelog     <v1> <v2> [out] [--message "..."]  Write a changelog between two versions
  release       <version> [--client|--server]      Build a release zip
  publish       <version> [--message "..."]        Build client release + push to GitHub
  update        <version> [--client|--server]      Rebuild the build folder for a version
  purge         [--all]                            Remove old files from the download cache
  export-example                                   Write modpackctl.toml.example from the built-in defaults
""".strip()

if __name__ == "__main__":
    load_config()  # ensure modpackctl.toml exists on every run

    if len(sys.argv) < 2:
        print(USAGE)
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "init":
        if len(sys.argv) < 3:
            print("Usage: init <zip> [--force]")
            sys.exit(1)
        init(sys.argv[2], "--force" in sys.argv)

    elif cmd == "commit":
        if len(sys.argv) < 3:
            print("Usage: commit <zip> [--major]")
            sys.exit(1)
        commit(sys.argv[2], "--major" in sys.argv)

    elif cmd == "log":
        show_log()

    elif cmd == "changelog":
        if len(sys.argv) < 3:
            print("Usage: changelog <v2> [output.md]")
            print("   or: changelog <v1> <v2> [output.md]")
            sys.exit(1)

        cl_message = ""
        if "--message" in sys.argv:
            message_index = sys.argv.index("--message")
            if message_index + 1 < len(sys.argv):
                cl_message = sys.argv[message_index + 1]
        clean_args = [arg for arg in sys.argv[3:] if arg != "--message" and arg != cl_message]

        if not clean_args:
            changelog(sys.argv[2], v2=None, out="changelog.md", message=cl_message)
        elif len(clean_args) == 1:
            if clean_args[0].lower().endswith(".md"):
                changelog(sys.argv[2], v2=None, out=clean_args[0], message=cl_message)
            else:
                changelog(sys.argv[2], clean_args[0], out="changelog.md", message=cl_message)
        else:
            changelog(sys.argv[2], clean_args[0], out=clean_args[1], message=cl_message)

    elif cmd == "release":
        if len(sys.argv) < 3:
            print("Usage: release <version> [--client|--server]")
            sys.exit(1)
        if "--client" in sys.argv:
            release_client(sys.argv[2])
        elif "--server" in sys.argv:
            release_server(sys.argv[2])
        else:
            release(sys.argv[2])

    elif cmd == "publish":
        if len(sys.argv) < 3:
            print("Usage: publish <version> [--message \"...\"]")
            sys.exit(1)
        pub_message = ""
        if "--message" in sys.argv:
            message_index = sys.argv.index("--message")
            if message_index + 1 < len(sys.argv):
                pub_message = sys.argv[message_index + 1]
        publish(sys.argv[2], message=pub_message)

    elif cmd == "update":
        if len(sys.argv) < 3:
            print("Usage: update <version> [--client|--server]")
            sys.exit(1)
        if "--client" in sys.argv:
            excluded = get_filter_list("server_only")
            update(sys.argv[2], exclude=excluded, suffix="client")
        elif "--server" in sys.argv:
            excluded = get_filter_list("client_only")
            update(sys.argv[2], exclude=excluded, exclude_categories={"shaderpacks", "resourcepacks"}, suffix="server")
        else:
            update(sys.argv[2])

    elif cmd == "purge":
        purge_cache("--all" in sys.argv)

    elif cmd == "export-example":
        example_path = Path("modpackctl.toml.example")
        example_path.write_text(DEFAULT_CONFIG, encoding="utf-8")
        print(f"[OK] Written to {example_path}")

    else:
        print(f"Unknown command: {cmd}\n")
        print(USAGE)
        sys.exit(1)
