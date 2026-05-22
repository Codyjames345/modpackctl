"""
client-updater.py  —  Modpack Updater
Double-click this file to check for and install modpack updates.

Requirements: Python 3.8+
(Python is free at https://www.python.org/downloads/)
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import tkinter as tk
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import filedialog, messagebox
from urllib.parse import unquote, urlparse

# -------------------------
# CONFIG  (baked in at release time by modpackctl)
# -------------------------

GITHUB_USER  = "__GITHUB_USER__"
GITHUB_REPO  = "__GITHUB_REPO__"
VERSIONS_URL = f"https://{GITHUB_USER}.github.io/{GITHUB_REPO}/versions.json"

if "__" in GITHUB_USER or "__" in GITHUB_REPO:
    print(
        "[ERROR] client-updater.py has not been configured.\n"
        "Download a configured copy from the modpack's GitHub Releases page,\n"
        "or run 'python modpackctl.py publish <version>' to produce one."
    )
    sys.exit(1)

HEADERS = {"User-Agent": f"{GITHUB_REPO}-updater/1.0"}


# -------------------------
# PREFS  (remembers the modpack folder between runs)
# -------------------------

def _prefs_dir() -> Path:
    """Return the per-user prefs directory for the updater."""
    return Path.home() / ".modpack-updater"


def _prefs_path() -> Path:
    """Return the prefs file path, namespaced per modpack."""
    return _prefs_dir() / f"{GITHUB_USER}-{GITHUB_REPO}.json"


def load_prefs() -> dict:
    """Return saved prefs (last folder, etc.), or empty dict if missing/corrupt."""
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
# FOLDER AUTODETECTION
# -------------------------

def autodetect_minecraft_folder() -> Path | None:
    """Return a likely default Minecraft folder, or None if none exists."""
    home = Path.home()
    candidates = [
        home / "AppData" / "Roaming" / ".minecraft",              # Windows
        home / "Library" / "Application Support" / "minecraft",   # macOS
        home / ".minecraft",                                      # Linux
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def is_likely_modpack_folder(folder: Path) -> bool:
    """Return True if folder looks like a Minecraft modpack root."""
    if not folder.is_dir():
        return False
    return (folder / "mods").is_dir() or (folder / "modpack_version.txt").exists()


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

def classify_downloaded_file(path: Path) -> str:
    """Inspect a file and return 'mods', 'shaderpacks', or 'resourcepacks'."""
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


def locate_existing_file(project_id: str, entry: dict, modpack_dir: Path) -> Path | None:
    """
    Find the on-disk file for a mod entry. Prefers an exact filename match
    in the entry's expected category, then falls back to all category folders,
    then to a substring match by project_id.
    """
    expected_category = entry.get("category", "mods")
    expected_filename = entry.get("file", "")

    if expected_filename:
        exact = modpack_dir / expected_category / expected_filename
        if exact.exists():
            return exact
        for category in ("mods", "shaderpacks", "resourcepacks"):
            candidate = modpack_dir / category / expected_filename
            if candidate.exists():
                return candidate

    for category in ("mods", "shaderpacks", "resourcepacks"):
        category_dir = modpack_dir / category
        if not category_dir.is_dir():
            continue
        for file_path in category_dir.iterdir():
            if project_id in file_path.stem:
                return file_path
    return None


# -------------------------
# DOWNLOAD
# -------------------------

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
# UPDATE PLAN
# -------------------------

def build_update_plan(old_snapshot: dict, new_snapshot: dict, modpack_dir: Path) -> dict:
    """
    Build an ordered list of operations to migrate from old_snapshot to
    new_snapshot in modpack_dir. Returns a dict with:
      - 'download': [(project_id, file_id, display_name), ...]
      - 'delete':   [(Path, display_name), ...]
    """
    changes  = diff_snapshots(old_snapshot, new_snapshot)
    download = []
    delete   = []

    for project_id, old_entry in changes["removed"]:
        existing = locate_existing_file(project_id, old_entry, modpack_dir)
        if existing:
            delete.append((existing, old_entry["name"]))

    for project_id, old_entry, new_entry in changes["updated"]:
        existing = locate_existing_file(project_id, old_entry, modpack_dir)
        if existing:
            delete.append((existing, old_entry["name"]))
        download.append((project_id, new_entry["file_id"], new_entry["name"]))

    for project_id, new_entry in changes["added"]:
        download.append((project_id, new_entry["file_id"], new_entry["name"]))

    return {"download": download, "delete": delete}


# -------------------------
# GUI THEME
# -------------------------

DARK_BG   = "#1a1a2e"
PANEL_BG  = "#16213e"
ACCENT    = "#00d4aa"
ACCENT_HV = "#00b894"
ACCENT2   = "#0f3460"
TEXT      = "#e0e0e0"
TEXT_DIM  = "#7a8a9a"
RED       = "#e05050"
GREEN     = "#00d4aa"
YELLOW    = "#f0c060"

FONT_TITLE = ("Consolas", 13, "bold")
FONT_LARGE = ("Consolas", 12)
FONT_BOLD  = ("Consolas", 11, "bold")
FONT_BODY  = ("Consolas", 10)
FONT_MONO  = ("Consolas", 9)


# -------------------------
# GUI APP
# -------------------------

class UpdaterApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{GITHUB_REPO} Modpack Updater")
        self.configure(bg=DARK_BG)
        self.geometry("640x540")
        self.minsize(560, 420)

        self.prefs: dict = load_prefs()
        self.modpack_dir: Path | None    = None
        self.local_version: str | None   = None
        self.latest_version: str | None  = None
        self.release_message: str        = ""
        self.old_snapshot: dict          = {}
        self.new_snapshot: dict          = {}
        self.update_plan: dict           = {}

        # Widget refs reused across screens
        self._current_frame: tk.Frame | None = None
        self._path_var       = tk.StringVar()
        self._picker_status  = tk.StringVar()
        self._progress_var   = tk.StringVar()
        self._log_text: tk.Text | None       = None

        self._show_folder_picker()

    # ---- frame management ----

    def _swap_frame(self, builder) -> None:
        if self._current_frame is not None:
            self._current_frame.destroy()
        new_frame = builder()
        new_frame.pack(fill="both", expand=True)
        self._current_frame = new_frame

    def _header(self, parent: tk.Misc, subtitle: str = "") -> tk.Frame:
        header = tk.Frame(parent, bg=ACCENT2, padx=20, pady=14)
        header.pack(fill="x")
        tk.Label(
            header, text=f"⛏  {GITHUB_REPO} Modpack Updater",
            font=FONT_TITLE, bg=ACCENT2, fg=ACCENT,
        ).pack(side="left")
        if subtitle:
            tk.Label(
                header, text=subtitle,
                font=FONT_MONO, bg=ACCENT2, fg=TEXT_DIM,
            ).pack(side="right")
        return header

    def _primary_button(self, parent: tk.Misc, text: str, command) -> tk.Button:
        return tk.Button(
            parent, text=text, font=FONT_BOLD,
            bg=ACCENT, fg=DARK_BG,
            activebackground=ACCENT_HV, activeforeground=DARK_BG,
            relief="flat", bd=0, padx=24, pady=8, cursor="hand2",
            command=command,
        )

    def _secondary_button(self, parent: tk.Misc, text: str, command) -> tk.Button:
        return tk.Button(
            parent, text=text, font=FONT_BODY,
            bg=PANEL_BG, fg=TEXT_DIM,
            activebackground=ACCENT2, activeforeground=TEXT,
            relief="flat", bd=0, padx=18, pady=8, cursor="hand2",
            command=command,
        )

    # ---- screen: folder picker ----

    def _show_folder_picker(self) -> None:
        def build() -> tk.Frame:
            frame = tk.Frame(self, bg=DARK_BG)
            self._header(frame)

            body = tk.Frame(frame, bg=DARK_BG, padx=20, pady=20)
            body.pack(fill="both", expand=True)

            tk.Label(
                body, text="Select your modpack folder",
                font=FONT_BOLD, bg=DARK_BG, fg=TEXT,
            ).pack(anchor="w")

            tk.Label(
                body,
                text=(
                    "This is the folder that contains 'mods', 'config', etc.\n"
                    "Usually inside your launcher's instances directory."
                ),
                font=FONT_BODY, bg=DARK_BG, fg=TEXT_DIM,
                justify="left", wraplength=560,
            ).pack(anchor="w", pady=(4, 14))

            default_folder = self.prefs.get("modpack_dir") or ""
            if not default_folder:
                detected = autodetect_minecraft_folder()
                if detected:
                    default_folder = str(detected)
            self._path_var.set(default_folder)

            path_row = tk.Frame(body, bg=DARK_BG)
            path_row.pack(fill="x", pady=(0, 6))
            entry = tk.Entry(
                path_row, textvariable=self._path_var,
                font=FONT_MONO, bg=PANEL_BG, fg=TEXT,
                relief="flat", insertbackground=TEXT,
            )
            entry.pack(side="left", fill="x", expand=True, ipady=6)
            tk.Button(
                path_row, text="Browse…",
                font=FONT_BODY, bg=PANEL_BG, fg=TEXT,
                activebackground=ACCENT2, activeforeground=TEXT,
                relief="flat", bd=0, padx=14, cursor="hand2",
                command=self._browse_folder,
            ).pack(side="left", padx=(8, 0))

            self._picker_status.set("")
            tk.Label(
                body, textvariable=self._picker_status,
                font=FONT_MONO, bg=DARK_BG, fg=YELLOW,
            ).pack(anchor="w", pady=(8, 0))

            button_row = tk.Frame(frame, bg=DARK_BG)
            button_row.pack(fill="x", padx=20, pady=12)
            self._secondary_button(button_row, "Close", self.destroy).pack(side="right", padx=(10, 0))
            self._primary_button(button_row, "Next  →", self._confirm_folder).pack(side="right")

            return frame
        self._swap_frame(build)

    def _browse_folder(self) -> None:
        initial = self._path_var.get() or str(Path.home())
        chosen = filedialog.askdirectory(
            initialdir=initial, title="Select your modpack folder",
        )
        if chosen:
            self._path_var.set(chosen)

    def _confirm_folder(self) -> None:
        raw_path = self._path_var.get().strip()
        if not raw_path:
            self._picker_status.set("Please enter or browse to a folder.")
            return
        path = Path(raw_path).expanduser().resolve()
        if not path.is_dir():
            self._picker_status.set(f"Folder does not exist: {path}")
            return
        if not is_likely_modpack_folder(path):
            confirmed = messagebox.askyesno(
                "Folder may not be a modpack",
                f"This folder does not contain a 'mods' subfolder or 'modpack_version.txt'.\n\n"
                f"{path}\n\nContinue anyway?",
            )
            if not confirmed:
                return
        self.modpack_dir = path
        self.prefs["modpack_dir"] = str(path)
        save_prefs(self.prefs)
        self._show_checking()

    # ---- screen: checking ----

    def _show_checking(self) -> None:
        def build() -> tk.Frame:
            frame = tk.Frame(self, bg=DARK_BG)
            self._header(frame)
            tk.Label(
                frame, text="Checking for updates…",
                font=FONT_LARGE, bg=DARK_BG, fg=TEXT, pady=80,
            ).pack(fill="x")
            return frame
        self._swap_frame(build)
        threading.Thread(target=self._run_check, daemon=True).start()

    def _run_check(self) -> None:
        try:
            assert self.modpack_dir is not None
            version_file = self.modpack_dir / "modpack_version.txt"
            self.local_version = (
                version_file.read_text(encoding="utf-8").strip()
                if version_file.exists() else None
            )

            versions_data = fetch_versions()
            self.latest_version = versions_data.get("latest")
            if not self.latest_version:
                self.after(0, lambda: self._show_outcome(
                    success=False,
                    message="Could not determine latest version from versions.json.",
                ))
                return

            if self.local_version == self.latest_version:
                self.after(0, self._show_up_to_date)
                return

            new_commit = None
            old_commit = None
            for entry in versions_data.get("versions", []):
                version_str = str(entry["version"])
                if version_str == str(self.latest_version):
                    new_commit = entry["commit"]
                    self.release_message = entry.get("message", "")
                if self.local_version and version_str == str(self.local_version):
                    old_commit = entry["commit"]

            if not new_commit:
                self.after(0, lambda: self._show_outcome(
                    success=False,
                    message=f"Could not find snapshot for v{self.latest_version}.",
                ))
                return

            self.new_snapshot = fetch_snapshot(new_commit)
            self.old_snapshot = fetch_snapshot(old_commit) if old_commit else {}
            self.update_plan  = build_update_plan(
                self.old_snapshot, self.new_snapshot, self.modpack_dir,
            )
            self.after(0, self._show_changelog)
        except Exception as exc:
            error_message = f"Error checking for updates:\n\n{exc}"
            self.after(0, lambda: self._show_outcome(success=False, message=error_message))

    # ---- screen: up to date ----

    def _show_up_to_date(self) -> None:
        def build() -> tk.Frame:
            frame = tk.Frame(self, bg=DARK_BG)
            self._header(frame)
            body = tk.Frame(frame, bg=DARK_BG, padx=20, pady=40)
            body.pack(fill="both", expand=True)
            tk.Label(
                body, text="✓  You're up to date!",
                font=("Consolas", 16, "bold"), bg=DARK_BG, fg=ACCENT,
            ).pack(pady=(10, 4))
            tk.Label(
                body, text=f"Installed version: v{self.local_version}",
                font=FONT_BODY, bg=DARK_BG, fg=TEXT_DIM,
            ).pack()
            button_row = tk.Frame(frame, bg=DARK_BG)
            button_row.pack(fill="x", padx=20, pady=12)
            self._primary_button(button_row, "Close", self.destroy).pack(side="right")
            return frame
        self._swap_frame(build)

    # ---- screen: changelog ----

    def _show_changelog(self) -> None:
        def build() -> tk.Frame:
            frame = tk.Frame(self, bg=DARK_BG)
            from_label = f"v{self.local_version}" if self.local_version else "fresh install"
            self._header(frame, subtitle=f"{from_label}  →  v{self.latest_version}")

            changes = diff_snapshots(self.old_snapshot, self.new_snapshot)
            num_added   = len(changes["added"])
            num_removed = len(changes["removed"])
            num_updated = len(changes["updated"])

            tk.Label(
                frame,
                text=f"{num_added} added · {num_removed} removed · {num_updated} updated",
                font=FONT_BODY, bg=DARK_BG, fg=TEXT, pady=8,
            ).pack(fill="x", padx=20)

            if self.release_message:
                tk.Label(
                    frame, text=self.release_message,
                    font=FONT_BODY, bg=DARK_BG, fg=TEXT_DIM,
                    wraplength=560, justify="left",
                ).pack(fill="x", padx=20, pady=(0, 8))

            text_frame = tk.Frame(frame, bg=PANEL_BG)
            text_frame.pack(fill="both", expand=True, padx=20, pady=(0, 10))
            scrollbar = tk.Scrollbar(text_frame)
            scrollbar.pack(side="right", fill="y")

            text = tk.Text(
                text_frame, font=FONT_MONO, bg=PANEL_BG, fg=TEXT,
                relief="flat", bd=0, wrap="word",
                yscrollcommand=scrollbar.set,
                state="disabled", padx=10, pady=6,
                selectbackground=ACCENT2,
            )
            text.pack(fill="both", expand=True)
            scrollbar.config(command=text.yview)

            text.tag_config("section", font=("Consolas", 10, "bold"), foreground=ACCENT)
            text.tag_config("added",   foreground=GREEN)
            text.tag_config("removed", foreground=RED)
            text.tag_config("updated", foreground=YELLOW)
            text.tag_config("dim",     foreground=TEXT_DIM)

            text.config(state="normal")

            text.insert("end", "## Added\n", "section")
            text.insert("end", "\n")
            if changes["added"]:
                for _, entry in changes["added"]:
                    text.insert("end", f"- {entry['name']}\n", "added")
            else:
                text.insert("end", "_No mods added._\n", "dim")
            text.insert("end", "\n")

            text.insert("end", "## Removed\n", "section")
            text.insert("end", "\n")
            if changes["removed"]:
                for _, entry in changes["removed"]:
                    text.insert("end", f"- {entry['name']}\n", "removed")
            else:
                text.insert("end", "_No mods removed._\n", "dim")
            text.insert("end", "\n")

            text.insert("end", "## Updated\n", "section")
            text.insert("end", "\n")
            if changes["updated"]:
                for _, old_entry, new_entry in changes["updated"]:
                    text.insert("end", f"- {new_entry['name']}", "updated")
                    text.insert("end", f"  _({old_entry['file']} → {new_entry['file']})_\n", "dim")
            else:
                text.insert("end", "_No mods updated._\n", "dim")

            text.config(state="disabled")

            button_row = tk.Frame(frame, bg=DARK_BG)
            button_row.pack(fill="x", padx=20, pady=12)
            self._secondary_button(button_row, "Cancel", self.destroy).pack(side="right", padx=(10, 0))
            self._primary_button(button_row, "Confirm & Update  →", self._show_updating).pack(side="right")
            return frame
        self._swap_frame(build)

    # ---- screen: updating ----

    def _show_updating(self) -> None:
        def build() -> tk.Frame:
            frame = tk.Frame(self, bg=DARK_BG)
            self._header(frame, subtitle=f"v{self.local_version or '?'}  →  v{self.latest_version}")
            self._progress_var.set("Starting…")
            tk.Label(
                frame, textvariable=self._progress_var,
                font=FONT_BODY, bg=DARK_BG, fg=TEXT, pady=8,
            ).pack(fill="x", padx=20)

            log_frame = tk.Frame(frame, bg=PANEL_BG)
            log_frame.pack(fill="both", expand=True, padx=20, pady=(0, 10))
            scrollbar = tk.Scrollbar(log_frame)
            scrollbar.pack(side="right", fill="y")
            log_text = tk.Text(
                log_frame, font=FONT_MONO, bg=PANEL_BG, fg=TEXT_DIM,
                relief="flat", bd=0, wrap="word",
                yscrollcommand=scrollbar.set,
                state="disabled", padx=10, pady=6,
            )
            log_text.pack(fill="both", expand=True)
            scrollbar.config(command=log_text.yview)
            self._log_text = log_text
            return frame
        self._swap_frame(build)
        threading.Thread(target=self._run_update, daemon=True).start()

    def _log(self, message: str) -> None:
        log_text = self._log_text
        if log_text is None:
            return
        def append() -> None:
            log_text.config(state="normal")
            log_text.insert("end", message + "\n")
            log_text.see("end")
            log_text.config(state="disabled")
        self.after(0, append)

    def _set_progress(self, message: str) -> None:
        self.after(0, lambda: self._progress_var.set(message))

    def _run_update(self) -> None:
        assert self.modpack_dir is not None and self.latest_version is not None
        # Temp dir lives inside the modpack folder so the final move is a
        # same-filesystem rename (fast and atomic on most platforms).
        tmp_dir = self.modpack_dir / ".update_tmp"
        try:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)
            tmp_dir.mkdir(parents=True, exist_ok=True)

            downloads = self.update_plan["download"]
            deletions = self.update_plan["delete"]
            downloaded_files: list[tuple[Path, str]] = []
            failed_downloads: list[str] = []

            # Phase 1: download all new/updated files concurrently
            completed_count = 0
            count_lock = threading.Lock()
            if downloads:
                self._set_progress(f"Downloading 0 / {len(downloads)}…")
            with ThreadPoolExecutor(max_workers=5) as executor:
                future_map = {
                    executor.submit(download_mod_file, project_id, file_id, tmp_dir): (project_id, display_name)
                    for project_id, file_id, display_name in downloads
                }
                for future in as_completed(future_map):
                    _, display_name = future_map[future]
                    local_path = future.result()
                    with count_lock:
                        completed_count += 1
                        count = completed_count
                    self._set_progress(f"Downloading {count} / {len(downloads)}…")
                    if local_path is None:
                        failed_downloads.append(display_name)
                        self._log(f"  [FAIL] {display_name}")
                        continue
                    category = classify_downloaded_file(local_path)
                    downloaded_files.append((local_path, category))
                    self._log(f"  ↓ {display_name}")

            if failed_downloads:
                self._log("")
                self._log(f"[ERROR] {len(failed_downloads)} file(s) failed to download.")
                self._log("        Your modpack was not modified.")
                self.after(0, lambda: self._show_outcome(
                    success=False,
                    message=(
                        f"{len(failed_downloads)} file(s) failed to download.\n"
                        "Your modpack was not modified. Check your internet "
                        "connection and try again."
                    ),
                ))
                return

            # Phase 2: delete old files
            self._set_progress("Applying changes…")
            self._log("")
            for old_path, display_name in deletions:
                try:
                    old_path.unlink()
                    self._log(f"  ✗ {display_name}")
                except OSError as exc:
                    self._log(f"  [warn] could not delete {display_name}: {exc}")

            # Phase 3: move downloaded files into place
            for tmp_path, category in downloaded_files:
                dest_dir = self.modpack_dir / category
                dest_dir.mkdir(parents=True, exist_ok=True)
                destination = dest_dir / tmp_path.name
                if destination.exists():
                    destination.unlink()
                try:
                    shutil.move(str(tmp_path), str(destination))
                    self._log(f"  + {destination.name}")
                except OSError as exc:
                    self._log(f"  [warn] could not install {tmp_path.name}: {exc}")

            # Phase 4: record the new version last so a crash mid-update
            # leaves modpack_version.txt pointing at the old version.
            (self.modpack_dir / "modpack_version.txt").write_text(
                self.latest_version, encoding="utf-8",
            )
            self._log("")
            self._log(f"modpack_version.txt → {self.latest_version}")

            self.after(0, lambda: self._show_outcome(
                success=True,
                message=f"Updated to v{self.latest_version}. Launch Minecraft to play.",
            ))
        except Exception as exc:
            self.after(0, lambda: self._show_outcome(
                success=False, message=f"Update failed:\n\n{exc}",
            ))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ---- screen: outcome (success or error) ----

    def _show_outcome(self, success: bool, message: str) -> None:
        def build() -> tk.Frame:
            frame = tk.Frame(self, bg=DARK_BG)
            self._header(frame)
            body = tk.Frame(frame, bg=DARK_BG, padx=20, pady=40)
            body.pack(fill="both", expand=True)
            icon  = "✓" if success else "✗"
            color = ACCENT if success else RED
            tk.Label(
                body, text=icon,
                font=("Consolas", 32, "bold"), bg=DARK_BG, fg=color,
            ).pack(pady=(0, 8))
            tk.Label(
                body, text=message,
                font=FONT_BODY, bg=DARK_BG, fg=TEXT,
                wraplength=560, justify="center",
            ).pack()
            button_row = tk.Frame(frame, bg=DARK_BG)
            button_row.pack(fill="x", padx=20, pady=12)
            self._primary_button(button_row, "Close", self.destroy).pack(side="right")
            return frame
        self._swap_frame(build)


# -------------------------
# ENTRY POINT
# -------------------------

if __name__ == "__main__":
    UpdaterApp().mainloop()
