# Build mpv on Windows 11 via MSYS2 (prototype baseline).
# Prereqs:
#   1) Git for Windows
#   2) MSYS2 installed at C:\msys64
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\scripts\build_mpv_windows.ps1

param(
  [string]$Workspace = "$HOME\dev\travis-player-mpv",
  [string]$MpvRepo = "https://github.com/mpv-player/mpv.git",
  [string]$MsysRoot = "C:\msys64",
  [string]$MsysShell = "ucrt64"
)

$ErrorActionPreference = "Stop"

function Require-Command($name) {
  if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
    throw "Required command '$name' not found."
  }
}

Require-Command git

$bash = Join-Path $MsysRoot "usr\bin\bash.exe"
if (-not (Test-Path $bash)) {
  throw "MSYS2 bash not found at $bash. Install MSYS2 first."
}

New-Item -ItemType Directory -Path $Workspace -Force | Out-Null
Set-Location $Workspace

if (-not (Test-Path "$Workspace\mpv")) {
  git clone $MpvRepo mpv
}

# Install core toolchain + common deps for mpv baseline build
$pkgCmd = @"
set -euo pipefail
pacman -Sy --noconfirm
pacman -S --needed --noconfirm mingw-w64-ucrt-x86_64-toolchain \
  mingw-w64-ucrt-x86_64-meson \
  mingw-w64-ucrt-x86_64-ninja \
  mingw-w64-ucrt-x86_64-python \
  mingw-w64-ucrt-x86_64-ffmpeg \
  mingw-w64-ucrt-x86_64-libplacebo \
  mingw-w64-ucrt-x86_64-luajit \
  mingw-w64-ucrt-x86_64-libass \
  mingw-w64-ucrt-x86_64-freetype \
  mingw-w64-ucrt-x86_64-fribidi \
  mingw-w64-ucrt-x86_64-harfbuzz \
  mingw-w64-ucrt-x86_64-shaderc
"@

& $bash -lc $pkgCmd

$buildCmd = @"
set -euo pipefail
cd /$(($Workspace -replace ':','') -replace '\\','/')/mpv
meson setup build --buildtype=release || true
meson compile -C build
"@

& $bash -lc $buildCmd

Write-Host "Build attempt completed. Check $Workspace\mpv\build for artifacts/logs."
