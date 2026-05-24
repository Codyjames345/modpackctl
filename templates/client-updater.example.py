"""
client-updater.py  —  Modpack Updater
Double-click this file to check for and install modpack updates.

Requirements: Python 3.8+
(Python is free at https://www.python.org/downloads/)
"""

from __future__ import annotations

import base64
import colorsys
import contextlib
import importlib
import io
import json
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import winsound
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk
from urllib.parse import unquote, urlparse

try:
    import yt_dlp as _yt_dlp  # type: ignore[import-untyped]
except ImportError:
    _yt_dlp = None  # type: ignore[assignment]

try:
    import imageio  # type: ignore[import-untyped]  # noqa: F401
    _HAS_IMAGEIO = True
except ImportError:
    _HAS_IMAGEIO = False

# -------------------------
# CONFIG  (baked in at release time by modpackctl)
# -------------------------

GITHUB_USER  = "__GITHUB_USER__"
GITHUB_REPO  = "__GITHUB_REPO__"
MODPACK_NAME = "__MODPACK_NAME__"
LOGO_URL     = "__LOGO_URL__"
VERSIONS_URL = f"https://{GITHUB_USER}.github.io/{GITHUB_REPO}/versions.json"

if "__" in GITHUB_USER or "__" in GITHUB_REPO:
    print(
        "[ERROR] client-updater.py has not been configured.\n"
        "Download a configured copy from the modpack's GitHub Releases page,\n"
        "or run 'python modpackctl.py publish <version>' to produce one."
    )
    sys.exit(1)

# Optional fields: fall back gracefully if not baked
if "__" in MODPACK_NAME:
    MODPACK_NAME = GITHUB_REPO
if "__" in LOGO_URL:
    LOGO_URL = ""

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

_BCC_CONFIG_PATH = Path("config") / "bcc-common.toml"
_BCC_VERSION_RE  = re.compile(r'^([ \t]*modpackVersion\s*=\s*)"([^"]*)"', re.MULTILINE)
_BCC_NAME_RE     = re.compile(r'^([ \t]*modpackName\s*=\s*)"([^"]*)"',    re.MULTILINE)


def read_installed_version(modpack_dir: Path) -> str | None:
    """Return the modpackVersion from config/bcc-common.toml, or None if absent/unset."""
    bcc_path = modpack_dir / _BCC_CONFIG_PATH
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


def write_installed_version(modpack_dir: Path, version: str) -> None:
    """Write modpackVersion (and modpackName) into config/bcc-common.toml."""
    bcc_path = modpack_dir / _BCC_CONFIG_PATH
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
    return (folder / "mods").is_dir() or (folder / _BCC_CONFIG_PATH).exists()


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

def filter_for_client(snapshot: dict, server_only_ids: set[str]) -> dict:
    """Return a copy of snapshot with server-only mods removed."""
    if not server_only_ids:
        return snapshot
    return {
        project_id: entry
        for project_id, entry in snapshot.items()
        if project_id not in server_only_ids
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
      - 'download': [(project_id, file_id, display_name, is_update), ...]
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
        download.append((project_id, new_entry["file_id"], new_entry["name"], True))

    for project_id, new_entry in changes["added"]:
        download.append((project_id, new_entry["file_id"], new_entry["name"], False))

    return {"download": download, "delete": delete}


# -------------------------
# GUI THEME
# -------------------------

_COLOUR_DEFAULTS: dict[str, str] = {
    "DARK_BG":   "#0c0431",
    "PANEL_BG":  "#120647",
    "ACCENT":    "#fedb0e",
    "ACCENT_HV": "#d8ba0d",
    "ACCENT2":   "#630a26",
    "TEXT":      "#e0e0e0",
    "TEXT_DIM":  "#7a8a9a",
    "RED":       "#e05050",
    "GREEN":     "#00d4aa",
    "YELLOW":    "#f0c060",
    "KONAMI":    "#ff69b4",
}

_COLOUR_LABELS: dict[str, str] = {
    "DARK_BG":   "Background",
    "PANEL_BG":  "Panels / Secondary buttons",
    "ACCENT":    "Header text / Primary buttons",
    "ACCENT_HV": "Primary buttons (clicked)",
    "ACCENT2":   "Header background / Secondary buttons (clicked)",
    "TEXT":      "Text",
    "TEXT_DIM":  "Dim text",
    "RED":       "Changelog: removed",
    "GREEN":     "Changelog: added",
    "YELLOW":    "Changelog: updated",
    "KONAMI":    "Secret Button",
}

DARK_BG   = _COLOUR_DEFAULTS["DARK_BG"]
PANEL_BG  = _COLOUR_DEFAULTS["PANEL_BG"]
ACCENT    = _COLOUR_DEFAULTS["ACCENT"]
ACCENT_HV = _COLOUR_DEFAULTS["ACCENT_HV"]
ACCENT2   = _COLOUR_DEFAULTS["ACCENT2"]
TEXT      = _COLOUR_DEFAULTS["TEXT"]
TEXT_DIM  = _COLOUR_DEFAULTS["TEXT_DIM"]
RED       = _COLOUR_DEFAULTS["RED"]
GREEN     = _COLOUR_DEFAULTS["GREEN"]
YELLOW    = _COLOUR_DEFAULTS["YELLOW"]
KONAMI    = _COLOUR_DEFAULTS["KONAMI"]

_KONAMI_SEQUENCE  = [
    "Up", "Up", "Down", "Down", "Left", "Right", "Left", "Right", "b", "a", "Return",
]
_DANCE_VIDEO_URL  = "__SECRET_VIDEO_URL__"   # replaced at bake time
_ENABLE_SECRET    = "__ENABLE_SECRET__"       # replaced at bake time (True / False)
_ENABLE_RAINBOW   = "__ENABLE_RAINBOW__"      # replaced at bake time (True / False)
_APPDATA_DIR        = Path.home() / ".modpack-updater"
_DANCE_VIDEO_FILE = _APPDATA_DIR / "dance_video.mp4"
_DANCE_AUDIO_FILE = _APPDATA_DIR / "dance_audio.wav"
_DANCE_URL_FILE   = _APPDATA_DIR / "dance_url.txt"
_DANCE_WARMUP_WAV = _APPDATA_DIR / "warmup.wav"
_DANCE_AUDIO_LOCK     = threading.Lock()
_DANCE_ASSETS_READY   = threading.Event()
_DANCE_ASSETS_ERROR:   list[str | None] = [None]
_DANCE_CURRENT_STATUS: list[str]        = ["Getting ready..."]
_DANCE_CACHED_FPS:      list[float]     = [0.0]
_DANCE_CACHED_DURATION: list[float]     = [0.0]


def _generate_silent_wav(path: Path, duration_ms: int = 100) -> None:
    """Write a minimal silent stereo 16-bit 44100 Hz WAV to path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sample_rate = 44100
    num_samples = sample_rate * duration_ms // 1000
    data_size   = num_samples * 4  # stereo, 2 bytes/sample
    riff_size   = 36 + data_size
    path.write_bytes(
        b"RIFF" + riff_size.to_bytes(4, "little")
        + b"WAVEfmt \x10\x00\x00\x00"      # fmt chunk, 16 bytes
        + b"\x01\x00\x02\x00"              # PCM, 2 channels
        + sample_rate.to_bytes(4, "little")
        + (sample_rate * 4).to_bytes(4, "little")  # byte rate
        + b"\x04\x00\x10\x00"              # block align=4, bits/sample=16
        + b"data" + data_size.to_bytes(4, "little")
        + bytes(data_size)
    )


def _invalidate_dance_cache_if_url_changed() -> None:
    """Delete cached dance files if the baked URL no longer matches what was downloaded."""
    if not _DANCE_URL_FILE.exists():
        return
    if _DANCE_URL_FILE.read_text(encoding="utf-8").strip() != _DANCE_VIDEO_URL:
        for stale in (_DANCE_VIDEO_FILE, _DANCE_AUDIO_FILE, _DANCE_URL_FILE):
            try:
                stale.unlink()
            except OSError:
                pass


def _bundled_dance_path(filename: str) -> Path | None:
    """Return the path of a dance asset bundled into the exe, or None when running from source."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass is not None:
        candidate = Path(meipass) / "dance" / filename
        if candidate.exists():
            return candidate
    return None


FONT_TITLE        = ("Consolas", 13, "bold")
FONT_LARGE        = ("Consolas", 12)
FONT_BOLD         = ("Consolas", 11, "bold")
FONT_BODY         = ("Consolas", 10)
FONT_MONO         = ("Consolas", 9)
FONT_MONO_ITALIC  = ("Consolas", 9, "italic")


def _apply_colour_overrides(overrides: dict) -> None:
    global DARK_BG, PANEL_BG, ACCENT, ACCENT_HV, ACCENT2, TEXT, TEXT_DIM, RED, GREEN, YELLOW, KONAMI
    DARK_BG   = overrides.get("DARK_BG",   _COLOUR_DEFAULTS["DARK_BG"])
    PANEL_BG  = overrides.get("PANEL_BG",  _COLOUR_DEFAULTS["PANEL_BG"])
    ACCENT    = overrides.get("ACCENT",    _COLOUR_DEFAULTS["ACCENT"])
    ACCENT_HV = overrides.get("ACCENT_HV", _COLOUR_DEFAULTS["ACCENT_HV"])
    ACCENT2   = overrides.get("ACCENT2",   _COLOUR_DEFAULTS["ACCENT2"])
    TEXT      = overrides.get("TEXT",      _COLOUR_DEFAULTS["TEXT"])
    TEXT_DIM  = overrides.get("TEXT_DIM",  _COLOUR_DEFAULTS["TEXT_DIM"])
    RED       = overrides.get("RED",       _COLOUR_DEFAULTS["RED"])
    GREEN     = overrides.get("GREEN",     _COLOUR_DEFAULTS["GREEN"])
    YELLOW    = overrides.get("YELLOW",    _COLOUR_DEFAULTS["YELLOW"])
    KONAMI    = overrides.get("KONAMI",    _COLOUR_DEFAULTS["KONAMI"])


# -------------------------
# DANCE ASSET PREFETCH
# -------------------------

def _prefetch_dance_assets() -> None:
    global _yt_dlp, _HAS_IMAGEIO
    if hasattr(sys, "_MEIPASS"):
        _DANCE_ASSETS_READY.set()  # bundled exe — assets are always present
        return
    try:
        to_install = []
        if _yt_dlp is None:
            to_install.append("yt-dlp")
        if not _HAS_IMAGEIO:
            to_install.extend(["Pillow", "imageio", "imageio-ffmpeg"])
        if to_install:
            _DANCE_CURRENT_STATUS[0] = "Installing packages..."
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--prefer-binary", *to_install],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stdout[-600:])
            if _yt_dlp is None:
                _yt_dlp = importlib.import_module("yt_dlp")  # type: ignore[assignment]
            _HAS_IMAGEIO = True

        if _yt_dlp is None or not _HAS_IMAGEIO:
            raise RuntimeError("Required packages could not be installed.")

        _DANCE_VIDEO_FILE.parent.mkdir(parents=True, exist_ok=True)
        _invalidate_dance_cache_if_url_changed()
        if not _DANCE_VIDEO_FILE.exists():
            _DANCE_CURRENT_STATUS[0] = "Downloading video..."
            def _progress_hook(info: dict) -> None:
                if info.get("status") == "downloading":
                    total = info.get("total_bytes") or info.get("total_bytes_estimate", 0)
                    done  = info.get("downloaded_bytes", 0)
                    if total:
                        pct = int(done / total * 100)
                        _DANCE_CURRENT_STATUS[0] = f"Downloading video... {pct}%"
                elif info.get("status") == "finished":
                    _DANCE_CURRENT_STATUS[0] = "Merging streams..."
            ydl_opts = {
                "format": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]/mp4/best",
                "outtmpl": str(_DANCE_VIDEO_FILE),
                "merge_output_format": "mp4",
                "quiet": True,
                "no_warnings": True,
                "progress_hooks": [_progress_hook],
            }
            with _yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[union-attr]
                ydl.download([_DANCE_VIDEO_URL])
            if not _DANCE_VIDEO_FILE.exists():
                raise FileNotFoundError("Video download failed.")
            _DANCE_URL_FILE.write_text(_DANCE_VIDEO_URL, encoding="utf-8")

        with _DANCE_AUDIO_LOCK:
            if not _DANCE_AUDIO_FILE.exists():
                _DANCE_CURRENT_STATUS[0] = "Preparing audio..."
                imageio_ffmpeg_mod = importlib.import_module("imageio_ffmpeg")
                ffmpeg_exe = imageio_ffmpeg_mod.get_ffmpeg_exe()
                subprocess.run(
                    [ffmpeg_exe, "-i", str(_DANCE_VIDEO_FILE),
                     "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
                     str(_DANCE_AUDIO_FILE), "-y"],
                    check=True, capture_output=True,
                )
        # Pre-cache fps and duration so build_player never needs a probe reader on
        # the main thread (which would cause a visible lag spike).
        if _DANCE_CACHED_FPS[0] == 0.0 and _DANCE_VIDEO_FILE.exists():
            try:
                imageio_mod = importlib.import_module("imageio")
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    probe = imageio_mod.get_reader(str(_DANCE_VIDEO_FILE), "ffmpeg")
                meta = probe.get_meta_data()
                probe.close()
                fps_raw = meta.get("fps") or meta.get("source_fps")
                _DANCE_CACHED_FPS[0]      = float(fps_raw) if fps_raw and float(fps_raw) > 0 else 30.0
                _DANCE_CACHED_DURATION[0] = float(meta.get("duration") or 0)
            except Exception:
                pass
    except Exception as exc:
        _DANCE_ASSETS_ERROR[0] = str(exc)
    finally:
        _DANCE_ASSETS_READY.set()


_MODLOADER_NAMES: dict[str, str] = {
    "neoforge": "NeoForge",
    "forge":    "Forge",
    "fabric":   "Fabric",
    "quilt":    "Quilt",
}


def _format_modloader(modloader_id: str) -> str:
    """Convert a modloader id (e.g. 'neoforge-21.1.229') to a display string ('NeoForge 21.1.229')."""
    if "-" in modloader_id:
        prefix, version = modloader_id.split("-", 1)
        return f"{_MODLOADER_NAMES.get(prefix, prefix.capitalize())} {version}"
    return modloader_id


# -------------------------
# GUI APP
# -------------------------

class UpdaterApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{MODPACK_NAME} Modpack Updater")
        self.configure(bg=DARK_BG)
        self.minsize(760, 600)

        self.prefs: dict = load_prefs()

        # Apply saved colour overrides before building any UI
        saved_colours = self.prefs.get("colours", {})
        if saved_colours:
            _apply_colour_overrides(saved_colours)
            self.configure(bg=DARK_BG)

        try:
            self.geometry(self.prefs.get("geometry", "760x600"))
        except tk.TclError:
            self.geometry("760x600")

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.modpack_dir: Path | None       = None
        self.local_version: str | None      = None
        self.latest_version: str | None     = None
        self.target_version: str | None     = None
        self.fresh_install: bool            = False
        self.versions_list: list[str]       = []
        self.versions_data: dict            = {}
        self.release_message: str           = ""
        self.target_modloader: str          = ""
        self.old_snapshot: dict             = {}
        self.new_snapshot: dict             = {}
        self.update_plan: dict              = {}

        # Widget refs reused across screens
        self._current_frame: tk.Frame | None        = None
        self._current_builder                       = None
        self._logo_image: tk.PhotoImage | None      = None
        self._icon_image: tk.PhotoImage | None      = None
        self._header_logo_label: tk.Label | None    = None
        self._header_title_label: tk.Label | None   = None
        self._update_progress_label: tk.Label | None = None
        self._update_button_row: tk.Frame | None    = None
        self._path_var      = tk.StringVar()
        self._picker_status = tk.StringVar()
        self._progress_var  = tk.StringVar()
        self._log_text: tk.Text | None              = None

        self._konami_progress: int                  = 0
        self._konami_unlocked: bool                 = False
        self._konami_button_row: tk.Frame | None    = None
        self._dance_visited: bool                   = False
        self._border_rainbow_started: bool          = False
        self._dance_audio_start: float | None       = None
        self._dance_stream_active:  list[bool]           = [False]
        self._dance_display_active: list[bool]           = [False]
        self._dance_frame_queue:    queue.Queue          = queue.Queue(maxsize=60)
        self._dance_start_time:     list[float]          = [float("inf")]
        self._dance_fps_ref:        list[float]          = [30.0]
        self._dance_display_size:   list[tuple[int,int]] = [(640, 360)]
        if _ENABLE_SECRET:
            threading.Thread(target=_prefetch_dance_assets, daemon=True).start()
            self.bind_all("<Key>", self._on_key_for_konami)

        if LOGO_URL:
            threading.Thread(target=self._prefetch_logo, daemon=True).start()

        saved_dir = self.prefs.get("modpack_dir")
        if saved_dir and Path(saved_dir).is_dir():
            self.modpack_dir = Path(saved_dir)
            self._show_checking()
        else:
            self._show_folder_picker()

    # ---- logo prefetch ----

    _LOGO_TARGET_HEIGHT = 32

    def _prefetch_logo(self) -> None:
        try:
            request = urllib.request.Request(LOGO_URL, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=10) as response:
                image_bytes = response.read()
            encoded = base64.b64encode(image_bytes).decode("ascii")
            def create_image() -> None:
                try:
                    full_image = tk.PhotoImage(data=encoded)
                    # Window / taskbar icon — keep close to original size, cap at 256px
                    icon_image = full_image
                    if icon_image.height() > 256:
                        icon_factor = max(1, round(icon_image.height() / 64))
                        icon_image = full_image.subsample(icon_factor)
                    self._icon_image = icon_image
                    self.iconphoto(True, icon_image)
                    # Header image — ~32px tall
                    header_image = full_image
                    if full_image.height() > self._LOGO_TARGET_HEIGHT:
                        factor = max(1, round(full_image.height() / self._LOGO_TARGET_HEIGHT))
                        header_image = full_image.subsample(factor)
                    self._logo_image = header_image
                    if self._header_logo_label is not None and self._header_title_label is not None:
                        self._header_logo_label.config(image=header_image)
                        self._header_logo_label.pack(
                            side="left", padx=(0, 8), before=self._header_title_label,
                        )
                        self._header_title_label.config(text=f"{MODPACK_NAME} Modpack Updater")
                except Exception:
                    pass
            self.after(0, create_image)
        except Exception:
            pass

    # ---- window lifecycle ----

    def _on_close(self) -> None:
        self.prefs["geometry"] = self.geometry()
        save_prefs(self.prefs)
        self.destroy()

    # ---- frame management ----

    def _swap_frame(self, builder, *, no_border: bool = False) -> None:
        if self._current_frame is not None:
            self._current_frame.destroy()
        self._current_builder = builder
        self._konami_button_row = None
        new_frame = builder()
        if self._dance_visited and not no_border:
            if _ENABLE_RAINBOW and not self._border_rainbow_started:
                self._start_border_rainbow()
            new_frame.pack(fill="both", expand=True, padx=8, pady=8)
        else:
            new_frame.pack(fill="both", expand=True)
        self._current_frame = new_frame

    def _start_border_rainbow(self) -> None:
        self._border_rainbow_started = True

        def step() -> None:
            r, g, b = colorsys.hsv_to_rgb(random.random(), 1.0, 1.0)
            self.configure(bg=f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}")
            self.after(150, step)

        step()

    def _header(self, parent: tk.Misc, subtitle: str = "") -> tk.Frame:
        header = tk.Frame(parent, bg=ACCENT2, padx=20, pady=14)
        header.pack(fill="x")

        logo_label = tk.Label(header, bg=ACCENT2)
        self._header_logo_label = logo_label
        if self._logo_image is not None:
            logo_label.config(image=self._logo_image)
            logo_label.pack(side="left", padx=(0, 8))
            title_text = f"{MODPACK_NAME} Modpack Updater"
        else:
            title_text = f"⛏  {MODPACK_NAME} Modpack Updater"

        title_label = tk.Label(
            header, text=title_text,
            font=FONT_TITLE, bg=ACCENT2, fg=ACCENT,
        )
        title_label.pack(side="left")
        self._header_title_label = title_label
        tk.Button(
            header, text="⚙",
            font=FONT_BODY, bg=ACCENT2, fg=TEXT_DIM,
            activebackground=ACCENT2, activeforeground=TEXT,
            relief="flat", bd=0, padx=8, cursor="hand2",
            command=self._show_colour_settings,
        ).pack(side="right")
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

    # ---- screen: settings ----

    def _show_colour_settings(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Settings")
        dialog.configure(bg=DARK_BG)
        saved_settings_geo = self.prefs.get("settings_geometry")
        if saved_settings_geo:
            dialog.geometry(saved_settings_geo)
        else:
            dialog.geometry("620x580")
        dialog.resizable(True, True)
        dialog.transient(self)
        dialog.grab_set()

        def _on_settings_close(event: tk.Event) -> None:
            if event.widget is dialog:
                self.prefs["settings_geometry"] = dialog.geometry()
                save_prefs(self.prefs)
        dialog.bind("<Destroy>", _on_settings_close)

        current_colours: dict[str, str] = {**_COLOUR_DEFAULTS, **self.prefs.get("colours", {})}
        colour_vars: dict[str, tk.StringVar] = {}

        header = tk.Frame(dialog, bg=ACCENT2, padx=16, pady=10)
        header.pack(fill="x")
        tk.Label(header, text="Settings", font=FONT_BOLD, bg=ACCENT2, fg=ACCENT).pack(side="left")

        body = tk.Frame(dialog, bg=DARK_BG, padx=20, pady=16)
        body.pack(fill="both", expand=True)

        # --- Modpack Location ---
        tk.Label(
            body, text="Modpack Location", font=FONT_BOLD, bg=DARK_BG, fg=TEXT,
        ).pack(anchor="w", pady=(0, 6))

        location_var = tk.StringVar(value=self.prefs.get("modpack_dir", ""))
        location_row = tk.Frame(body, bg=DARK_BG)
        location_row.pack(fill="x", pady=(0, 16))

        tk.Entry(
            location_row, textvariable=location_var, font=FONT_MONO,
            bg=PANEL_BG, fg=TEXT, relief="flat", insertbackground=TEXT,
        ).pack(side="left", fill="x", expand=True, ipady=3)

        def browse_location() -> None:
            initial = location_var.get() or str(Path.home())
            chosen = filedialog.askdirectory(
                initialdir=initial, title="Select your modpack folder", parent=dialog,
            )
            if chosen:
                location_var.set(chosen)

        tk.Button(
            location_row, text="Browse…", font=FONT_BODY,
            bg=PANEL_BG, fg=TEXT_DIM,
            activebackground=ACCENT2, activeforeground=TEXT,
            relief="flat", bd=0, padx=14, cursor="hand2",
            command=browse_location,
        ).pack(side="left", padx=(8, 0))

        # --- Colour Settings ---
        tk.Label(
            body, text="Colour Settings", font=FONT_BOLD, bg=DARK_BG, fg=TEXT,
        ).pack(anchor="w", pady=(0, 6))

        for key, label_text in _COLOUR_LABELS.items():
            row = tk.Frame(body, bg=DARK_BG)
            row.pack(fill="x", pady=3)

            tk.Label(
                row, text=label_text, font=FONT_BODY, bg=DARK_BG, fg=TEXT,
                width=50, anchor="w",
            ).pack(side="left")

            colour_var = tk.StringVar(value=current_colours.get(key, _COLOUR_DEFAULTS[key]))
            colour_vars[key] = colour_var

            swatch = tk.Label(row, width=3, bg=colour_var.get(), relief="flat")
            swatch.pack(side="left", padx=(0, 4))

            entry = tk.Entry(
                row, textvariable=colour_var, font=FONT_MONO,
                bg=PANEL_BG, fg=TEXT, relief="flat", width=9,
                insertbackground=TEXT,
            )
            entry.pack(side="left", ipady=3)

            def make_pick(var: tk.StringVar, sw: tk.Label):
                def pick() -> None:
                    result = colorchooser.askcolor(color=var.get(), parent=dialog, title="Pick colour")
                    if result and result[1]:
                        var.set(result[1])
                        sw.config(bg=result[1])
                return pick

            def make_trace(var: tk.StringVar, sw: tk.Label) -> None:
                def on_change(*_) -> None:
                    value = var.get().strip()
                    if len(value) == 7 and value.startswith("#"):
                        try:
                            dialog.winfo_rgb(value)
                            sw.config(bg=value)
                        except tk.TclError:
                            pass
                var.trace_add("write", on_change)

            tk.Button(
                row, text="Pick…", font=FONT_MONO,
                bg=PANEL_BG, fg=TEXT_DIM,
                activebackground=ACCENT2, activeforeground=TEXT,
                relief="flat", bd=0, padx=8, cursor="hand2",
                command=make_pick(colour_var, swatch),
            ).pack(side="left", padx=(4, 0))
            make_trace(colour_var, swatch)

        button_row = tk.Frame(dialog, bg=DARK_BG)
        button_row.pack(fill="x", padx=20, pady=12)

        def reset_defaults() -> None:
            for key, var in colour_vars.items():
                var.set(_COLOUR_DEFAULTS[key])

        def apply_changes() -> None:
            new_colours = {key: var.get() for key, var in colour_vars.items()}
            _apply_colour_overrides(new_colours)
            self.prefs["colours"] = new_colours
            new_location = location_var.get().strip()
            if new_location:
                new_path = Path(new_location)
                if not new_path.is_dir():
                    messagebox.showerror(
                        "Invalid folder", f"Folder does not exist:\n{new_path}", parent=dialog,
                    )
                    return
                if not is_likely_modpack_folder(new_path):
                    confirmed = messagebox.askyesno(
                        "Folder may not be a modpack",
                        f"This folder does not contain a 'mods' subfolder or 'config/bcc-common.toml'.\n\n"
                        f"{new_path}\n\nContinue anyway?",
                        parent=dialog,
                    )
                    if not confirmed:
                        return
                self.prefs["modpack_dir"] = new_location
                self.modpack_dir = new_path
                self._path_var.set(new_location)
            save_prefs(self.prefs)
            self.configure(bg=DARK_BG)
            dialog.destroy()
            if self._current_builder is not None:
                self._swap_frame(self._current_builder)

        tk.Button(
            button_row, text="Reset colours", font=FONT_BODY,
            bg=PANEL_BG, fg=TEXT_DIM,
            activebackground=ACCENT2, activeforeground=TEXT,
            relief="flat", bd=0, padx=14, pady=8, cursor="hand2",
            command=reset_defaults,
        ).pack(side="left")
        tk.Button(
            button_row, text="Cancel", font=FONT_BODY,
            bg=PANEL_BG, fg=TEXT_DIM,
            activebackground=ACCENT2, activeforeground=TEXT,
            relief="flat", bd=0, padx=14, pady=8, cursor="hand2",
            command=dialog.destroy,
        ).pack(side="right", padx=(10, 0))
        self._primary_button(button_row, "Apply", apply_changes).pack(side="right")

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
            tk.Entry(
                path_row, textvariable=self._path_var,
                font=FONT_MONO, bg=PANEL_BG, fg=TEXT,
                relief="flat", insertbackground=TEXT,
            ).pack(side="left", fill="x", expand=True, ipady=6)
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
            self._secondary_button(button_row, "Close", self._on_close).pack(side="right", padx=(10, 0))
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
                f"This folder does not contain a 'mods' subfolder or 'config/bcc-common.toml'.\n\n"
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
            self.local_version = read_installed_version(self.modpack_dir)

            versions_data       = fetch_versions()
            self.versions_data  = versions_data
            self.latest_version = versions_data.get("latest")
            self.versions_list  = [
                str(entry["version"])
                for entry in versions_data.get("versions", [])
            ]

            if not self.latest_version:
                self.after(0, lambda: self._show_outcome(
                    success=False,
                    message="Could not determine latest version from versions.json.",
                ))
                return

            if self.local_version == self.latest_version:
                self.after(0, self._show_up_to_date)
                return

            self.target_version = self.latest_version
            self.after(0, self._show_version_options)
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
            self._konami_button_row = button_row
            if self._konami_unlocked:
                self._add_dance_button(button_row)
            self._secondary_button(
                button_row, "Choose version…", self._show_version_options,
            ).pack(side="right", padx=(10, 0))
            self._primary_button(button_row, "Close", self._on_close).pack(side="right")
            return frame
        self._swap_frame(build)

    # ---- screen: version options ----

    def _show_version_options(self) -> None:
        def build() -> tk.Frame:
            frame = tk.Frame(self, bg=DARK_BG)
            self._header(frame)

            body = tk.Frame(frame, bg=DARK_BG, padx=20, pady=20)
            body.pack(fill="both", expand=True)

            tk.Label(
                body, text="Select version to install",
                font=FONT_BOLD, bg=DARK_BG, fg=TEXT,
            ).pack(anchor="w")
            tk.Label(
                body, text="Defaults to the latest available version.",
                font=FONT_BODY, bg=DARK_BG, fg=TEXT_DIM,
            ).pack(anchor="w", pady=(2, 16))

            version_row = tk.Frame(body, bg=DARK_BG)
            version_row.pack(fill="x", pady=(0, 8))
            tk.Label(
                version_row, text="Version:", font=FONT_BODY,
                bg=DARK_BG, fg=TEXT, width=12, anchor="w",
            ).pack(side="left")

            default_version = self.target_version or self.latest_version or (
                self.versions_list[0] if self.versions_list else ""
            )
            options = [
                f"{version_str} (latest)" if version_str == self.latest_version else version_str
                for version_str in self.versions_list
            ]
            selected_label = (
                f"{default_version} (latest)"
                if default_version == self.latest_version
                else default_version
            )
            version_var = tk.StringVar(value=selected_label if options else "")

            modloader_by_version: dict[str, str] = {
                str(entry["version"]): _format_modloader(entry["modloader"])
                for entry in self.versions_data.get("versions", [])
                if entry.get("modloader")
            }
            modloader_var = tk.StringVar(
                value=modloader_by_version.get(default_version, "")
            )

            def on_version_change(*_) -> None:
                version_str = version_var.get().replace(" (latest)", "").strip()
                modloader_var.set(modloader_by_version.get(version_str, ""))

            version_var.trace_add("write", on_version_change)

            if options:
                menu = tk.OptionMenu(version_row, version_var, *options)
                menu.config(
                    font=FONT_BODY, bg=PANEL_BG, fg=TEXT,
                    activebackground=ACCENT2, activeforeground=TEXT,
                    relief="flat", bd=0, highlightthickness=0,
                )
                menu["menu"].config(
                    font=FONT_BODY, bg=PANEL_BG, fg=TEXT,
                    activebackground=ACCENT2, activeforeground=TEXT,
                )
                menu.pack(side="left")
            else:
                tk.Label(
                    version_row, text="No versions available",
                    font=FONT_BODY, bg=DARK_BG, fg=RED,
                ).pack(side="left")

            if modloader_by_version:
                modloader_row = tk.Frame(body, bg=DARK_BG)
                modloader_row.pack(fill="x", pady=(0, 8))
                tk.Label(
                    modloader_row, text="Modloader:", font=FONT_BODY,
                    bg=DARK_BG, fg=TEXT, width=12, anchor="w",
                ).pack(side="left")
                tk.Label(
                    modloader_row, textvariable=modloader_var,
                    font=FONT_MONO, bg=DARK_BG, fg=TEXT_DIM,
                ).pack(side="left")

            fresh_var = tk.BooleanVar(value=self.fresh_install or not self.local_version)
            fresh_row = tk.Frame(body, bg=DARK_BG)
            fresh_row.pack(fill="x", pady=(12, 4))
            tk.Checkbutton(
                fresh_row,
                text="Fresh install  (deletes all existing mods and re-downloads everything)",
                variable=fresh_var, font=FONT_BODY,
                bg=DARK_BG, fg=TEXT, selectcolor=PANEL_BG,
                activebackground=DARK_BG, activeforeground=TEXT,
            ).pack(anchor="w")

            if self.local_version:
                tk.Label(
                    body, text=f"Installed: v{self.local_version}",
                    font=FONT_MONO, bg=DARK_BG, fg=TEXT_DIM,
                ).pack(anchor="w", pady=(12, 0))

            def confirm() -> None:
                raw_version = version_var.get().replace(" (latest)", "").strip()
                self.target_version = raw_version
                self.fresh_install  = fresh_var.get()
                self._show_fetching_snapshots()

            button_row = tk.Frame(frame, bg=DARK_BG)
            button_row.pack(fill="x", padx=20, pady=12)
            self._secondary_button(button_row, "Cancel", self._on_close).pack(side="right", padx=(10, 0))
            self._primary_button(button_row, "Continue  →", confirm).pack(side="right")
            return frame
        self._swap_frame(build)

    # ---- screen: fetching snapshots ----

    def _show_fetching_snapshots(self) -> None:
        def build() -> tk.Frame:
            frame = tk.Frame(self, bg=DARK_BG)
            self._header(frame)
            tk.Label(
                frame, text="Fetching version data…",
                font=FONT_LARGE, bg=DARK_BG, fg=TEXT, pady=80,
            ).pack(fill="x")
            return frame
        self._swap_frame(build)
        threading.Thread(target=self._run_fetch_snapshots, daemon=True).start()

    def _run_fetch_snapshots(self) -> None:
        try:
            assert self.modpack_dir is not None and self.target_version is not None

            target_commit = None
            old_commit    = None
            for entry in self.versions_data.get("versions", []):
                version_str = str(entry["version"])
                if version_str == str(self.target_version):
                    target_commit = entry["commit"]
                    self.release_message  = entry.get("message", "")
                    self.target_modloader = entry.get("modloader", "")
                if (not self.fresh_install
                        and self.local_version
                        and version_str == str(self.local_version)):
                    old_commit = entry["commit"]

            if not target_commit:
                self.after(0, lambda: self._show_outcome(
                    success=False,
                    message=f"Could not find snapshot for v{self.target_version}.",
                ))
                return

            server_only_ids: set[str] = set(
                str(pid) for pid in self.versions_data.get("server_only_ids", [])
            )
            self.new_snapshot = filter_for_client(fetch_snapshot(target_commit), server_only_ids)
            self.old_snapshot = filter_for_client(fetch_snapshot(old_commit), server_only_ids) if old_commit else {}
            self.update_plan  = build_update_plan(
                self.old_snapshot, self.new_snapshot, self.modpack_dir,
            )
            self.after(0, self._show_changelog)
        except Exception as exc:
            error_message = f"Error fetching version data:\n\n{exc}"
            self.after(0, lambda: self._show_outcome(success=False, message=error_message))

    # ---- screen: changelog ----

    def _show_changelog(self) -> None:
        def build() -> tk.Frame:
            frame = tk.Frame(self, bg=DARK_BG)
            from_label = (
                f"v{self.local_version}"
                if self.local_version and not self.fresh_install
                else "fresh install"
            )
            self._header(frame, subtitle=f"{from_label}  →  v{self.target_version}")

            changes     = diff_snapshots(self.old_snapshot, self.new_snapshot)
            num_added   = len(changes["added"])
            num_updated = len(changes["updated"])

            # On fresh install, show existing files that Phase 0 will wipe.
            existing_files: list[str] = []
            if self.fresh_install and self.modpack_dir is not None:
                for category in ("mods", "shaderpacks", "resourcepacks"):
                    category_dir = self.modpack_dir / category
                    if category_dir.is_dir():
                        for f in sorted(category_dir.iterdir(), key=lambda p: p.name.lower()):
                            existing_files.append(f.name)
            num_removed = len(existing_files) if self.fresh_install else len(changes["removed"])

            stats_row = tk.Frame(frame, bg=DARK_BG)
            stats_row.pack()
            if self.fresh_install:
                stat_items = (
                    (str(num_added),   GREEN),
                    (" to download",   TEXT_DIM),
                    ("  ·  ",          TEXT_DIM),
                    (str(num_removed), RED),
                    (" to delete",     TEXT_DIM),
                )
            else:
                stat_items = (
                    (str(num_added),   GREEN),
                    (" added",         TEXT_DIM),
                    ("  ·  ",          TEXT_DIM),
                    (str(num_removed), RED),
                    (" removed",       TEXT_DIM),
                    ("  ·  ",          TEXT_DIM),
                    (str(num_updated), YELLOW),
                    (" updated",       TEXT_DIM),
                )
            for label_text, colour in stat_items:
                tk.Label(
                    stats_row, text=label_text,
                    font=FONT_BODY, bg=DARK_BG, fg=colour, pady=8,
                ).pack(side="left")

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

            text.tag_config("section_added",   font=FONT_BOLD,        foreground=GREEN)
            text.tag_config("section_removed", font=FONT_BOLD,        foreground=RED)
            text.tag_config("section_updated", font=FONT_BOLD,        foreground=YELLOW)
            text.tag_config("added",           font=FONT_MONO,        foreground=GREEN)
            text.tag_config("removed",         font=FONT_MONO,        foreground=RED)
            text.tag_config("updated",         font=FONT_MONO,        foreground=YELLOW)
            text.tag_config("dim",             font=FONT_MONO,        foreground=TEXT_DIM)
            text.tag_config("placeholder",     font=FONT_MONO_ITALIC, foreground=TEXT_DIM)

            text.config(state="normal")

            if self.fresh_install:
                text.insert("end", "To Download\n", "section_added")
                text.insert("end", "\n")
                if changes["added"]:
                    for _, entry in changes["added"]:
                        text.insert("end", f"  - {entry['name']}\n", "added")
                else:
                    text.insert("end", "  No mods to download.\n", "placeholder")
                text.insert("end", "\n")

                text.insert("end", "To Delete\n", "section_removed")
                text.insert("end", "\n")
                if existing_files:
                    for filename in existing_files:
                        text.insert("end", f"  - {filename}\n", "removed")
                else:
                    text.insert("end", "  No existing files to delete.\n", "placeholder")
            else:
                text.insert("end", "Added\n", "section_added")
                text.insert("end", "\n")
                if changes["added"]:
                    for _, entry in changes["added"]:
                        text.insert("end", f"  - {entry['name']}\n", "added")
                else:
                    text.insert("end", "  No mods added.\n", "placeholder")
                text.insert("end", "\n")

                text.insert("end", "Removed\n", "section_removed")
                text.insert("end", "\n")
                if changes["removed"]:
                    for _, entry in changes["removed"]:
                        text.insert("end", f"  - {entry['name']}\n", "removed")
                else:
                    text.insert("end", "  No mods removed.\n", "placeholder")
                text.insert("end", "\n")

                text.insert("end", "Updated\n", "section_updated")
                text.insert("end", "\n")
                if changes["updated"]:
                    for _, _, new_entry in changes["updated"]:
                        text.insert("end", f"  - {new_entry['name']}\n", "updated")
                else:
                    text.insert("end", "  No mods updated.\n", "placeholder")

            text.config(state="disabled")

            button_row = tk.Frame(frame, bg=DARK_BG)
            button_row.pack(fill="x", padx=20, pady=12)
            self._secondary_button(button_row, "Cancel", self._on_close).pack(side="right", padx=(10, 0))
            self._secondary_button(
                button_row, "←  Back", self._show_version_options,
            ).pack(side="right", padx=(10, 0))
            confirm_label = "Confirm & Reset  →" if self.fresh_install else "Confirm & Update  →"
            self._primary_button(
                button_row, confirm_label, self._show_updating,
            ).pack(side="right")
            return frame
        self._swap_frame(build)

    # ---- screen: updating ----

    def _show_updating(self) -> None:
        def build() -> tk.Frame:
            frame = tk.Frame(self, bg=DARK_BG)
            from_label = (
                f"v{self.local_version or '?'}" if not self.fresh_install else "fresh install"
            )
            self._header(frame, subtitle=f"{from_label}  →  v{self.target_version}")
            self._progress_var.set("Starting…")
            progress_label = tk.Label(
                frame, textvariable=self._progress_var,
                font=FONT_BODY, bg=DARK_BG, fg=TEXT, pady=8,
            )
            progress_label.pack(fill="x", padx=20)
            self._update_progress_label = progress_label

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
            log_text.tag_config("log_add",      foreground=GREEN,   font=FONT_MONO)
            log_text.tag_config("log_update",   foreground=YELLOW,  font=FONT_MONO)
            log_text.tag_config("log_remove",   foreground=RED,     font=FONT_MONO)
            log_text.tag_config("log_download", foreground=TEXT,    font=FONT_MONO)
            log_text.tag_config("log_warn",     foreground=YELLOW,  font=FONT_MONO)
            log_text.tag_config("log_error",    foreground=RED,     font=FONT_MONO)
            log_text.tag_config("log_version",  foreground=ACCENT,  font=FONT_MONO)
            self._log_text = log_text

            button_row = tk.Frame(frame, bg=DARK_BG)
            button_row.pack(fill="x", padx=20, pady=12)
            self._update_button_row = button_row
            return frame
        self._swap_frame(build)
        threading.Thread(target=self._run_update, daemon=True).start()

    def _log(self, message: str, tag: str = "") -> None:
        log_text = self._log_text
        if log_text is None:
            return
        def append() -> None:
            at_bottom = log_text.yview()[1] >= 0.99
            log_text.config(state="normal")
            log_text.insert("end", message + "\n", tag or ())
            if at_bottom:
                log_text.see("end")
            log_text.config(state="disabled")
        self.after(0, append)

    def _set_progress(self, message: str) -> None:
        self.after(0, lambda: self._progress_var.set(message))

    def _run_update(self) -> None:
        assert self.modpack_dir is not None and self.target_version is not None
        # Temp dir inside the modpack folder so the final move is a same-filesystem rename.
        tmp_dir = self.modpack_dir / ".update_tmp"
        try:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)
            tmp_dir.mkdir(parents=True, exist_ok=True)

            downloads = self.update_plan["download"]
            deletions = self.update_plan["delete"]
            downloaded_files: list[tuple[Path, str, bool]] = []
            failed_downloads: list[str] = []

            # Phase 0 (fresh install only): wipe all existing mod files
            if self.fresh_install:
                self._set_progress("Clearing existing mods…")
                for category in ("mods", "shaderpacks", "resourcepacks"):
                    category_dir = self.modpack_dir / category
                    if category_dir.is_dir():
                        for existing_file in list(category_dir.iterdir()):
                            try:
                                existing_file.unlink()
                                self._log(f"  - {existing_file.name}", "log_remove")
                            except OSError as exc:
                                self._log(f"  [warn] could not delete {existing_file.name}: {exc}", "log_warn")

            # Phase 1: download all new/updated files concurrently
            completed_count = 0
            count_lock = threading.Lock()
            if downloads:
                self._set_progress(f"Downloading 0 / {len(downloads)}…")
            with ThreadPoolExecutor(max_workers=10) as executor:
                future_map = {
                    executor.submit(download_mod_file, project_id, file_id, tmp_dir): (project_id, display_name, is_update)
                    for project_id, file_id, display_name, is_update in downloads
                }
                for future in as_completed(future_map):
                    _, display_name, is_update = future_map[future]
                    local_path = future.result()
                    with count_lock:
                        completed_count += 1
                        count = completed_count
                    self._set_progress(f"Downloading {count} / {len(downloads)}…")
                    if local_path is None:
                        failed_downloads.append(display_name)
                        self._log(f"  [FAIL] {display_name}", "log_error")
                        continue
                    category = classify_downloaded_file(local_path)
                    downloaded_files.append((local_path, category, is_update))
                    self._log(f"  ↓ {display_name}", "log_download")

            if failed_downloads:
                self._log("")
                self._log(f"[ERROR] {len(failed_downloads)} file(s) failed to download.", "log_error")
                self._log("        Your modpack was not modified.", "log_error")
                self.after(0, lambda: self._show_outcome(
                    success=False,
                    message=(
                        f"{len(failed_downloads)} file(s) failed to download.\n"
                        "Your modpack was not modified. Check your internet "
                        "connection and try again."
                    ),
                ))
                return

            # Phase 2: delete old files (skipped for fresh install — already wiped in phase 0)
            if not self.fresh_install:
                self._set_progress("Applying changes…")
                self._log("")
                for old_path, display_name in deletions:
                    try:
                        old_path.unlink()
                        self._log(f"  - {display_name}", "log_remove")
                    except OSError as exc:
                        self._log(f"  [warn] could not delete {display_name}: {exc}", "log_warn")

            # Phase 3: move downloaded files into place
            self._set_progress("Installing files…")
            for tmp_path, category, is_update in downloaded_files:
                dest_dir = self.modpack_dir / category
                dest_dir.mkdir(parents=True, exist_ok=True)
                destination = dest_dir / tmp_path.name
                if destination.exists():
                    destination.unlink()
                try:
                    shutil.move(str(tmp_path), str(destination))
                    icon = "~" if is_update else "+"
                    self._log(f"  {icon} {destination.name}", "log_update" if is_update else "log_add")
                except OSError as exc:
                    self._log(f"  [warn] could not install {tmp_path.name}: {exc}", "log_warn")

            # Phase 4: record the installed version last — a crash during
            # phases 1-3 leaves bcc-common.toml at the previous version.
            write_installed_version(self.modpack_dir, self.target_version)
            self._log("")
            self._log(f"bcc-common.toml → {self.target_version}", "log_version")

            self.after(0, lambda: self._show_outcome(
                success=True,
                message=f"Updated to v{self.target_version}. Launch Minecraft to play.",
            ))
        except Exception as exc:
            self.after(0, lambda: self._show_outcome(
                success=False, message=f"Update failed:\n\n{exc}",
            ))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ---- screen: outcome (success or error) ----

    def _show_outcome(self, success: bool, message: str) -> None:
        colour = ACCENT if success else RED
        if self._update_button_row is not None:
            # Inline result on the download screen — just update the progress label
            # and add a Finish button; don't navigate away.
            self._set_progress(message)
            progress_label  = self._update_progress_label
            button_row      = self._update_button_row
            if progress_label is not None:
                self.after(0, lambda: progress_label.config(fg=colour))
            def add_finish_button() -> None:
                self._primary_button(button_row, "Finish", self._on_close).pack(side="right")
                if success:
                    self._konami_button_row = button_row
                    if self._konami_unlocked:
                        self._add_dance_button(button_row)
            self.after(0, add_finish_button)
            # Update _current_builder so that returning from the dance screen
            # shows a proper outcome screen rather than rebuilding the blank
            # updating screen (which would show "Starting…" with an empty log).
            _success, _message, _colour = success, message, colour
            def _outcome_builder() -> tk.Frame:
                self._update_button_row = None
                self._update_progress_label = None
                frame = tk.Frame(self, bg=DARK_BG)
                self._header(frame)
                body = tk.Frame(frame, bg=DARK_BG, padx=20, pady=40)
                body.pack(fill="both", expand=True)
                icon = "✓" if _success else "✗"
                tk.Label(
                    body, text=icon,
                    font=("Consolas", 32, "bold"), bg=DARK_BG, fg=_colour,
                ).pack(pady=(0, 8))
                tk.Label(
                    body, text=_message,
                    font=FONT_BODY, bg=DARK_BG, fg=TEXT,
                    wraplength=560, justify="center",
                ).pack()
                btn_row = tk.Frame(frame, bg=DARK_BG)
                btn_row.pack(fill="x", padx=20, pady=12)
                self._konami_button_row = btn_row
                if self._konami_unlocked:
                    self._add_dance_button(btn_row)
                self._primary_button(btn_row, "Close", self._on_close).pack(side="right")
                return frame
            self._current_builder = _outcome_builder
        else:
            # Pre-update error (network failure, bad snapshot, etc.) — show outcome screen.
            def build() -> tk.Frame:
                frame = tk.Frame(self, bg=DARK_BG)
                self._header(frame)
                body = tk.Frame(frame, bg=DARK_BG, padx=20, pady=40)
                body.pack(fill="both", expand=True)
                icon = "✓" if success else "✗"
                tk.Label(
                    body, text=icon,
                    font=("Consolas", 32, "bold"), bg=DARK_BG, fg=colour,
                ).pack(pady=(0, 8))
                tk.Label(
                    body, text=message,
                    font=FONT_BODY, bg=DARK_BG, fg=TEXT,
                    wraplength=560, justify="center",
                ).pack()
                button_row = tk.Frame(frame, bg=DARK_BG)
                button_row.pack(fill="x", padx=20, pady=12)
                self._primary_button(button_row, "Close", self._on_close).pack(side="right")
                return frame
            self._swap_frame(build)


    # ---- easter egg: konami code ----

    def _on_key_for_konami(self, event: tk.Event) -> None:
        expected = _KONAMI_SEQUENCE[self._konami_progress]
        if event.keysym == expected:
            self._konami_progress += 1
            if self._konami_progress == len(_KONAMI_SEQUENCE):
                self._konami_progress = 0
                self._on_konami_complete()
        else:
            self._konami_progress = 1 if event.keysym == _KONAMI_SEQUENCE[0] else 0

    def _on_konami_complete(self) -> None:
        self._konami_unlocked = True
        if self._konami_button_row is not None:
            try:
                self._add_dance_button(self._konami_button_row)
            except tk.TclError:
                pass

    def _add_dance_button(self, button_row: tk.Frame) -> None:
        tk.Button(
            button_row, text="Dance? 🎵",
            font=FONT_BODY, bg=KONAMI, fg="white",
            activebackground=KONAMI, activeforeground="white",
            relief="flat", bd=0, padx=14, pady=8, cursor="hand2",
            command=self._show_dance,
        ).pack(side="left")

    def _show_dance(self) -> None:
        self._dance_visited = True
        previous_builder = self._current_builder

        def go_back() -> None:
            if previous_builder is not None:
                self._swap_frame(previous_builder)
            else:
                self._on_close()

        def add_header_with_question(frame: tk.Frame) -> None:
            self._header(frame)
            if self._header_title_label is not None:
                self._header_title_label.configure(
                    text=self._header_title_label.cget("text") + "?"
                )

        def add_back_button(frame: tk.Frame) -> None:
            row = tk.Frame(frame, bg=DARK_BG)
            row.pack(fill="x", padx=20, pady=12, side="bottom")
            self._secondary_button(row, "← Back", go_back).pack(side="right")

        def start_rainbow_flash(frame: tk.Frame, body: tk.Frame) -> None:
            after_id: list[str | None] = [None]

            def step() -> None:
                r, g, b = colorsys.hsv_to_rgb(random.random(), 1.0, 1.0)
                color = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
                try:
                    body.configure(bg=color)
                    after_id[0] = frame.after(150, step)
                except tk.TclError:
                    pass

            def on_destroy(event: tk.Event) -> None:
                if event.widget is frame and after_id[0] is not None:
                    frame.after_cancel(after_id[0])

            frame.bind("<Destroy>", on_destroy)
            step()

        def build_loading() -> tk.Frame:
            frame = tk.Frame(self, bg=DARK_BG)
            add_header_with_question(frame)
            add_back_button(frame)
            body = tk.Frame(frame, bg=DARK_BG)
            body.pack(fill="both", expand=True)
            centre_panel = tk.Frame(body, bg=PANEL_BG)
            centre_panel.pack(fill="both", expand=True, padx=8, pady=8)
            status_var = tk.StringVar(value=_DANCE_CURRENT_STATUS[0])
            tk.Label(
                centre_panel, textvariable=status_var,
                font=FONT_BODY, fg=TEXT, bg=PANEL_BG, justify="center",
            ).pack(expand=True, pady=(20, 8))
            bar = ttk.Progressbar(
                centre_panel, orient="horizontal",
                length=300, mode="indeterminate", maximum=100,
            )
            bar.pack(pady=(0, 24))
            bar.start(15)

            def poll_status() -> None:
                try:
                    status_var.set(_DANCE_CURRENT_STATUS[0])
                    frame.after(250, poll_status)
                except tk.TclError:
                    pass

            frame.after(250, poll_status)
            return frame

        def on_download_done(video_path: Path, audio_path: Path) -> None:
            def build_player() -> tk.Frame:
                imageio_mod = importlib.import_module("imageio")
                pil_image   = importlib.import_module("PIL.Image")
                pil_imagetk = importlib.import_module("PIL.ImageTk")

                self._dance_display_active[0] = True

                frame = tk.Frame(self, bg=DARK_BG)
                add_header_with_question(frame)
                add_back_button(frame)

                body = tk.Frame(frame, bg="black")
                body.pack(fill="both", expand=True)
                display = tk.Label(body, bg="black")
                display.pack(fill="both", expand=True, padx=8, pady=8)
                if _ENABLE_RAINBOW:
                    start_rainbow_flash(frame, body)

                photo_ref:     list[object]       = [None]
                pending_frame: list[tuple | None] = [None]

                def on_display_configure(event: tk.Event) -> None:
                    if event.widget is display:
                        self._dance_display_size[0] = (max(event.width, 1), max(event.height, 1))

                display.bind("<Configure>", on_display_configure)

                def open_and_stream() -> None:
                    reader = None
                    try:
                        fps      = _DANCE_CACHED_FPS[0] if _DANCE_CACHED_FPS[0] > 0 else 0.0
                        duration = _DANCE_CACHED_DURATION[0]

                        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                            reader = imageio_mod.get_reader(str(video_path), "ffmpeg")
                        if fps == 0.0:
                            meta     = reader.get_meta_data()
                            fps_raw  = meta.get("fps") or meta.get("source_fps")
                            fps      = float(fps_raw) if fps_raw and float(fps_raw) > 0 else 30.0
                            duration = float(meta.get("duration") or 0)
                            _DANCE_CACHED_FPS[0]      = fps
                            _DANCE_CACHED_DURATION[0] = duration
                        self._dance_fps_ref[0] = fps

                        def audio_starter() -> None:
                            try:
                                if not _DANCE_WARMUP_WAV.exists():
                                    _generate_silent_wav(_DANCE_WARMUP_WAV)
                                winsound.PlaySound(
                                    str(_DANCE_WARMUP_WAV),
                                    winsound.SND_FILENAME | winsound.SND_SYNC,
                                )
                            except Exception:
                                pass
                            winsound.PlaySound(str(audio_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
                            actual_start = time.monotonic()
                            self._dance_audio_start = actual_start
                            self._dance_start_time[0] = actual_start

                        threading.Thread(target=audio_starter, daemon=True).start()

                        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                            for frame_index, img_array in enumerate(reader):
                                if not self._dance_stream_active[0]:
                                    break
                                disp_w, disp_h = self._dance_display_size[0]
                                img = pil_image.fromarray(img_array)
                                src_w, src_h = img.size
                                ratio = min(disp_w / src_w, disp_h / src_h)
                                img = img.resize(
                                    (max(1, int(src_w * ratio)), max(1, int(src_h * ratio))),
                                    pil_image.BILINEAR,
                                )
                                while self._dance_stream_active[0]:
                                    try:
                                        self._dance_frame_queue.put_nowait((img, frame_index))
                                        break
                                    except queue.Full:
                                        if not self._dance_display_active[0]:
                                            # Not displaying: drain one frame to keep reader
                                            # advancing at real-time pace rather than blocking.
                                            try:
                                                self._dance_frame_queue.get_nowait()
                                            except queue.Empty:
                                                pass
                                            time.sleep(1.0 / fps)
                                        else:
                                            time.sleep(0.005)
                    except Exception:
                        pass
                    finally:
                        self._dance_stream_active[0] = False
                        if reader is not None:
                            try:
                                reader.close()
                            except Exception:
                                pass

                if not self._dance_stream_active[0]:
                    # Fresh start: clear any stale queue state from a previous session.
                    self._dance_stream_active[0] = True
                    self._dance_start_time[0] = float("inf")
                    while not self._dance_frame_queue.empty():
                        try:
                            self._dance_frame_queue.get_nowait()
                        except queue.Empty:
                            break
                    threading.Thread(target=open_and_stream, daemon=True).start()
                # If stream is already active, show_frame below will pick up from the
                # live queue immediately — no FFmpeg restart needed.

                def show_frame() -> None:
                    if not self._dance_display_active[0]:
                        return
                    if self._dance_start_time[0] < float("inf"):
                        fps = self._dance_fps_ref[0]
                        now = time.monotonic()
                        while True:
                            if pending_frame[0] is None:
                                try:
                                    pending_frame[0] = self._dance_frame_queue.get_nowait()
                                except queue.Empty:
                                    break
                            img, frame_index = pending_frame[0]
                            target_time = self._dance_start_time[0] + (frame_index + 1) / fps
                            if now < target_time:
                                break  # frame is on time; wait for it
                            # Frame is past due. Peek at the next frame to decide whether
                            # to display this one or skip it as stale.
                            try:
                                next_frame = self._dance_frame_queue.get_nowait()
                                next_target = self._dance_start_time[0] + (next_frame[1] + 1) / fps
                                if now >= next_target:
                                    # Next frame is also past due: skip current, loop again.
                                    pending_frame[0] = next_frame
                                else:
                                    # Next frame is in the future: current is the right one.
                                    photo_ref[0] = pil_imagetk.PhotoImage(img)
                                    display.configure(image=photo_ref[0])
                                    pending_frame[0] = next_frame
                                    break
                            except queue.Empty:
                                # No next frame: display current (most recent available).
                                photo_ref[0] = pil_imagetk.PhotoImage(img)
                                display.configure(image=photo_ref[0])
                                pending_frame[0] = None
                                break
                    self.after(4, show_frame)

                def on_destroy(event: tk.Event) -> None:
                    if event.widget is frame:
                        self._dance_display_active[0] = False

                frame.bind("<Destroy>", on_destroy)
                self.after(4, show_frame)
                return frame

            self._swap_frame(build_player, no_border=True)

        def on_dance_error(error: str) -> None:
            def build_error() -> tk.Frame:
                frame = tk.Frame(self, bg=DARK_BG)
                add_header_with_question(frame)
                add_back_button(frame)
                body = tk.Frame(frame, bg=DARK_BG)
                body.pack(fill="both", expand=True)
                if _ENABLE_RAINBOW:
                    start_rainbow_flash(frame, body)
                centre_panel = tk.Frame(body, bg=PANEL_BG)
                centre_panel.pack(fill="both", expand=True, padx=8, pady=8)
                tk.Label(
                    centre_panel,
                    text=f"Something went wrong.\n\n{error[:600]}",
                    font=FONT_BODY, fg=TEXT_DIM, bg=PANEL_BG, justify="center",
                ).pack(expand=True, pady=20)
                return frame

            self._swap_frame(build_error, no_border=True)

        def proceed() -> None:
            if _DANCE_ASSETS_ERROR[0] is not None:
                on_dance_error(_DANCE_ASSETS_ERROR[0])
                return
            if hasattr(sys, "_MEIPASS"):
                bundled_video = _bundled_dance_path("dance_video.mp4")
                bundled_audio = _bundled_dance_path("dance_audio.wav")
                if bundled_video and bundled_audio:
                    on_download_done(bundled_video, bundled_audio)
                else:
                    on_dance_error("Dance assets were not bundled into this exe.")
            else:
                on_download_done(_DANCE_VIDEO_FILE, _DANCE_AUDIO_FILE)

        if _DANCE_ASSETS_READY.is_set():
            proceed()
        else:
            self._swap_frame(build_loading, no_border=True)

            def wait_for_assets() -> None:
                _DANCE_ASSETS_READY.wait()
                self.after(0, proceed)

            threading.Thread(target=wait_for_assets, daemon=True).start()


# -------------------------
# ENTRY POINT
# -------------------------

if __name__ == "__main__":
    UpdaterApp().mainloop()
