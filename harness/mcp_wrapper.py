#!/usr/bin/env python3
"""MCP Process Wrapper — ensures child processes die when parent (Claude Code) exits.

Uses Windows Job Objects with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
When this wrapper exits for ANY reason (normal exit, crash, killed),
the OS kernel kills all child processes in the job.

Usage: python mcp_wrapper.py [--label NAME] [--pid-dir DIR] -- <command> [args...]
"""

import atexit
import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path


def parse_wrapper_args(argv):
    """Parse wrapper-specific args before '--'. Returns (wrapper_opts, child_cmd)."""
    label = None
    pid_dir = None
    idx = 0
    while idx < len(argv):
        if argv[idx] == "--":
            idx += 1
            break
        if argv[idx] == "--label" and idx + 1 < len(argv):
            label = argv[idx + 1]
            idx += 2
        elif argv[idx] == "--pid-dir" and idx + 1 < len(argv):
            pid_dir = argv[idx + 1]
            idx += 2
        else:
            idx += 1
    return {"label": label, "pid_dir": pid_dir}, argv[idx:]


def create_kill_on_close_job():
    """Create a Windows Job Object configured to kill children on handle close."""
    if sys.platform != "win32":
        return None

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
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

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", ctypes.c_byte * 48),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())

    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

    size = ctypes.sizeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION)
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), size):
        raise ctypes.WinError(ctypes.get_last_error())

    return job


def assign_process_to_job(job, pid):
    """Assign a process to the job object by PID."""
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001
    handle = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        result = kernel32.AssignProcessToJobObject(job, handle)
        return bool(result)
    finally:
        kernel32.CloseHandle(handle)


def proxy_stream(src, dst):
    """Copy data from src to dst until EOF, then close dst."""
    try:
        while True:
            data = src.read(4096)
            if not data:
                break
            dst.write(data)
            dst.flush()
    except (IOError, OSError, ValueError):
        pass
    finally:
        try:
            dst.close()
        except Exception:
            pass


def write_pid_file(pid_dir, label, wrapper_pid, child_pid):
    """Write a PID marker file so harness can track this wrapper."""
    if not pid_dir or not label:
        return None
    try:
        pid_path = Path(pid_dir) / f"{label}.json"
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "label": label,
            "wrapper_pid": wrapper_pid,
            "child_pid": child_pid,
            "start_time": datetime.now().isoformat(),
        }
        pid_path.write_text(json.dumps(data, indent=2))
        return pid_path
    except Exception:
        return None


def remove_pid_file(pid_path):
    """Remove the PID marker file."""
    if pid_path:
        try:
            pid_path.unlink(missing_ok=True)
        except Exception:
            pass


def main():
    opts, child_cmd = parse_wrapper_args(sys.argv[1:])

    if not child_cmd:
        print("Usage: python mcp_wrapper.py [--label NAME] [--pid-dir DIR] -- <command> [args...]",
              file=sys.stderr)
        sys.exit(1)

    wrapper_pid = os.getpid()
    pid_path = None

    # --- Windows Job Object (kill child when wrapper exits) ---
    job = create_kill_on_close_job()

    # --- Start actual MCP server ---
    try:
        proc = subprocess.Popen(
            child_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except Exception as e:
        print(f"[mcp_wrapper] Failed to start {child_cmd[0]}: {e}", file=sys.stderr)
        sys.exit(1)

    if job:
        assign_process_to_job(job, proc.pid)

    # --- Write PID marker for harness visibility ---
    pid_path = write_pid_file(opts["pid_dir"], opts["label"], wrapper_pid, proc.pid)
    atexit.register(remove_pid_file, pid_path)

    # --- Proxy stdin/stdout/stderr ---
    threads = [
        threading.Thread(target=proxy_stream, args=(sys.stdin.buffer, proc.stdin), daemon=True),
        threading.Thread(target=proxy_stream, args=(proc.stdout, sys.stdout.buffer), daemon=True),
        threading.Thread(target=proxy_stream, args=(proc.stderr, sys.stderr.buffer), daemon=True),
    ]
    for t in threads:
        t.start()

    # --- Wait for child to exit ---
    proc.wait()

    for t in threads:
        t.join(timeout=2)

    # Clean up PID file before exit (atexit also does this as backup)
    remove_pid_file(pid_path)

    # Use os._exit to avoid daemon-thread cleanup noise during Python shutdown
    os._exit(proc.returncode)


if __name__ == "__main__":
    main()
