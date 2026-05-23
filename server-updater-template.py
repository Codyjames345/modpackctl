"""
server-updater.py  —  Modpack Server Updater
Run this script on the server to check for and install modpack updates.

Requirements: Python 3.8+
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote, urlparse

# -------------------------
# CONFIG  (baked in at release time by modpackctl)
# -------------------------

GITHUB_USER  = "__GITHUB_USER__"
GITHUB_REPO  = "__GITHUB_REPO__"
MODPACK_NAME = "__MODPACK_NAME__"
VERSIONS_URL = f"https://{GITHUB_USER}.github.io/{GITHUB_REPO}/versions.json"

if "__" in GITHUB_USER or "__" in GITHUB_REPO:
    print(
        "[ERROR] server-updater.py has not been configured.\n"
        "Run 'python modpackctl.py bake-updater --server' to produce a configured copy."
    )
    sys.exit(1)

if "__" in MODPACK_NAME:
    MODPACK_NAME = GITHUB_REPO

HEADERS = {"User-Agent": f"{GITHUB_REPO}-server-updater/1.0"}


# -------------------------
# PREFS  (remembers the server directory between runs)
# -------------------------

def _prefs_dir() -> Path:
    """Return the per-user prefs directory for the updater."""
    return Path.home() / ".modpack-updater"


def _prefs_path() -> Path:
    """Return the prefs file path, namespaced per modpack (server variant)."""
    return _prefs_dir() / f"{GITHUB_USER}-{GITHUB_REPO}-server.json"


def load_prefs() -> dict:
    """Return saved prefs, or empty dict if missing/corrupt."""
    path = _prefs_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_prefs(data: dict) -> None:
    """Persist prefs to disk. Best-effort; failures are silently ignored."""
    path = _prefs_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


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
    url = (
        f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}"
        f"/gh-pages/snapshots/{commit_id}.json"
    )
    return fetch_json(url)


# -------------------------
# FILTERING
# -------------------------

def filter_for_server(snapshot: dict, client_only_ids: set[str]) -> dict:
    """
    Return a copy of snapshot with client-only mods and non-mods categories removed.
    client_only_ids is the set of project IDs that should never go on the server.
    """
    return {
        project_id: entry
        for project_id, entry in snapshot.items()
        if project_id not in client_only_ids
        and entry.get("category", "mods") == "mods"
    }


# -------------------------
# DIFF
# -------------------------

def diff_snapshots(old: dict, new: dict) -> dict:
    """
    Compute the difference between two snapshots.
    Returns dict with keys 'added', 'removed' (list of (pid, entry)) and
    'updated' (list of (pid, old_entry, new_entry)).
    """
    added = sorted(
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
# FILE OPERATIONS
# -------------------------

def locate_existing_file(project_id: str, entry: dict, mods_dir: Path) -> Path | None:
    """
    Find the on-disk file for a mod entry inside mods_dir.
    Tries exact filename first, then a substring match by project_id.
    """
    expected_filename = entry.get("file", "")
    if expected_filename:
        exact = mods_dir / expected_filename
        if exact.exists():
            return exact

    if mods_dir.is_dir():
        for file_path in mods_dir.iterdir():
            if project_id in file_path.stem:
                return file_path
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
            final_url  = response.url
            url_path   = urlparse(final_url).path
            filename   = os.path.basename(unquote(url_path))
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
# VERSION FILE
# -------------------------

_BCC_CONFIG_PATH = Path("config") / "bcc-common.toml"
_BCC_VERSION_RE  = re.compile(r'^([ \t]*modpackVersion\s*=\s*)"([^"]*)"', re.MULTILINE)
_BCC_NAME_RE     = re.compile(r'^([ \t]*modpackName\s*=\s*)"([^"]*)"',    re.MULTILINE)


def read_installed_version(server_dir: Path) -> str | None:
    """Return the modpackVersion from config/bcc-common.toml, or None if absent/unset."""
    bcc_path = server_dir / _BCC_CONFIG_PATH
    if not bcc_path.exists():
        return None
    match = _BCC_VERSION_RE.search(bcc_path.read_text(encoding="utf-8"))
    if not match:
        return None
    version = match.group(2)
    return version if version and version != "CHANGE_ME" else None


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


def write_installed_version(server_dir: Path, version: str) -> None:
    """Write modpackVersion (and modpackName) into config/bcc-common.toml."""
    bcc_path = server_dir / _BCC_CONFIG_PATH
    if not bcc_path.exists():
        bcc_path.parent.mkdir(parents=True, exist_ok=True)
        bcc_path.write_text(
            _BCC_TEMPLATE.format(name=MODPACK_NAME, version=version),
            encoding="utf-8",
        )
        return
    text = bcc_path.read_text(encoding="utf-8")
    text = _BCC_VERSION_RE.sub(rf'\g<1>"{version}"',      text)
    text = _BCC_NAME_RE.sub(   rf'\g<1>"{MODPACK_NAME}"', text)
    bcc_path.write_text(text, encoding="utf-8")


# -------------------------
# UPDATE PLAN
# -------------------------

def build_update_plan(old_snapshot: dict, new_snapshot: dict, mods_dir: Path) -> dict:
    """
    Build an ordered list of operations to migrate from old_snapshot to new_snapshot.
    Returns dict with:
      - 'download': [(project_id, file_id, display_name, is_update), ...]
      - 'delete':   [(Path, display_name), ...]
    """
    changes  = diff_snapshots(old_snapshot, new_snapshot)
    download: list = []
    delete:   list = []

    for project_id, old_entry in changes["removed"]:
        existing = locate_existing_file(project_id, old_entry, mods_dir)
        if existing:
            delete.append((existing, old_entry["name"]))

    for project_id, old_entry, new_entry in changes["updated"]:
        existing = locate_existing_file(project_id, old_entry, mods_dir)
        if existing:
            delete.append((existing, old_entry["name"]))
        download.append((project_id, new_entry["file_id"], new_entry["name"], True))

    for project_id, new_entry in changes["added"]:
        download.append((project_id, new_entry["file_id"], new_entry["name"], False))

    return {"download": download, "delete": delete}


# -------------------------
# DISPLAY HELPERS
# -------------------------

def print_changelog(old_snapshot: dict, new_snapshot: dict) -> None:
    """Print a human-readable changelog between two snapshots."""
    changes = diff_snapshots(old_snapshot, new_snapshot)

    if changes["added"]:
        print(f"\n  Added ({len(changes['added'])}):")
        for _, entry in changes["added"]:
            print(f"    + {entry['name']}")

    if changes["removed"]:
        print(f"\n  Removed ({len(changes['removed'])}):")
        for _, entry in changes["removed"]:
            print(f"    - {entry['name']}")

    if changes["updated"]:
        print(f"\n  Updated ({len(changes['updated'])}):")
        for _, old_entry, new_entry in changes["updated"]:
            print(f"    ~ {new_entry['name']}")

    total = len(changes["added"]) + len(changes["removed"]) + len(changes["updated"])
    if total == 0:
        print("\n  No changes.")


# -------------------------
# MAIN
# -------------------------

def main() -> None:
    """Entry point for the server updater CLI."""
    parser = argparse.ArgumentParser(
        prog="server-updater",
        description=f"{MODPACK_NAME} server updater — fetch and apply modpack updates.",
    )
    parser.add_argument(
        "server_dir",
        nargs="?",
        help="Path to the server directory (defaults to current directory).",
    )
    parser.add_argument(
        "--version",
        metavar="VERSION",
        help="Target version to install (defaults to latest).",
    )
    fresh_group = parser.add_mutually_exclusive_group()
    fresh_group.add_argument(
        "--fresh",
        dest="fresh",
        action="store_true",
        default=None,
        help="Wipe existing mods and re-download everything clean.",
    )
    fresh_group.add_argument(
        "--no-fresh",
        dest="fresh",
        action="store_false",
        help="Perform an incremental update (default unless no version is detected).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        metavar="N",
        help="Number of parallel download workers (default: 4).",
    )
    args = parser.parse_args()

    prefs = load_prefs()

    if args.server_dir:
        server_dir = Path(args.server_dir)
    else:
        remembered = prefs.get("last_server_dir")
        if remembered and Path(remembered).is_dir():
            prompt = f"Server directory [{remembered}]: "
        else:
            prompt = "Server directory: "
        try:
            entered = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(0)
        if not entered and remembered:
            server_dir = Path(remembered)
        elif entered:
            server_dir = Path(entered)
        else:
            print("[ERROR] No server directory provided.")
            sys.exit(1)

    if not server_dir.is_dir():
        print(f"[ERROR] Server directory does not exist: {server_dir}")
        sys.exit(1)

    mods_dir = server_dir / "mods"

    # ---- Fetch available versions ----
    print(f"Fetching version list from {VERSIONS_URL} ...")
    try:
        versions_data = fetch_versions()
    except Exception as error:
        print(f"[ERROR] Could not fetch versions.json: {error}")
        sys.exit(1)

    client_only_ids: set[str] = set(
        str(pid) for pid in versions_data.get("client_only_ids", [])
    )

    available_versions: list[dict] = versions_data.get("versions", [])
    if not available_versions:
        print("[ERROR] versions.json contains no versions.")
        sys.exit(1)

    latest_version = versions_data.get("latest") or available_versions[-1]["version"]
    target_version_str = args.version or latest_version

    target_entry = next(
        (entry for entry in available_versions if str(entry["version"]) == str(target_version_str)),
        None,
    )
    if target_entry is None:
        available = ", ".join(str(entry["version"]) for entry in available_versions)
        print(f"[ERROR] Version '{target_version_str}' not found. Available: {available}")
        sys.exit(1)

    target_version = str(target_entry["version"])
    target_commit  = target_entry["commit"]

    # ---- Detect installed version ----
    installed_version = read_installed_version(server_dir)

    # Smart fresh default: if nothing is installed, default to fresh
    fresh: bool = args.fresh if args.fresh is not None else (installed_version is None)

    # ---- Print plan summary ----
    print(f"\n{MODPACK_NAME} Server Updater")
    print("=" * 40)
    if installed_version:
        print(f"  Installed : {installed_version}")
    else:
        print(f"  Installed : (none detected)")
    print(f"  Target    : {target_version}")
    if fresh:
        print(f"  Mode      : fresh install")
    else:
        print(f"  Mode      : incremental update")

    if installed_version == target_version and not fresh:
        print(f"\n[OK] Already on version {target_version}. Nothing to do.")
        sys.exit(0)

    # ---- Fetch snapshots ----
    print(f"\nFetching snapshot for {target_version} ...")
    try:
        new_raw_snapshot = fetch_snapshot(target_commit)
    except Exception as error:
        print(f"[ERROR] Could not fetch snapshot for {target_version}: {error}")
        sys.exit(1)

    new_snapshot = filter_for_server(new_raw_snapshot, client_only_ids)

    if fresh:
        old_snapshot: dict = {}
    else:
        installed_entry = next(
            (entry for entry in available_versions if str(entry["version"]) == str(installed_version)),
            None,
        )
        if installed_entry is None:
            print(f"[WARN] Installed version '{installed_version}' not found in versions.json — treating as fresh install.")
            old_snapshot = {}
            fresh = True
        else:
            print(f"Fetching snapshot for installed version {installed_version} ...")
            try:
                old_raw_snapshot = fetch_snapshot(installed_entry["commit"])
            except Exception as error:
                print(f"[ERROR] Could not fetch snapshot for {installed_version}: {error}")
                sys.exit(1)
            old_snapshot = filter_for_server(old_raw_snapshot, client_only_ids)

    # ---- Show changelog ----
    print("\nChanges:")
    print_changelog(old_snapshot, new_snapshot)

    plan = build_update_plan(old_snapshot, new_snapshot, mods_dir)

    if not plan["download"] and not plan["delete"]:
        print("\n[OK] Nothing to change.")
        write_installed_version(server_dir, target_version)
        sys.exit(0)

    print(
        f"\n  {len(plan['download'])} file(s) to download,"
        f" {len(plan['delete'])} file(s) to remove."
    )

    # ---- Confirm ----
    if not args.yes:
        try:
            answer = input("\nProceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(0)
        if answer not in ("y", "yes"):
            print("Aborted.")
            sys.exit(0)

    # ---- Apply: download to temp, then atomic move ----
    mods_dir.mkdir(parents=True, exist_ok=True)

    failed_downloads: list[str] = []

    if plan["download"]:
        print(f"\nDownloading {len(plan['download'])} file(s) ...")
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp_dir = Path(tmp_str)
            downloaded: list[tuple[Path, str]] = []

            def _download_one(task: tuple) -> tuple[str | None, str]:
                project_id, file_id, display_name, _ = task
                local_path = download_mod_file(project_id, file_id, tmp_dir)
                return (str(local_path) if local_path else None, display_name)

            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {executor.submit(_download_one, task): task for task in plan["download"]}
                for future in as_completed(futures):
                    local_str, display_name = future.result()
                    if local_str:
                        print(f"  [+] {display_name}")
                        downloaded.append((Path(local_str), display_name))
                    else:
                        print(f"  [FAIL] {display_name}")
                        failed_downloads.append(display_name)

            if failed_downloads:
                print(f"\n[ERROR] {len(failed_downloads)} download(s) failed. Aborting — no files changed.")
                sys.exit(1)

            # All downloads succeeded — delete old files then move new ones in
            for file_path, display_name in plan["delete"]:
                try:
                    file_path.unlink()
                    print(f"  [-] {display_name}")
                except OSError as error:
                    print(f"  [WARN] Could not delete {file_path.name}: {error}")

            for src_path, display_name in downloaded:
                shutil.move(str(src_path), mods_dir / src_path.name)
    else:
        # Only deletions
        for file_path, display_name in plan["delete"]:
            try:
                file_path.unlink()
                print(f"  [-] {display_name}")
            except OSError as error:
                print(f"  [WARN] Could not delete {file_path.name}: {error}")

    write_installed_version(server_dir, target_version)
    prefs["last_server_dir"] = str(server_dir)
    save_prefs(prefs)
    print(f"\n[OK] Updated to {MODPACK_NAME} {target_version}.")


if __name__ == "__main__":
    main()
