"""
server-updater.py  —  Modpack Server Updater
Run this script on the server to check for and install modpack updates.

Requirements: Python 3.8+
"""

from __future__ import annotations

import argparse
try:
    import argcomplete
except ModuleNotFoundError:
    argcomplete = None  # type: ignore[assignment]
import io
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote, urlparse

# -------------------------
# CONFIG  (shared config is in updater_common; server-specific bits below)
# -------------------------

from updater_common import *  # noqa: F403  (inlined at bake time by modpackctl)

# The server installs mods only (shaderpacks/resourcepacks are stripped by filter_snapshot);
# this drives the fresh-install wipe scope.
CATEGORY_DIRS: list[str] = ["mods"]

# Override files under these top-level folders are client-only content and are never
# installed on the server (mirrors filter_snapshot's mods_only behaviour for CF mods).
NON_SERVER_OVERRIDE_DIRS: set[str] = {"shaderpacks", "resourcepacks"}


# -------------------------
# DISPLAY HELPERS
# -------------------------

def print_grouped_tree(grouped: dict[str, list[str]], names: dict | None = None) -> None:
    """
    Print a folder-grouped tree: 'folder/' headers with '|_ name' leaves. Entries with
    an empty folder key (zip-root files) print as flat '- name' lines. names, if given,
    maps a full 'folder/file' path to a display name (used for custom override mods).
    """
    names = names or {}
    for folder in sorted(grouped):
        files = sorted(grouped[folder], key=str.lower)
        if not files:
            continue
        if folder:
            print(f"    {folder}/")
            for name in files:
                print(f"      |_ {names.get(f'{folder}/{name}', name)}")
        else:
            for name in files:
                print(f"    - {names.get(name, name)}")


def merge_grouped(*groups: dict) -> dict[str, list[str]]:
    """Combine several {folder: [name]} dicts into one."""
    out: dict[str, list[str]] = {}
    for group in groups:
        for folder, files in group.items():
            out.setdefault(folder, []).extend(files)
    return out


def print_delete_tree(deletes: list[tuple[Path, str]], install_dir: Path, names: dict | None = None) -> None:
    """Print a folder-grouped tree of files queued for deletion (group_deletes_by_folder is shared)."""
    print_grouped_tree(group_deletes_by_folder(deletes, install_dir), names)


def group_downloads_by_category(downloads: list, new_snapshot: dict, is_update_wanted: bool) -> dict[str, list[str]]:
    """
    Group CurseForge mod downloads by their destination folder (the snapshot category;
    always mods/ on the server, since shaderpacks/resourcepacks are filtered out).
    """
    grouped: dict[str, list[str]] = {}
    for project_id, _file_id, name, is_upd in downloads:
        if is_upd != is_update_wanted:
            continue
        category = (new_snapshot.get(project_id) or {}).get("category") or "mods"
        grouped.setdefault(category, []).append(name)
    return grouped


def print_changelog(
    old_snapshot: dict,
    new_snapshot: dict,
    fresh: bool = False,
    install_dir: Path | None = None,
    plan: dict | None = None,
    override_ops: dict | None = None,
    custom_mod_names: dict | None = None,
) -> None:
    """
    Print a human-readable changelog. CurseForge mods and override files are grouped
    together by destination folder (mods/, config/, …), mirroring the client updater.
    """
    plan             = plan or {"download": [], "delete": []}
    override_ops     = override_ops or {}
    custom_mod_names = custom_mod_names or {}
    ov_added   = override_ops.get("added", {})
    ov_removed = override_ops.get("removed", {})
    ov_updated = override_ops.get("updated", {})

    def count(grouped: dict) -> int:
        return sum(len(v) for v in grouped.values())

    cf_added   = group_downloads_by_category(plan["download"], new_snapshot, False)
    cf_updated = group_downloads_by_category(plan["download"], new_snapshot, True)

    # Wipe-targets that the override zip re-extracts shouldn't appear in
    # Removed/To Delete — they show up under Added/Updated instead (same path,
    # new content). Mirrors the client updater's changelog.
    updated_names = {name for _, _, name, is_upd in plan["download"] if is_upd}
    override_write_paths: set[Path] = (
        {install_dir / p for p in (override_ops.get("write") or [])}
        if install_dir is not None else set()
    )
    removed_entries = [
        (p, name) for p, name in plan["delete"]
        if name not in updated_names and p not in override_write_paths
    ]

    if fresh:
        download_grouped = merge_grouped(cf_added, cf_updated, ov_added, ov_updated)
        if download_grouped:
            print(f"\n  To Download ({count(download_grouped)}):")
            print_grouped_tree(download_grouped, custom_mod_names)
        else:
            print("\n  To Download: (none)")

        if removed_entries:
            print(f"\n  To Delete ({len(removed_entries)}):")
            if install_dir is not None:
                print_delete_tree(removed_entries, install_dir, custom_mod_names)
            else:
                for _, name in sorted(removed_entries, key=lambda pair: pair[1].lower()):
                    print(f"    - {name}")
        else:
            print("\n  To Delete: (none)")
        return
    added_grouped   = merge_grouped(cf_added, ov_added)
    updated_grouped = merge_grouped(cf_updated, ov_updated)

    if added_grouped:
        print(f"\n  Added ({count(added_grouped)}):")
        print_grouped_tree(added_grouped, custom_mod_names)
    if removed_entries or ov_removed:
        print(f"\n  Removed ({len(removed_entries) + count(ov_removed)}):")
        if install_dir is not None:
            print_delete_tree(removed_entries, install_dir, custom_mod_names)
        else:
            for _, name in sorted(removed_entries, key=lambda pair: pair[1].lower()):
                print(f"    - {name}")
        if ov_removed:
            print_grouped_tree(ov_removed, custom_mod_names)
    if updated_grouped:
        print(f"\n  Updated ({count(updated_grouped)}):")
        print_grouped_tree(updated_grouped, custom_mod_names)
    if not added_grouped and not updated_grouped and not removed_entries and not ov_removed:
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
        help="Wipe mods/ and reinstall everything from scratch.",
    )
    fresh_group.add_argument(
        "--no-fresh",
        dest="fresh",
        action="store_false",
        help="Perform an incremental update (default unless no version is detected).",
    )
    parser.add_argument(
        "--reset-overrides",
        action="store_true",
        help="Wipe and re-extract every overrides folder (config/, kubejs/, etc.).",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip the confirmation and reset-overrides prompts (useful for automated deployments).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=10,
        metavar="N",
        help="Number of parallel download workers (default: 10).",
    )
    if argcomplete:
        argcomplete.autocomplete(parser)
    args = parser.parse_args()

    prefs = load_prefs("-server")

    if args.server_dir:
        server_dir = Path(args.server_dir)
    else:
        remembered = prefs.get("last_server_dir")
        if remembered and Path(remembered).is_dir():
            server_dir = Path(remembered)
        else:
            try:
                entered = input("Server directory: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n[ERROR] Aborted.")
                sys.exit(0)
            if not entered:
                print("[ERROR] No server directory provided.")
                sys.exit(1)
            server_dir = Path(entered)

    if not server_dir.is_dir():
        print(f"[ERROR] Server directory does not exist: {server_dir}")
        sys.exit(1)

    prev_dir = prefs.get("last_server_dir")
    if prev_dir != str(server_dir):
        prefs["last_server_dir"] = str(server_dir)
        save_prefs(prefs, "-server")
        print(f"Server directory set to: {server_dir}")

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
    # Client-only custom override mods are dropped from the server install; display
    # names let custom mods (which the CF API can't resolve) print readably.
    client_only_overrides: set[str] = set(versions_data.get("client_only_overrides", []))
    custom_mod_names: dict = versions_data.get("custom_mod_names", {})

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

    target_version   = str(target_entry["version"])
    target_commit    = target_entry["commit"]
    release_message  = target_entry.get("message", "")
    target_modloader = target_entry.get("modloader", "")

    # ---- Detect installed version ----
    installed_version = read_installed_version(server_dir)

    # Smart fresh default: if nothing is installed or version is malformed, default to fresh
    fresh: bool = args.fresh if args.fresh is not None else (installed_version is None)
    if installed_version is not None and bare_version(installed_version) == "?":
        fresh = True

    # ---- Fetch the target version's overrides (content + manifest) ----
    override_zip: bytes | None = None
    try:
        override_zip = fetch_overrides_zip(target_commit)
    except Exception:
        pass
    override_folders: list[str] = [
        folder
        for folder in (get_override_folders(override_zip) if override_zip else [])
        if folder not in NON_SERVER_OVERRIDE_DIRS
    ]

    def _filter_overrides(manifest: dict) -> dict:
        return {
            p: h for p, h in manifest.items()
            if p not in client_only_overrides
            and p.split("/", 1)[0] not in NON_SERVER_OVERRIDE_DIRS
        }

    new_override_manifest = _filter_overrides(fetch_override_manifest(target_commit))
    old_override_manifest: dict = {}

    # Folders wiped on fresh install: category dirs only (override folders are independent).
    fresh_wipe_dirs = list(CATEGORY_DIRS)

    # ---- Print plan summary ----
    print(f"\n{MODPACK_NAME} Server Updater")
    print("=" * 40)
    if installed_version:
        print(f"  Installed : {display_version(installed_version)}")
    else:
        print(f"  Installed : (none detected)")
    print(f"  Target    : v{target_version}")
    if target_modloader:
        print(f"  Modloader : {target_modloader}")
    if fresh:
        print(f"  Mode      : fresh install")
    else:
        print(f"  Mode      : incremental update")
    print(f"  Directory : {server_dir}")

    folder_list = ", ".join(f"{name}/" for name in fresh_wipe_dirs)
    if not installed_version:
        if fresh_wipe_dirs:
            print(f"[WARN] Installing this modpack will clear: {folder_list}")
        else:
            print("[WARN] Installing this modpack will clear the mods/ folder.")
    elif bare_version(installed_version) == "?":
        print("[WARN] Installed version is unrecognized — proceeding as a fresh install.")
        if fresh_wipe_dirs:
            print(f"[WARN] The following folders will be cleared: {folder_list}")

    if bare_version(installed_version) == str(target_version) and not fresh:
        print(f"\n[OK] Already on version {target_version} — nothing to do.")
        sys.exit(0)

    # ---- Fetch snapshots ----
    print(f"\nFetching snapshot for v{target_version} ...")
    try:
        new_raw_snapshot = fetch_snapshot(target_commit)
    except Exception as error:
        print(f"[ERROR] Could not fetch snapshot for {target_version}: {error}")
        sys.exit(1)

    new_snapshot = filter_snapshot(new_raw_snapshot, client_only_ids, mods_only=True)

    if fresh:
        old_snapshot: dict = {}
    else:
        installed_entry = next(
            (entry for entry in available_versions if str(entry["version"]) == bare_version(installed_version)),
            None,
        )
        if installed_entry is None:
            print(f"[WARN] Installed version '{display_version(installed_version)}' not found in versions.json — treating as fresh install.")
            old_snapshot = {}
            fresh = True
        else:
            print(f"Fetching snapshot for installed version {display_version(installed_version)} ...")
            try:
                old_raw_snapshot = fetch_snapshot(installed_entry["commit"])
            except Exception as error:
                print(f"[ERROR] Could not fetch snapshot for {display_version(installed_version)}: {error}")
                sys.exit(1)
            old_snapshot = filter_snapshot(old_raw_snapshot, client_only_ids, mods_only=True)
            old_override_manifest = _filter_overrides(fetch_override_manifest(installed_entry["commit"]))

    # ---- Reset overrides prompt (before changelog so it reflects the decision) ----
    # --yes skips the prompt and keeps the default (no reset) for automated runs.
    reset_overrides = bool(args.reset_overrides)
    if override_folders and not reset_overrides and not args.yes:
        override_list = ", ".join(f"{name}/" for name in override_folders)
        reset_prompt = f"\nWould you like to reset overrides? The following folders will be wiped and re-extracted:\n{override_list}"
        try:
            ans = input(f"{reset_prompt} [y/N] ").strip().lower()
            reset_overrides = ans in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            print("\n[ERROR] Aborted.")
            sys.exit(0)

    # ---- Plan override operations (version-controlled) ----
    override_ops = compute_override_ops(
        old_override_manifest, new_override_manifest, server_dir,
        fresh=fresh, reset_overrides=reset_overrides, override_folders=override_folders,
        category_dirs=CATEGORY_DIRS,
    )

    # ---- Build plan ----
    plan = build_update_plan(old_snapshot, new_snapshot, server_dir)
    if fresh:
        for folder_name in fresh_wipe_dirs:
            plan["delete"].extend(collect_wipe_targets(server_dir, [folder_name]))
    if reset_overrides:
        plan["delete"].extend(collect_wipe_targets(server_dir, override_folders))

    # ---- Show changelog ----
    if release_message:
        print(f"\n  Note: {release_message}")
    print("\nChanges:")
    print_changelog(old_snapshot, new_snapshot, fresh=fresh, plan=plan, install_dir=server_dir,
                    override_ops=override_ops, custom_mod_names=custom_mod_names)

    if not plan["download"] and not plan["delete"] and not override_ops["write"] and not override_ops["delete"]:
        print("\n[OK] Nothing to change.")
        write_installed_version(server_dir, target_version)
        sys.exit(0)

    download_count = len(plan["download"])
    delete_count   = len(plan["delete"])
    print(
        f"\n  {download_count} file(s) to download,"
        f" {delete_count} file(s) to remove."
    )

    # ---- Confirm ----
    if not args.yes:
        try:
            answer = input("\nProceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n[ERROR] Aborted.")
            sys.exit(0)
        if answer not in ("y", "yes"):
            print("[INFO] Aborted.")
            sys.exit(0)

    # ---- Apply: download to temp, then atomic move ----
    failed_downloads: list[str] = []
    updated_names = {name for _, _, name, is_upd in plan["download"] if is_upd}

    if plan["download"]:
        print(f"\nDownloading {len(plan['download'])} file(s) ...")
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp_dir = Path(tmp_str)
            downloaded: list[tuple[Path, str]] = []

            def _download_one(task: tuple) -> tuple[str | None, str, bool]:
                project_id, file_id, display_name, is_update = task
                local_path = download_mod_file(project_id, file_id, tmp_dir)
                return (str(local_path) if local_path else None, display_name, is_update)

            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {executor.submit(_download_one, task): task for task in plan["download"]}
                for future in as_completed(futures):
                    local_str, display_name, is_update = future.result()
                    if local_str:
                        icon = "[~]" if is_update else "[+]"
                        print(f"  {icon} {display_name}")
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
                    if display_name not in updated_names:
                        print(f"  [-] {display_name}")
                except OSError as error:
                    print(f"  [WARN] Could not delete {file_path.name}: {error}")

            for src_path, display_name in downloaded:
                category = classify_downloaded_file(src_path)
                dest_dir = server_dir / category
                dest_dir.mkdir(parents=True, exist_ok=True)
                destination = dest_dir / src_path.name
                if destination.exists():
                    destination.unlink()
                shutil.move(str(src_path), str(destination))
    else:
        # Only deletions
        for file_path, display_name in plan["delete"]:
            try:
                file_path.unlink()
                if display_name not in updated_names:
                    print(f"  [-] {display_name}")
            except OSError as error:
                print(f"  [WARN] Could not delete {file_path.name}: {error}")

    # ---- Apply overrides (version-controlled; mods synced, configs preserve-edits) ----
    for rel in override_ops["delete"]:
        try:
            (server_dir / rel).unlink()
            print(f"  [-] {custom_mod_names.get(rel, rel)}")
        except OSError as error:
            print(f"  [WARN] Could not delete {rel}: {error}")
    if override_ops["write"]:
        if override_zip is None:
            print("[WARN] Override content unavailable — override files were not applied.")
        else:
            applied = extract_override_members(server_dir, override_zip, override_ops["write"])
            if applied:
                label = "Resetting overrides:" if reset_overrides else "Applying override files:"
                print(f"\n{label}")
                for rel_path in applied:
                    print(f"  + {custom_mod_names.get(rel_path, rel_path)}")

    write_installed_version(server_dir, target_version)
    print(f"\n[OK] Updated to {MODPACK_NAME} {target_version}")


if __name__ == "__main__":
    main()
