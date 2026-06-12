# travis-player v2 launcher
#   .\launch_v2.ps1                         # plays the bundled clip
#   .\launch_v2.ps1 "C:\path\to\video.mkv"  # local file
#   .\launch_v2.ps1 "https://host/live.m3u8" # stream URL (HLS/HTTP/RTSP/...)
param([string]$Source = "The King of Queens - S04E01 - Walk, Man.mp4")

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
& "$PSScriptRoot\.venv\Scripts\python.exe" -m v2 $Source
