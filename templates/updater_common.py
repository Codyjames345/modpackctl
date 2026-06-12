"""
updater_common.py — shared helpers for the modpack updaters.

The client (GUI) and server (CLI) updaters differ only by presentation; everything else
(network, diff, classification, the override-sync policy, version/bcc handling, …) lives
here so there is a single source of truth.

modpackctl inlines this module into each updater at bake time (replacing the
`from updater_common import *` line), so the distributed updaters remain single,
self-contained, stdlib-only files. This module is never imported at runtime in the
baked output; the import line exists only so editors can resolve the shared names.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlparse

# -------------------------
# CONFIG  (baked in at release time by modpackctl)
# -------------------------

GITHUB_USER  = "__GITHUB_USER__"
GITHUB_REPO  = "__GITHUB_REPO__"
MODPACK_NAME = "__MODPACK_NAME__"
VERSIONS_URL  = f"https://{GITHUB_USER}.github.io/{GITHUB_REPO}/versions.json"
SNAPSHOTS_URL = f"https://{GITHUB_USER}.github.io/{GITHUB_REPO}/snapshots"
# Override files are version-controlled per commit. The content for a commit lives at
# {OVERRIDES_URL}/{commit}.zip and its path->hash manifest at {SNAPSHOTS_URL}/{commit}.overrides.json.
OVERRIDES_URL = f"https://{GITHUB_USER}.github.io/{GITHUB_REPO}/overrides"

if "__" in GITHUB_USER or "__" in GITHUB_REPO:
    print(
        "[ERROR] This updater has not been configured.\n"
        "Download a configured copy from the modpack's GitHub Releases page,\n"
        "or run 'python modpackctl.py publish' (client) / 'bake-updater --server' to produce one."
    )
    sys.exit(1)

if "__" in MODPACK_NAME:
    MODPACK_NAME = GITHUB_REPO

HEADERS = {"User-Agent": f"{GITHUB_REPO}-updater/1.0"}


# -------------------------
# PREFS  (remembers settings between runs; suffix namespaces client vs server)
# -------------------------

def _prefs_dir() -> Path:
    """Return the per-user prefs directory for the updaters."""
    return Path.home() / ".modpack-updater"


def prefs_path(suffix: str = "") -> Path:
    """Return the prefs file path, namespaced per modpack (and per side via suffix)."""
    return _prefs_dir() / f"{GITHUB_USER}-{GITHUB_REPO}{suffix}.json"


def load_prefs(suffix: str = "") -> dict:
    """Return saved prefs, or an empty dict if missing/corrupt."""
    path = prefs_path(suffix)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_prefs(data: dict, suffix: str = "") -> None:
    """Persist prefs to disk. Best-effort; failures are silently ignored."""
    path = prefs_path(suffix)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


# -------------------------
# INSTALLED VERSION  (Better Compatibility Checker's config/bcc-common.toml)
# -------------------------

BCC_CONFIG_PATH = Path("config") / "bcc-common.toml"
_BCC_VERSION_RE  = re.compile(r'^([ \t]*modpackVersion\s*=\s*)"([^"]*)"', re.MULTILINE)
_BCC_NAME_RE     = re.compile(r'^([ \t]*modpackName\s*=\s*)"([^"]*)"',    re.MULTILINE)

_BCC_TEMPLATE = """\
#General settings
[general]
\t#The name of the modpack
\tmodpackName = "{name}"
\t#The version of the modpack
\tmodpackVersion = "{version}"
\t#Use the metadata.json to determine the modpack version
\t#ONLY ENABLE THIS IF YOU KNOW WHAT YOU ARE DOING
\tuseMetadata = false
"""


def read_installed_version(install_dir: Path) -> str | None:
    """Return the modpackVersion from config/bcc-common.toml, or None if absent/unset."""
    bcc_path = install_dir / BCC_CONFIG_PATH
    if not bcc_path.exists():
        return None
    match = _BCC_VERSION_RE.search(bcc_path.read_text(encoding="utf-8"))
    if not match:
        return None
    version = match.group(2)
    return version if version and version != "CHANGE_ME" else None


def bare_version(version: str | None) -> str:
    """
    Normalise a stored modpackVersion to a bare 'x.y.z' string, or '?' if absent/malformed.
    modpackName is a separate bcc field, so modpackVersion holds only the number. The legacy
    'MODPACK_NAME - x.y.z' format (and a stray leading 'v') is still accepted for old installs.
    """
    if not version:
        return "?"
    prefix = f"{MODPACK_NAME} - "
    if version.startswith(prefix):
        version = version[len(prefix):]
    version = version.strip().lstrip("vV").strip()
    return version if re.fullmatch(r"\d+(\.\d+)*", version) else "?"


def display_version(version: str | None) -> str:
    """Format a version for display: 'v1.2.0', or '?' if absent/malformed."""
    bare = bare_version(version)
    return "?" if bare == "?" else f"v{bare}"


def write_installed_version(install_dir: Path, version: str) -> None:
    """Write modpackVersion (bare 'x.y.z') and modpackName into config/bcc-common.toml."""
    bcc_path = install_dir / BCC_CONFIG_PATH
    bare = str(version)
    if not bcc_path.exists():
        bcc_path.parent.mkdir(parents=True, exist_ok=True)
        bcc_path.write_text(
            _BCC_TEMPLATE.format(name=MODPACK_NAME, version=bare),
            encoding="utf-8",
        )
        return
    text = bcc_path.read_text(encoding="utf-8")
    text = _BCC_VERSION_RE.sub(rf'\g<1>"{bare}"', text)
    text = _BCC_NAME_RE.sub(   rf'\g<1>"{MODPACK_NAME}"', text)
    bcc_path.write_text(text, encoding="utf-8")


# -------------------------
# NETWORK
# -------------------------

def fetch_json(url: str) -> dict:
    """GET a URL and return its parsed JSON body."""
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_versions() -> dict:
    """Fetch versions.json from gh-pages."""
    return fetch_json(VERSIONS_URL)


def fetch_snapshot(commit_id: str) -> dict:
    """Fetch a snapshot from gh-pages."""
    return fetch_json(f"{SNAPSHOTS_URL}/{commit_id}.json")


def fetch_override_manifest(commit_id: str) -> dict:
    """
    Fetch a commit's override manifest (path -> sha256 hex) from gh-pages.
    Returns {} when the commit has no overrides or the manifest is unavailable
    (e.g. a pack published before override versioning existed).
    """
    try:
        return fetch_json(f"{SNAPSHOTS_URL}/{commit_id}.overrides.json")
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return {}


def fetch_overrides_zip(commit_id: str) -> bytes | None:
    """Download a commit's overrides zip from gh-pages. Returns bytes, or None if unavailable."""
    try:
        request = urllib.request.Request(f"{OVERRIDES_URL}/{commit_id}.zip", headers=HEADERS)
        with urllib.request.urlopen(request, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        return None
    except (urllib.error.URLError, OSError, TimeoutError):
        return None


def download_mod_file(project_id: str, file_id: str, dest_dir: Path) -> Path | None:
    """
    Download a single CurseForge mod file into dest_dir.
    Returns the local path on success, or None on failure.
    """
    url = f"https://www.curseforge.com/api/v1/mods/{project_id}/files/{file_id}/download"
    request = urllib.request.Request(url, headers=HEADERS)

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            final_url    = response.url
            url_path     = urlparse(final_url).path
            filename     = os.path.basename(unquote(url_path))
            if not filename or "." not in filename:
                content_type = response.headers.get("Content-Type", "")
                extension    = ".zip" if "zip" in content_type else ".jar"
                filename     = f"{project_id}-{file_id}{extension}"

            local_path = dest_dir / filename
            with open(local_path, "wb") as fh:
                shutil.copyfileobj(response, fh)
            return local_path
    except (urllib.error.URLError, OSError, TimeoutError):
        return None


# -------------------------
# FILTERING
# -------------------------

def filter_snapshot(snapshot: dict, exclude_ids: set[str], mods_only: bool = False) -> dict:
    """
    Return a copy of snapshot with the given project IDs removed. When mods_only is True
    (server side), non-mod categories (shaderpacks, resourcepacks) are dropped too.
    Client side passes mods_only=False; server side passes mods_only=True.
    """
    if not exclude_ids and not mods_only:
        return snapshot
    return {
        project_id: entry
        for project_id, entry in snapshot.items()
        if project_id not in exclude_ids
        and (not mods_only or (entry.get("category") or "mods") == "mods")
    }


# -------------------------
# DIFF
# -------------------------

def diff_snapshots(old: dict, new: dict) -> dict:
    """
    Compute the difference between two enriched snapshots.
    Returns dict with keys 'added', 'removed' (list of (pid, entry)) and
    'updated' (list of (pid, old_entry, new_entry)).
    """
    added   = sorted(
        ((project_id, new[project_id]) for project_id in new if project_id not in old),
        key=lambda pair: pair[1]["name"].lower(),
    )
    removed = sorted(
        ((project_id, old[project_id]) for project_id in old if project_id not in new),
        key=lambda pair: pair[1]["name"].lower(),
    )
    updated_unsorted = [
        (project_id, old[project_id], new[project_id])
        for project_id in set(old) & set(new)
        if old[project_id]["file_id"] != new[project_id]["file_id"]
    ]
    updated = sorted(updated_unsorted, key=lambda triple: triple[2]["name"].lower())
    return {"added": added, "removed": removed, "updated": updated}


# -------------------------
# FILE CLASSIFICATION
# -------------------------

# Folders a mod file may live in. Lookups search all three so existing files are found
# wherever they are; the server only ever installs mods (see filter_snapshot).
INSTALL_CATEGORIES = ("mods", "shaderpacks", "resourcepacks")


def classify_downloaded_file(path: Path) -> str:
    """Inspect a downloaded file and return 'mods', 'shaderpacks', or 'resourcepacks'."""
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


def locate_existing_file(project_id: str, entry: dict, install_dir: Path) -> Path | None:
    """
    Find the on-disk file for a mod entry under install_dir. Prefers an exact filename
    match in the entry's expected category, then falls back to all category folders,
    then to a substring match by project_id.
    """
    expected_category = entry.get("category", "mods")
    expected_filename = entry.get("file", "")

    if expected_filename:
        exact = install_dir / expected_category / expected_filename
        if exact.exists():
            return exact
        for category in INSTALL_CATEGORIES:
            candidate = install_dir / category / expected_filename
            if candidate.exists():
                return candidate

    for category in INSTALL_CATEGORIES:
        category_dir = install_dir / category
        if not category_dir.is_dir():
            continue
        for file_path in category_dir.iterdir():
            if project_id in file_path.stem:
                return file_path
    return None


# -------------------------
# OVERRIDES
# -------------------------

def extract_override_members(
    install_dir: Path,
    zip_bytes: bytes,
    paths: list[str],
    on_progress=None,
    on_error=None,
) -> list[str]:
    """
    Extract the given member paths from the override zip into install_dir, overwriting
    any existing files. The caller (the version-aware override planner) decides which
    paths to write, so this is unconditional — it never skips existing files.
    on_progress, if given, is called as on_progress(current, total) after every member.
    on_error, if given, is called as on_error(path, exception) for OSError failures and
    extraction continues; if not given, OSError propagates and aborts the extraction.
    Returns a list of relative paths that were written.
    """
    wanted = set(paths)
    applied: list[str] = []
    install_root = install_dir.resolve()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        members = [m for m in zf.infolist() if not m.is_dir() and m.filename in wanted]
        total = len(members)
        for i, member in enumerate(members, 1):
            dest_path = install_dir / member.filename
            # Zip-slip guard: never extract a member whose resolved destination
            # escapes install_dir (e.g. '../' components or an absolute path).
            try:
                dest_path.resolve().relative_to(install_root)
            except ValueError:
                exc = OSError(f"unsafe path outside install directory: {member.filename}")
                if on_error is None:
                    raise exc
                on_error(member.filename, exc)
                if on_progress is not None:
                    on_progress(i, total)
                continue
            try:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(dest_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                applied.append(member.filename)
            except OSError as exc:
                if on_error is None:
                    raise
                on_error(member.filename, exc)
            if on_progress is not None:
                on_progress(i, total)
    return applied


def get_override_entries(override_zip: bytes) -> dict[str, list[str]]:
    """
    Return a dict mapping top-level groups in the overrides zip to the file paths within.
    Files at the zip root are grouped under "" (empty string). Sub-folder files are
    grouped under their top-level folder name; values are paths relative to that group.
    """
    grouped: dict[str, list[str]] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(override_zip)) as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                head, sep, tail = member.filename.partition("/")
                if sep and head:
                    grouped.setdefault(head, []).append(tail)
                else:
                    grouped.setdefault("", []).append(member.filename)
    except zipfile.BadZipFile:
        pass
    return grouped


def get_override_folders(override_zip: bytes) -> list[str]:
    """Return the sorted top-level folder names contained in the overrides zip."""
    return sorted(folder for folder in get_override_entries(override_zip) if folder)


def collect_wipe_targets(install_dir: Path, folder_names: list[str]) -> list[tuple[Path, str]]:
    """
    For each folder name in folder_names, walk install_dir/folder_name and collect every file.
    Returns a list of (file_path, display_name) tuples where display_name is the filename.
    """
    targets: list[tuple[Path, str]] = []
    for folder_name in folder_names:
        folder_path = install_dir / folder_name
        if not folder_path.is_dir():
            continue
        for file_path in folder_path.rglob("*"):
            if file_path.is_file():
                targets.append((file_path, file_path.name))
    return targets


def group_deletes_by_folder(
    deletes: list[tuple[Path, str]],
    install_dir: Path,
) -> dict[str, list[str]]:
    """Group delete entries by their top-level folder under install_dir."""
    grouped: dict[str, list[str]] = {}
    for file_path, _ in deletes:
        try:
            rel = file_path.relative_to(install_dir)
            parts = rel.parts
            folder = parts[0] if len(parts) > 1 else ""
            sub = "/".join(parts[1:]) if len(parts) > 1 else file_path.name
        except ValueError:
            folder = ""
            sub = file_path.name
        grouped.setdefault(folder, []).append(sub)
    return grouped


def compute_override_ops(
    old_manifest: dict,
    new_manifest: dict,
    install_dir: Path | None,
    fresh: bool,
    reset_overrides: bool,
    override_folders: list[str],
    category_dirs: list[str],
) -> dict:
    """
    Plan version-accurate override operations by diffing the previous and target commits'
    override manifests (path -> sha256). Returns a dict with:
      - 'added' / 'removed' / 'updated': {folder: [filename, ...]} for the changelog
        ("" folder key holds zip-root files), grouped like get_override_entries.
      - 'write':  list of zip member paths to extract (overwriting).
      - 'delete': list of install-relative paths to delete.

    Policy:
      * Files under mods/ are custom mods and are fully synced — added, overwritten when
        their content changes, and deleted when dropped from the pack.
      * Other override files (configs, kubejs, etc.) default to preserve-edits: only
        missing files are written, and nothing is overwritten or deleted unless the user
        opts into a Reset (or a fresh install), which wipes and re-extracts.
      * Wiped folders (category_dirs on a fresh install, override_folders on a reset) are
        re-extracted here; their deletions are handled by the wipe.
    """
    added: dict[str, list[str]]   = {}
    removed: dict[str, list[str]] = {}
    updated: dict[str, list[str]] = {}
    write: list[str]              = []
    delete: list[str]             = []
    ops = {"added": added, "removed": removed, "updated": updated,
           "write": write, "delete": delete}
    if install_dir is None:
        return ops

    wiped: set[str] = set()
    if fresh:
        wiped.update(category_dirs)
    if reset_overrides:
        wiped.update(override_folders)

    for path in set(old_manifest) | set(new_manifest):
        top      = path.split("/", 1)[0] if "/" in path else ""
        folder   = top
        filename = path[len(top) + 1:] if top else path
        is_mod   = top == "mods"
        in_old   = path in old_manifest
        in_new   = path in new_manifest
        changed  = in_old and in_new and old_manifest[path] != new_manifest[path]
        exists_locally = (install_dir / path).exists()
        folder_wiped   = top in wiped

        if in_new:
            if is_mod:
                if not in_old:
                    write.append(path)
                    added.setdefault(folder, []).append(filename)
                elif changed:
                    write.append(path)
                    updated.setdefault(folder, []).append(filename)
                elif folder_wiped:
                    write.append(path)          # unchanged, but wiped — restore it
                    if fresh:
                        added.setdefault(folder, []).append(filename)
            else:
                if folder_wiped or not exists_locally:
                    write.append(path)
                    if not in_old:
                        added.setdefault(folder, []).append(filename)
                    elif changed:
                        updated.setdefault(folder, []).append(filename)
                    elif fresh:
                        added.setdefault(folder, []).append(filename)
                # else: preserve the player's existing copy (no write)
        else:
            # Dropped from the pack. Wiped folders are cleared by the wipe; otherwise only
            # custom mods are auto-removed (configs are left for the player).
            if is_mod and not folder_wiped and exists_locally:
                delete.append(path)
                removed.setdefault(folder, []).append(filename)

    return ops


# -------------------------
# UPDATE PLAN
# -------------------------

def build_update_plan(old_snapshot: dict, new_snapshot: dict, install_dir: Path) -> dict:
    """
    Build an ordered list of operations to migrate from old_snapshot to new_snapshot.
    Returns a dict with:
      - 'download': [(project_id, file_id, display_name, is_update), ...]
      - 'delete':   [(Path, display_name), ...]
    """
    changes  = diff_snapshots(old_snapshot, new_snapshot)
    download: list = []
    delete:   list = []

    for project_id, old_entry in changes["removed"]:
        existing = locate_existing_file(project_id, old_entry, install_dir)
        if existing:
            delete.append((existing, old_entry["name"]))

    for project_id, old_entry, new_entry in changes["updated"]:
        existing = locate_existing_file(project_id, old_entry, install_dir)
        if existing:
            delete.append((existing, old_entry["name"]))
        download.append((project_id, new_entry["file_id"], new_entry["name"], True))

    for project_id, new_entry in changes["added"]:
        download.append((project_id, new_entry["file_id"], new_entry["name"], False))

    return {"download": download, "delete": delete}
