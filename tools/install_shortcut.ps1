# Creates a "travis-player" shortcut on your Desktop that launches the app with NO video
# (opens empty - drag a file or stream URL onto the window). Re-runnable.
#
#   powershell -ExecutionPolicy Bypass -File tools\install_shortcut.ps1

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $repo ".venv\Scripts\pythonw.exe"   # windowed python = no console window
$icon = Join-Path $repo "assets\travis.ico"

if (-not (Test-Path $pythonw)) {
    Write-Error "pythonw.exe not found at $pythonw - create the .venv first (see README)."
}

$desktop = [Environment]::GetFolderPath("Desktop")
$lnkPath = Join-Path $desktop "travis-player.lnk"

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
$lnk.TargetPath = $pythonw
$lnk.Arguments = "-m v2"            # no source = opens idle, waiting for a drop
$lnk.WorkingDirectory = $repo
$lnk.WindowStyle = 1
$lnk.Description = "travis-player - motion-transparency video player (drag a video in)"
if (Test-Path $icon) { $lnk.IconLocation = $icon }
$lnk.Save()

Write-Host "Created shortcut: $lnkPath"
Write-Host "  target : $pythonw -m v2"
Write-Host "  workdir: $repo"
