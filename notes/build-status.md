# Build status

## What was done in this repo
- Added Windows bootstrap script for cloning mpv fork: `scripts/bootstrap_mpv_fork.ps1`.
- Added Windows build script for baseline mpv build via MSYS2: `scripts/build_mpv_windows.ps1`.
- Added implementation planning and rubric docs under `notes/`.

## What is still required to complete "fork and build"
Because this environment is not connected to your personal GitHub account or a configured remote, final fork/push must be run from a machine with your GitHub credentials.

### GitHub fork/push commands to run locally
1. Create a new repo (e.g. `travis-player`) under your account.
2. In this repo:
   - `git remote add origin <your-github-repo-url>`
   - `git push -u origin work`

### mpv build commands on Windows 11
- Run `scripts/bootstrap_mpv_fork.ps1`.
- Run `scripts/build_mpv_windows.ps1`.
- Verify resulting mpv binary runs before adding transparency pipeline changes.
