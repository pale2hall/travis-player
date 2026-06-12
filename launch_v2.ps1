# travis-player v2 launcher
#   .\launch_v2.ps1                          # opens empty — drag a video/URL in
#   .\launch_v2.ps1 "C:\path\to\video.mkv"   # local file
#   .\launch_v2.ps1 "https://host/live.m3u8" # stream URL (HLS/HTTP/RTSP/...)
param([string]$Source = "")

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if ($Source) { & $py -m v2 $Source } else { & $py -m v2 }
