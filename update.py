"""
update.py  —  CAGS4LIFE Modpack Updater
Double-click this file to check for and install modpack updates.

Requirements: Python 3.8+  and  pip install requests
(Python is free at https://www.python.org/downloads/)
"""

import json
import os
import re
import shutil
import sys
import threading
import tkinter as tk
import zipfile
from pathlib import Path
from tkinter import font as tkfont
from urllib.parse import unquote, urlparse

# -------------------------
# CONFIG  (baked in at publish time)
# -------------------------

VERSIONS_URL = "https://Codyjames345.github.io/CAGS4LIFE/versions.json"
GITHUB_USER  = "Codyjames345"
GITHUB_REPO  = "CAGS4LIFE"

# Paths relative to where update.py lives (the modpack root)
HERE         = Path(__file__).parent.resolve()
VERSION_FILE = HERE / "modpack_version.txt"
MODS_DIR     = HERE / "mods"
SHADERS_DIR  = HERE / "shaderpacks"
RESOURCES_DIR= HERE / "resourcepacks"

HEADERS = {"User-Agent": "CAGS4LIFE-updater/1.0"}

# -------------------------
# NETWORK
# -------------------------

def fetch_json(url):
    import urllib.request
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode())


def fetch_versions():
    return fetch_json(VERSIONS_URL)


def fetch_snapshot(commit):
    url = (
        f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}"
        f"/gh-pages/snapshots/{commit}.json"
    )
    return fetch_json(url)

# -------------------------
# VERSION DETECTION
# -------------------------

def local_version():
    if VERSION_FILE.exists():
        return VERSION_FILE.read_text().strip()
    return None


def find_commit(versions_data, version):
    for entry in versions_data.get("versions", []):
        if str(entry["version"]) == str(version):
            return entry["commit"]
    return None

# -------------------------
# DIFF
# -------------------------

def diff(old, new):
    old = {str(k): str(v) for k, v in old.items()}
    new = {str(k): str(v) for k, v in new.items()}
    added   = new.keys() - old.keys()
    removed = old.keys() - new.keys()
    updated = [
        (k, old[k], new[k])
        for k in old.keys() & new.keys()
        if old[k] != new[k]
    ]
    return {"added": added, "removed": removed, "updated": updated}

# -------------------------
# FILE CLASSIFICATION
# -------------------------

def classify_file(path: Path) -> str:
    if path.suffix.lower() != ".zip":
        return "mods"
    try:
        with zipfile.ZipFile(path, "r") as z:
            names = z.namelist()
        if any(n == "shaders/" or n.startswith("shaders/") for n in names):
            return "shaderpacks"
        return "resourcepacks"
    except Exception:
        return "mods"


def category_dir(category: str) -> Path:
    d = {"mods": MODS_DIR, "shaderpacks": SHADERS_DIR, "resourcepacks": RESOURCES_DIR}[category]
    d.mkdir(parents=True, exist_ok=True)
    return d

# -------------------------
# DOWNLOAD
# -------------------------

def guess_filename(response, pid, fid):
    path = urlparse(response.url).path
    name = os.path.basename(unquote(path))
    if not name or "." not in name:
        ct = response.headers.get("Content-Type", "").split(";")[0].strip()
        ext = {
            "application/zip": ".zip",
            "application/java-archive": ".jar",
        }.get(ct, ".jar")
        name = f"{pid}-{fid}{ext}"
    return name


def download_file(pid, fid, tmp_dir: Path, log_fn=None):
    """Download a single mod file into tmp_dir, return (filename, category)."""
    import urllib.request, urllib.error

    url = f"https://www.curseforge.com/api/v1/mods/{pid}/files/{fid}/download"

    class HeadRequest(urllib.request.Request):
        def get_method(self):
            return "HEAD"

    # Follow redirects to get the real filename
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            final_url = resp.url
            path = urlparse(final_url).path
            filename = os.path.basename(unquote(path))
            ct = resp.headers.get("Content-Type", "")
            if not filename or "." not in filename:
                ext = ".zip" if "zip" in ct else ".jar"
                filename = f"{pid}-{fid}{ext}"

        tmp_path = tmp_dir / filename
        if log_fn:
            log_fn(f"  Downloading {filename}...")

        urllib.request.urlretrieve(url, tmp_path)
        category = classify_file(tmp_path)
        return tmp_path, filename, category

    except Exception as e:
        if log_fn:
            log_fn(f"  [WARN] Failed to download {pid}: {e}")
        return None, None, None

# -------------------------
# APPLY UPDATE
# -------------------------

def find_existing_file(pid, fid):
    """Search mods/shaderpacks/resourcepacks for a file matching pid-fid pattern."""
    pattern = re.compile(rf"^{re.escape(pid)}.*{re.escape(fid)}")
    for d in [MODS_DIR, SHADERS_DIR, RESOURCES_DIR]:
        if not d.exists():
            continue
        for f in d.iterdir():
            if pattern.match(f.stem) or f"{pid}_{fid}" in f.name:
                return f
    return None


def apply_delta(old_snap, new_snap, log_fn=None):
    """
    Apply the diff between two snapshots to the live install.
    Returns (success_count, fail_count).
    """
    d = diff(old_snap, new_snap)
    tmp_dir = HERE / ".update_tmp"
    tmp_dir.mkdir(exist_ok=True)
    success = fail = 0

    try:
        # Remove deleted mods
        for pid in d["removed"]:
            old_fid = old_snap[pid]
            removed = False
            for search_dir in [MODS_DIR, SHADERS_DIR, RESOURCES_DIR]:
                if not search_dir.exists():
                    continue
                for f in list(search_dir.iterdir()):
                    # Match on pid anywhere in the filename (best effort)
                    if pid in f.stem:
                        f.unlink()
                        if log_fn:
                            log_fn(f"  [-] Removed {f.name}")
                        removed = True
                        break
                if removed:
                    break
            if not removed and log_fn:
                log_fn(f"  [-] Could not find file for {pid} — skipping")

        # Download updated mods (remove old first)
        for pid, old_fid, new_fid in d["updated"]:
            for search_dir in [MODS_DIR, SHADERS_DIR, RESOURCES_DIR]:
                if not search_dir.exists():
                    continue
                for f in list(search_dir.iterdir()):
                    if pid in f.stem:
                        f.unlink()
                        break

            tmp_path, filename, category = download_file(pid, new_fid, tmp_dir, log_fn)
            if tmp_path:
                dest = category_dir(category) / filename
                shutil.move(str(tmp_path), dest)
                if log_fn:
                    log_fn(f"  [~] Updated {filename}")
                success += 1
            else:
                fail += 1

        # Download added mods
        for pid in d["added"]:
            fid = new_snap[pid]
            tmp_path, filename, category = download_file(pid, fid, tmp_dir, log_fn)
            if tmp_path:
                dest = category_dir(category) / filename
                shutil.move(str(tmp_path), dest)
                if log_fn:
                    log_fn(f"  [+] Added {filename}")
                success += 1
            else:
                fail += 1

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return success, fail

# -------------------------
# GUI
# -------------------------

DARK_BG   = "#1a1a2e"
PANEL_BG  = "#16213e"
ACCENT    = "#00d4aa"
ACCENT2   = "#0f3460"
TEXT      = "#e0e0e0"
TEXT_DIM  = "#7a8a9a"
RED       = "#e05050"
GREEN     = "#00d4aa"
FONT_BODY = ("Consolas", 10)
FONT_HEAD = ("Consolas", 13, "bold")
FONT_MONO = ("Consolas", 9)


class UpdaterApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CAGS4LIFE Modpack Updater")
        self.configure(bg=DARK_BG)
        self.resizable(False, False)
        self.geometry("600x520")

        self._versions_data = None
        self._old_snap      = None
        self._new_snap      = None
        self._target_ver    = None
        self._local_ver     = None

        self._build_ui()
        self.after(200, self._check_for_updates)

    # ---- UI construction ----

    def _build_ui(self):
        # Header
        hdr = tk.Frame(self, bg=ACCENT2, padx=20, pady=14)
        hdr.pack(fill="x")

        tk.Label(hdr, text="⛏  CAGS4LIFE Modpack Updater",
                 font=FONT_HEAD, bg=ACCENT2, fg=ACCENT).pack(side="left")

        self._version_label = tk.Label(hdr, text="", font=FONT_MONO,
                                       bg=ACCENT2, fg=TEXT_DIM)
        self._version_label.pack(side="right")

        # Status line
        self._status = tk.StringVar(value="Checking for updates…")
        tk.Label(self, textvariable=self._status, font=FONT_BODY,
                 bg=DARK_BG, fg=TEXT, pady=8).pack(fill="x", padx=20)

        # Changelog box
        cl_frame = tk.Frame(self, bg=PANEL_BG, bd=0)
        cl_frame.pack(fill="both", expand=True, padx=20, pady=(0, 10))

        tk.Label(cl_frame, text="Changelog", font=("Consolas", 10, "bold"),
                 bg=PANEL_BG, fg=ACCENT, pady=6).pack(anchor="w", padx=10)

        scroll = tk.Scrollbar(cl_frame)
        scroll.pack(side="right", fill="y")

        self._changelog = tk.Text(
            cl_frame, font=FONT_MONO, bg=PANEL_BG, fg=TEXT,
            relief="flat", bd=0, wrap="word",
            yscrollcommand=scroll.set,
            state="disabled", padx=10, pady=6,
            selectbackground=ACCENT2,
        )
        self._changelog.pack(fill="both", expand=True)
        scroll.config(command=self._changelog.yview)

        # Tags for markdown-ish styling
        self._changelog.tag_config("h1",    font=("Consolas", 12, "bold"), foreground=ACCENT)
        self._changelog.tag_config("h2",    font=("Consolas", 10, "bold"), foreground=ACCENT)
        self._changelog.tag_config("added", foreground=GREEN)
        self._changelog.tag_config("removed", foreground=RED)
        self._changelog.tag_config("updated", foreground="#f0c060")
        self._changelog.tag_config("dim",   foreground=TEXT_DIM)

        # Log / progress box (hidden until update starts)
        self._log_frame = tk.Frame(self, bg=PANEL_BG, bd=0)

        tk.Label(self._log_frame, text="Progress", font=("Consolas", 10, "bold"),
                 bg=PANEL_BG, fg=ACCENT, pady=6).pack(anchor="w", padx=10)

        log_scroll = tk.Scrollbar(self._log_frame)
        log_scroll.pack(side="right", fill="y")

        self._log_text = tk.Text(
            self._log_frame, font=FONT_MONO, bg=PANEL_BG, fg=TEXT_DIM,
            relief="flat", bd=0, wrap="word",
            yscrollcommand=log_scroll.set,
            state="disabled", padx=10, pady=6, height=6,
        )
        self._log_text.pack(fill="both", expand=True)
        log_scroll.config(command=self._log_text.yview)

        # Button row
        btn_row = tk.Frame(self, bg=DARK_BG)
        btn_row.pack(fill="x", padx=20, pady=12)

        self._btn = tk.Button(
            btn_row, text="Update",
            font=("Consolas", 11, "bold"),
            bg=ACCENT, fg=DARK_BG,
            activebackground="#00b894",
            activeforeground=DARK_BG,
            relief="flat", bd=0,
            padx=24, pady=8,
            cursor="hand2",
            state="disabled",
            command=self._start_update,
        )
        self._btn.pack(side="right")

        self._close_btn = tk.Button(
            btn_row, text="Close",
            font=("Consolas", 11),
            bg=PANEL_BG, fg=TEXT_DIM,
            activebackground=ACCENT2,
            activeforeground=TEXT,
            relief="flat", bd=0,
            padx=18, pady=8,
            cursor="hand2",
            command=self.destroy,
        )
        self._close_btn.pack(side="right", padx=(0, 10))

    # ---- Helpers ----

    def _set_status(self, msg, color=TEXT):
        self._status.set(msg)
        # find the label and update fg — easiest is to store a ref
        for w in self.winfo_children():
            if isinstance(w, tk.Label) and w.cget("textvariable") == str(self._status):
                w.config(fg=color)
                break

    def _append_changelog(self, text):
        self._changelog.config(state="normal")

        for line in text.splitlines(keepends=True):
            stripped = line.rstrip("\n")
            if stripped.startswith("# "):
                self._changelog.insert("end", stripped[2:] + "\n", "h1")
            elif stripped.startswith("## ➕"):
                self._changelog.insert("end", stripped + "\n", "h2")
            elif stripped.startswith("## ➖"):
                self._changelog.insert("end", stripped + "\n", "h2")
            elif stripped.startswith("## 🔄"):
                self._changelog.insert("end", stripped + "\n", "h2")
            elif stripped.startswith("- ") and any(e in text[:text.find(stripped)] for e in ["➕", "Added"]):
                self._changelog.insert("end", stripped + "\n", "added")
            elif stripped.startswith("- ") and any(e in text[:text.find(stripped)] for e in ["➖", "Removed"]):
                self._changelog.insert("end", stripped + "\n", "removed")
            elif stripped.startswith("- "):
                # default list item — try to detect section from context
                self._changelog.insert("end", stripped + "\n")
            elif stripped.startswith("_") or stripped.startswith(">"):
                self._changelog.insert("end", stripped + "\n", "dim")
            else:
                self._changelog.insert("end", line)

        self._changelog.config(state="disabled")
        self._changelog.see("end")

    def _log(self, msg):
        self._log_text.config(state="normal")
        self._log_text.insert("end", msg + "\n")
        self._log_text.config(state="disabled")
        self._log_text.see("end")
        self.update_idletasks()

    # ---- Core flow ----

    def _check_for_updates(self):
        def worker():
            try:
                self._local_ver = local_version()
                self._versions_data = fetch_versions()
                latest = self._versions_data.get("latest")

                ver_text = f"installed: {self._local_ver or 'unknown'}  /  latest: {latest}"
                self.after(0, lambda: self._version_label.config(text=ver_text))

                if not latest:
                    self.after(0, lambda: self._set_status("Could not determine latest version.", RED))
                    return

                if self._local_ver == latest:
                    self.after(0, lambda: self._set_status(
                        f"✓  You're up to date! (v{latest})", ACCENT))
                    self.after(0, lambda: self._append_changelog(
                        f"# v{latest}\n\nYou have the latest version installed.\nNo updates needed."))
                    return

                # Fetch both snapshots
                new_commit = find_commit(self._versions_data, latest)
                if not new_commit:
                    self.after(0, lambda: self._set_status("Could not find snapshot for latest version.", RED))
                    return

                self._new_snap  = fetch_snapshot(new_commit)
                self._target_ver = latest

                if self._local_ver:
                    old_commit = find_commit(self._versions_data, self._local_ver)
                    if old_commit:
                        self._old_snap = fetch_snapshot(old_commit)
                    else:
                        self._old_snap = {}
                else:
                    self._old_snap = {}

                # Build changelog display
                d = diff(self._old_snap, self._new_snap)
                n_changes = len(d["added"]) + len(d["removed"]) + len(d["updated"])

                from_label = self._local_ver or "scratch"
                changelog_text = self._build_changelog_text(d, from_label, latest)

                self.after(0, lambda: self._set_status(
                    f"Update available: v{self._local_ver or '?'} → v{latest}  "
                    f"({n_changes} change{'s' if n_changes != 1 else ''})", ACCENT))
                self.after(0, lambda: self._append_changelog(changelog_text))
                self.after(0, lambda: self._btn.config(
                    state="normal",
                    text=f"Update to v{latest}",
                ))

            except Exception as e:
                self.after(0, lambda: self._set_status(f"Error: {e}", RED))
                self.after(0, lambda: self._append_changelog(
                    f"# Connection Error\n\n{e}\n\nCheck your internet connection and try again."))

        threading.Thread(target=worker, daemon=True).start()

    def _build_changelog_text(self, d, v_from, v_to):
        lines = [f"# v{v_from} → v{v_to}", ""]

        if d["added"]:
            lines.append(f"## ➕ Added  ({len(d['added'])})")
            for pid in sorted(d["added"]):
                lines.append(f"- mod:{pid}")
            lines.append("")

        if d["removed"]:
            lines.append(f"## ➖ Removed  ({len(d['removed'])})")
            for pid in sorted(d["removed"]):
                lines.append(f"- mod:{pid}")
            lines.append("")

        if d["updated"]:
            lines.append(f"## 🔄 Updated  ({len(d['updated'])})")
            for pid, old_fid, new_fid in sorted(d["updated"]):
                lines.append(f"- mod:{pid}  (file {old_fid} → {new_fid})")
            lines.append("")

        if not d["added"] and not d["removed"] and not d["updated"]:
            lines.append("_No mod-list changes in this update._")

        return "\n".join(lines)

    def _start_update(self):
        self._btn.config(state="disabled", text="Updating…")
        self._log_frame.pack(fill="x", padx=20, pady=(0, 6))

        def worker():
            try:
                self._log("Starting update…")
                success, fail = apply_delta(
                    self._old_snap, self._new_snap,
                    log_fn=lambda m: self.after(0, lambda msg=m: self._log(msg))
                )

                # Write new version file
                VERSION_FILE.write_text(self._target_ver)
                self._log(f"\nVersion file updated to {self._target_ver}.")

                if fail == 0:
                    self.after(0, lambda: self._set_status(
                        f"✓  Updated to v{self._target_ver} successfully!", ACCENT))
                    self.after(0, lambda: self._btn.config(
                        text="Done! Restart Minecraft", state="disabled"))
                else:
                    self.after(0, lambda: self._set_status(
                        f"Updated with {fail} failure(s). Check the log above.", "#f0c060"))
                    self.after(0, lambda: self._btn.config(
                        text="Completed (with errors)", state="disabled"))

            except Exception as e:
                self.after(0, lambda: self._set_status(f"Update failed: {e}", RED))
                self.after(0, lambda: self._btn.config(
                    text="Retry", state="normal", command=self._start_update))
                self._log(f"\n[ERROR] {e}")

        threading.Thread(target=worker, daemon=True).start()


# -------------------------
# ENTRY POINT
# -------------------------

if __name__ == "__main__":
    # Check requests is available (only needed for modpackctl side, not here —
    # update.py uses urllib only, so no extra installs needed)
    app = UpdaterApp()
    app.mainloop()
