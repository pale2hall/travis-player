"""audio sidecar — unchanged from v1.

a parallel audio-only mpv process driven over a Windows named pipe (pause/resume/volume/seek
without restart), wrapped in a Job Object so it dies with the parent. v2 phase 1 keeps this
as-is; the in-process unified-clock audio path is a documented later migration.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import shutil
import subprocess
import sys
from ctypes import wintypes

log = logging.getLogger("travis.v2")

_job_handle: int | None = None


def create_job_for_children() -> int | None:
    """Windows Job Object that auto-kills any assigned child when this process exits."""
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    class BASIC(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class EXT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC),
            ("IoInfo", IO),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = EXT()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        kernel32.CloseHandle(job)
        return None
    return job


def set_job_handle(handle: int | None) -> None:
    global _job_handle
    _job_handle = handle


def _add_to_job(pid: int) -> bool:
    if _job_handle is None or sys.platform != "win32":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
    if not handle:
        return False
    ok = bool(kernel32.AssignProcessToJobObject(_job_handle, handle))
    kernel32.CloseHandle(handle)
    return ok


class AudioController:
    def __init__(self, source: str, pipe_name: str | None = None) -> None:
        self.source = source
        self.pipe_name = pipe_name or rf"\\.\pipe\travis-audio-{os.getpid()}"
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        mpv = shutil.which("mpv")
        if not mpv:
            log.warning("mpv not in PATH — running silent")
            return
        args = [
            mpv, "--no-video", "--no-terminal", "--really-quiet",
            "--keep-open=no", "--loop-file=inf",
            f"--input-ipc-server={self.pipe_name}", self.source,
        ]
        self._proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )
        in_job = _add_to_job(self._proc.pid)
        log.info("audio sidecar pid=%d job=%s", self._proc.pid, "yes" if in_job else "NO")

    def _send(self, cmd: dict) -> None:
        if self._proc is None or self._proc.poll() is not None or sys.platform != "win32":
            return
        data = (json.dumps(cmd) + "\n").encode("utf-8")
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.CreateFileW(self.pipe_name, 0x40000000, 0, None, 3, 0, None)
            if handle in (-1, 0):
                return
            try:
                written = wintypes.DWORD(0)
                kernel32.WriteFile(handle, data, len(data), ctypes.byref(written), None)
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            log.exception("audio IPC failed")

    def set_paused(self, paused: bool) -> None:
        self._send({"command": ["set_property", "pause", paused]})

    def set_volume(self, vol: int) -> None:
        self._send({"command": ["set_property", "volume", max(0, min(100, vol))]})

    def seek(self, pos_sec: float) -> None:
        self._send({"command": ["seek", pos_sec, "absolute"]})

    def stop(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
