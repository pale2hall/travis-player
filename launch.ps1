# travis-player launcher
#
# Usage:
#   .\launch.ps1                              # use first video found in this dir
#   .\launch.ps1 "path\to\video.mp4"
#   .\launch.ps1 "https://example.com/stream.m3u8"
#   .\launch.ps1 -Console                     # keep console open to see python errors

param(
    [Parameter(Position=0)]
    [string]$Source = "",
    [switch]$Console
)

$ErrorActionPreference = "Stop"
$Root    = $PSScriptRoot
$Script  = Join-Path $Root "travis_player.py"
$LogDir  = Join-Path $Root "logs"
$LogFile = Join-Path $LogDir "travis.log"

if (-not (Test-Path $Script)) {
    Write-Error "travis_player.py not found at $Script"
}

if ($Console) {
    $PyExe = Join-Path $Root ".venv\Scripts\python.exe"
} else {
    $PyExe = Join-Path $Root ".venv\Scripts\pythonw.exe"
}

if (-not (Test-Path $PyExe)) {
    $alt = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $alt) {
        $PyExe = $alt
    } else {
        Write-Error "venv missing. Run: python -m venv .venv ; .\.venv\Scripts\pip install PyQt6 opencv-python numpy"
    }
}

if (-not $Source) {
    $exts = '.mp4','.mkv','.webm','.mov','.avi'
    $candidate = Get-ChildItem -Path $Root -File | Where-Object { $exts -contains $_.Extension } | Select-Object -First 1
    if ($candidate) {
        $Source = $candidate.FullName
        Write-Host "using found video: $Source"
    } else {
        Write-Host "no video specified and none found"
        Write-Host "usage: .\launch.ps1 <video-or-url> [-Console]"
        exit 1
    }
}

Write-Host "launching travis-player..."
Write-Host "  python : $PyExe"
Write-Host "  source : $Source"
Write-Host "  logs   : $LogFile"

$args = @("`"$Script`"", "`"$Source`"")
$proc = Start-Process -FilePath $PyExe -ArgumentList $args -WorkingDirectory $Root -PassThru
Write-Host "  pid    : $($proc.Id)"

Start-Sleep -Seconds 2

if ($proc.HasExited) {
    Write-Host ""
    Write-Host "ERROR: process exited immediately (code $($proc.ExitCode))" -ForegroundColor Red
    if (Test-Path $LogFile) {
        Write-Host "last log lines:"
        Get-Content $LogFile -Tail 20
    } else {
        Write-Host "no log file. try: .\launch.ps1 -Console"
    }
    exit 1
}

Write-Host "  alive after 2s. drag a file or url onto either window to swap source."
if (Test-Path $LogFile) {
    Write-Host ""
    Write-Host "recent log:"
    Get-Content $LogFile -Tail 8
}
