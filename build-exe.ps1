# build-exe.ps1 — Build client-updater.exe for Windows distribution
#
# Run this after baking the updater:
#   python modpackctl.py bake-updater
#   .\build-exe.ps1
#
# The finished exe is written to releases\client-updater.exe

param(
    [string]$Source = "releases\client-updater.py",
    [string]$Name   = "client-updater"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host "Installing build dependencies..."
pip install --quiet pyinstaller yt-dlp moviepy Pillow imageio-ffmpeg

Write-Host "Building $Name.exe..."
pyinstaller `
    --onefile `
    --windowed `
    --name $Name `
    --collect-all yt_dlp `
    --collect-all moviepy `
    --collect-all imageio `
    --collect-all imageio_ffmpeg `
    --collect-all PIL `
    --distpath releases `
    --workpath .pyinstaller\work `
    --specpath .pyinstaller `
    $Source

Write-Host ""
Write-Host "Done: releases\$Name.exe"
