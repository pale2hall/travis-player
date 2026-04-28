# Bootstrap script for local Windows setup of the mpv-fork prototype.
# Run from a Developer PowerShell prompt.

param(
  [string]$Workspace = "$HOME\dev\travis-player-mpv",
  [string]$MpvRepo = "https://github.com/mpv-player/mpv.git"
)

$ErrorActionPreference = "Stop"

Write-Host "Creating workspace: $Workspace"
New-Item -ItemType Directory -Path $Workspace -Force | Out-Null
Set-Location $Workspace

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  throw "git not found. Install Git for Windows first."
}

if (-not (Test-Path "$Workspace\mpv")) {
  Write-Host "Cloning mpv..."
  git clone $MpvRepo mpv
} else {
  Write-Host "mpv directory already exists; skipping clone."
}

Write-Host "Done. Next manual steps:"
Write-Host "1) Install MSYS2 + required build deps for mpv."
Write-Host "2) Build upstream mpv baseline and verify playback."
Write-Host "3) Create feature branch for motion-transparency pipeline."
