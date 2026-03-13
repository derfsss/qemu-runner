#!/usr/bin/env python3
"""
Automated dev cycle for AmigaOS 4 — works with QEMU or real hardware.

Wires together: QEMU lifecycle, Docker build, TCP upload, guest execute, output capture.

QEMU mode (default):
    python dev_cycle.py start                  # Start QEMU with auto-restart
    python dev_cycle.py stop                   # Stop QEMU and auto-restart
    python dev_cycle.py start --wait           # Start and wait for SerialShell
    python dev_cycle.py status                 # Check QEMU + guest status
    python dev_cycle.py reset                  # Reboot guest via QMP
    python dev_cycle.py build-run --project-dir projects/tools/qemu-runner/amiga --binary hello --test

Remote mode (real hardware):
    python dev_cycle.py --remote --host 192.168.1.50 status
    python dev_cycle.py --remote --host 192.168.1.50 reset
    python dev_cycle.py --remote --host 192.168.1.50 build-run --project-dir ... --binary ... --test
"""

import argparse
import io
import os
import subprocess
import sys
import time

# Ensure stdout handles non-ASCII from guest output
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from serial_client import SerialClient
from qmp_client import QMPClient

REPO_ROOT = r"W:\Code\amiga\antigravity"

DOCKER_CMD = (
    'wsl sh -c "docker run --rm '
    '-v /mnt/w/Code/amiga/antigravity:/src '
    '-w /src/{project_dir} '
    'walkero/amigagccondocker:os4-gcc11 '
    '{make_cmd}"'
)

DEFAULT_HOST = "localhost"
DEFAULT_SERIAL_PORT = 4321
DEFAULT_QMP_PORT = 4322
DEFAULT_CONFIG = r"E:\Emulators\QEMU\QEMU_Machines\base_a1\config_dev.json"
DEFAULT_QEMU_PATH = r"E:\Emulators\QEMU\QEMU_Install\qemu-system-ppc.exe"

# PID file for tracking the qemu_manager background process
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MANAGER_PIDFILE = os.path.join(SCRIPT_DIR, ".qemu_manager.pid")


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is running (Windows-compatible)."""
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        return str(pid) in r.stdout
    except Exception:
        return False


def _kill_process(pid: int):
    """Kill a process by PID (Windows-compatible)."""
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def _read_pidfile() -> int | None:
    """Read the manager PID from the pidfile. Returns None if not found."""
    if not os.path.isfile(MANAGER_PIDFILE):
        return None
    try:
        with open(MANAGER_PIDFILE, "r") as f:
            pid = int(f.read().strip())
        return pid
    except (ValueError, OSError):
        return None


def _write_pidfile(pid: int):
    with open(MANAGER_PIDFILE, "w") as f:
        f.write(str(pid))


def _remove_pidfile():
    try:
        os.remove(MANAGER_PIDFILE)
    except OSError:
        pass


def is_qemu_running(host: str, qmp_port: int) -> bool:
    """Check if QEMU is reachable via QMP."""
    try:
        qmp = QMPClient(host, qmp_port)
        qmp.connect(timeout=2)
        qmp.close()
        return True
    except Exception:
        return False


def start_qemu(config: str, qemu_path: str, host: str, serial_port: int,
               qmp_port: int, wait: bool = False,
               wait_timeout: float = 120.0) -> bool:
    """Start QEMU with auto-restart manager in the background."""
    # Check if already running
    existing_pid = _read_pidfile()
    if existing_pid:
        if is_qemu_running(host, qmp_port):
            print(f"QEMU manager already running (PID {existing_pid}), QEMU is up.")
            if wait:
                return _wait_for_guest(host, serial_port, wait_timeout)
            return True
        else:
            print(f"Stale manager (PID {existing_pid}), cleaning up...")
            _kill_process(existing_pid)
            _remove_pidfile()

    if not os.path.isfile(config):
        print(f"Config not found: {config}")
        return False

    print(f"\n=== START QEMU ===")
    print(f"Config: {config}")

    # Launch qemu_manager.py as a detached background process
    manager_script = os.path.join(SCRIPT_DIR, "qemu_manager.py")
    log_file = os.path.join(SCRIPT_DIR, "qemu_manager.log")

    with open(log_file, "a") as lf:
        proc = subprocess.Popen(
            [sys.executable, manager_script, config, "--qemu-path", qemu_path],
            stdout=lf,
            stderr=lf,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                          | subprocess.DETACHED_PROCESS,
        )

    _write_pidfile(proc.pid)
    print(f"QEMU manager started (PID {proc.pid}), log: {log_file}")

    if wait:
        return _wait_for_guest(host, serial_port, wait_timeout)
    else:
        # Brief pause to check it didn't fail immediately
        time.sleep(2)
        if _is_process_alive(proc.pid):
            print("QEMU manager is running.")
            return True
        else:
            print("QEMU manager exited immediately — check log.")
            _remove_pidfile()
            return False


def _wait_for_guest(host: str, serial_port: int,
                    wait_timeout: float) -> bool:
    """Wait for SerialShell to become reachable."""
    print(f"Waiting for SerialShell (timeout {wait_timeout}s)...")
    client = SerialClient(host, serial_port)
    try:
        if client.wait_for_ready(wait_timeout):
            print("Guest is ready.")
            return True
        else:
            print("Timeout waiting for guest.")
            return False
    finally:
        client.close()


def stop_qemu(host: str, qmp_port: int) -> bool:
    """Kill the manager process (stops auto-restart), then quit QEMU."""
    print("\n=== STOP QEMU ===")

    # Step 1: Kill the manager FIRST so it can't auto-restart QEMU
    manager_pid = _read_pidfile()
    if manager_pid:
        print(f"Stopping manager (PID {manager_pid})...")
        _kill_process(manager_pid)
        for _ in range(10):
            if not _is_process_alive(manager_pid):
                break
            time.sleep(0.5)
        _remove_pidfile()
        print("Manager stopped.")
    else:
        print("No manager PID found.")

    # Step 2: Quit QEMU via QMP
    try:
        qmp = QMPClient(host, qmp_port)
        qmp.connect(timeout=3)
        qmp.quit()
        qmp.close()
        print("QMP quit sent — QEMU shutting down.")
    except Exception as e:
        print(f"QMP quit failed ({e}), QEMU may already be stopped.")

    return True


def build(project_dir: str, make_clean: bool = True) -> bool:
    """Build via Docker cross-compiler."""
    print(f"\n=== BUILD: {project_dir} ===")

    if make_clean:
        cmd = DOCKER_CMD.format(project_dir=project_dir, make_cmd="make clean")
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  Clean warning: {r.stderr.strip()}")

    cmd = DOCKER_CMD.format(project_dir=project_dir, make_cmd="make -j$(nproc) all")
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.stdout.strip():
        print(r.stdout.strip())
    if r.returncode != 0:
        print(f"BUILD FAILED:\n{r.stderr}")
        return False

    print("Build OK.")
    return True


def upload(local_path: str, guest_path: str, host: str, port: int,
           timeout: float = 120.0) -> bool:
    """Upload binary to guest via TCP."""
    size = os.path.getsize(local_path)
    print(f"\n=== UPLOAD: {local_path} -> {guest_path} ({size} bytes) ===")

    client = SerialClient(host, port)
    try:
        client.connect(timeout=5)
        client.upload_file(local_path, guest_path, timeout=timeout)
        print("Upload OK.")
        return True
    except Exception as e:
        print(f"Upload FAILED: {e}")
        return False
    finally:
        client.close()


def run_command(guest_cmd: str, host: str, port: int,
                timeout: float = 60.0) -> str:
    """Run a command on the guest and return output."""
    print(f"\n=== RUN: {guest_cmd} ===")

    client = SerialClient(host, port)
    try:
        client.connect(timeout=5)
        output = client.send_command(guest_cmd, timeout=timeout)
        return output
    finally:
        client.close()


def guest_path_for(binary: str, guest_dest: str) -> str:
    """Build guest path from binary name and destination."""
    filename = os.path.basename(binary)
    if not guest_dest:
        return f"T:{filename}"
    if guest_dest.endswith(":") or guest_dest.endswith("/"):
        return guest_dest + filename
    return guest_dest


def check_guest(host: str, serial_port: int, qmp_port: int,
                remote: bool = False) -> bool:
    """Check if guest is reachable."""
    client = SerialClient(host, serial_port)
    try:
        client.connect(timeout=5)
        print(f"Guest ({host}:{serial_port}): ALIVE")
        client.close()
        return True
    except Exception:
        pass
    finally:
        client.close()

    if remote:
        print(f"Guest ({host}:{serial_port}): UNREACHABLE")
        return False

    print("Guest: SerialShell unreachable, checking QEMU via QMP...")
    try:
        qmp = QMPClient(host, qmp_port)
        qmp.connect()
        status = qmp.status()
        print(f"QEMU status: {status.get('return', {}).get('status', 'unknown')}")
        qmp.close()
        return False
    except Exception as e:
        print(f"QEMU: unreachable ({e})")
        return False


def reset_guest(host: str, qmp_port: int, serial_port: int,
                wait_timeout: float = 120.0,
                remote: bool = False) -> bool:
    """Reset guest and wait for SerialShell.

    QEMU mode: reset via QMP.
    Remote mode: send 'reboot' command via SerialShell.
    """
    print("\n=== RESET ===")

    if remote:
        # Send reboot via SerialShell
        client = SerialClient(host, serial_port)
        try:
            client.connect(timeout=5)
            # Send reboot — don't wait for output, connection will drop
            try:
                client.send_raw("reboot\n")
            except Exception:
                pass
            print("Reboot command sent.")
        except Exception as e:
            print(f"Could not send reboot: {e}")
            return False
        finally:
            client.close()
    else:
        # QMP reset
        qmp = QMPClient(host, qmp_port)
        try:
            qmp.connect()
            qmp.reset()
            print("QMP reset sent.")
        except Exception as e:
            print(f"QMP reset failed: {e}")
            return False
        finally:
            qmp.close()

    # Wait for SerialShell to come back
    print(f"Waiting for SerialShell (timeout {wait_timeout}s)...")
    client = SerialClient(host, serial_port)
    try:
        if client.wait_for_ready(wait_timeout):
            print("Guest is back.")
            return True
        else:
            print("Timeout waiting for guest.")
            return False
    finally:
        client.close()


def build_and_run(project_dir: str, binary: str, guest_dest: str,
                  args_str: str, host: str, serial_port: int,
                  qmp_port: int, timeout: float, make_clean: bool,
                  parse_tests: bool, remote: bool = False) -> bool:
    """Full cycle: build, upload, run, capture output."""

    # Step 1: Build
    if not build(project_dir, make_clean):
        return False

    # Step 2: Locate binary
    src_path = os.path.join(REPO_ROOT, project_dir, binary)
    if not os.path.isfile(src_path):
        print(f"Binary not found: {src_path}")
        return False

    # Step 3: Upload
    guest_path = guest_path_for(binary, guest_dest)
    if not upload(src_path, guest_path, host, serial_port):
        # Try reset and retry
        print("Attempting reset and retry...")
        if not reset_guest(host, qmp_port, serial_port, remote=remote):
            return False
        if not upload(src_path, guest_path, host, serial_port):
            return False

    # Step 4: Run
    cmd = guest_path
    if args_str:
        cmd += " " + args_str
    output = run_command(cmd, host, serial_port, timeout)

    # Step 5: Display output
    print(f"\n--- Output ---\n{output}\n--- End ---")

    # Step 6: Parse test results if requested
    if parse_tests:
        return parse_and_report(output)

    return True


def parse_and_report(output: str) -> bool:
    """Parse PASS/FAIL lines and report."""
    import re
    passed = 0
    failed = 0
    failures = []

    for line in output.splitlines():
        m = re.match(r"^.*?\.\.\.\s*(PASS|FAIL)\s*$", line, re.IGNORECASE)
        if m:
            if m.group(1).upper() == "PASS":
                passed += 1
            else:
                failed += 1
                failures.append(line.strip())
            continue

        m = re.match(r"^\s*(PASS|FAIL):\s*(.*)$", line, re.IGNORECASE)
        if m:
            if m.group(1).upper() == "PASS":
                passed += 1
            else:
                failed += 1
                failures.append(line.strip())

    total = passed + failed
    if total == 0:
        print("\nNo test results found in output.")
        return False

    if failures:
        print(f"\n--- FAILURES ({failed}) ---")
        for f in failures:
            print(f"  {f}")

    print(f"\n--- Results: {passed}/{total} passed ---")
    return failed == 0


def main():
    parser = argparse.ArgumentParser(description="AmigaOS 4 automated dev cycle")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--serial-port", type=int, default=DEFAULT_SERIAL_PORT)
    parser.add_argument("--qmp-port", type=int, default=DEFAULT_QMP_PORT)
    parser.add_argument("--remote", action="store_true",
                         help="Remote mode for real hardware (no QMP, no QEMU manager)")

    sub = parser.add_subparsers(dest="action")

    # start: launch QEMU with auto-restart
    p_start = sub.add_parser("start", help="Start QEMU with auto-restart")
    p_start.add_argument("--config", default=DEFAULT_CONFIG,
                          help=f"QEMU config.json (default: {DEFAULT_CONFIG})")
    p_start.add_argument("--qemu-path", default=DEFAULT_QEMU_PATH)
    p_start.add_argument("--wait", action="store_true",
                          help="Wait for SerialShell to be ready")
    p_start.add_argument("--wait-timeout", type=float, default=120)

    # stop: shut down QEMU and auto-restart manager
    sub.add_parser("stop", help="Stop QEMU and auto-restart manager")

    # status: check QEMU + guest
    sub.add_parser("status", help="Check QEMU and guest status")

    # reset: reboot guest
    p_reset = sub.add_parser("reset", help="Reset guest via QMP")
    p_reset.add_argument("--wait-timeout", type=float, default=120)

    # build-run: full dev cycle
    p_br = sub.add_parser("build-run", help="Build, upload, and run")
    p_br.add_argument("--project-dir", required=True)
    p_br.add_argument("--binary", required=True)
    p_br.add_argument("--guest-dest", default="T:",
                       help="Guest destination (default: T:)")
    p_br.add_argument("--args", default="")
    p_br.add_argument("--timeout", type=float, default=60)
    p_br.add_argument("--no-clean", action="store_true")
    p_br.add_argument("--test", action="store_true",
                       help="Parse output for PASS/FAIL results")

    args = parser.parse_args()
    if not args.action:
        parser.print_help()
        sys.exit(1)

    if args.action == "start":
        if args.remote:
            print("'start' is not available in remote mode (real hardware).")
            print("Ensure SerialShell is running on the target, then use 'status'.")
            sys.exit(1)
        ok = start_qemu(args.config, args.qemu_path,
                         args.host, args.serial_port, args.qmp_port,
                         wait=args.wait, wait_timeout=args.wait_timeout)
        sys.exit(0 if ok else 1)

    elif args.action == "stop":
        if args.remote:
            print("'stop' is not available in remote mode (real hardware).")
            print("Use 'reset' to reboot, or shut down manually.")
            sys.exit(1)
        ok = stop_qemu(args.host, args.qmp_port)
        sys.exit(0 if ok else 1)

    elif args.action == "status":
        check_guest(args.host, args.serial_port, args.qmp_port,
                     remote=args.remote)

    elif args.action == "reset":
        ok = reset_guest(args.host, args.qmp_port, args.serial_port,
                         args.wait_timeout, remote=args.remote)
        sys.exit(0 if ok else 1)

    elif args.action == "build-run":
        ok = build_and_run(
            project_dir=args.project_dir,
            binary=args.binary,
            guest_dest=args.guest_dest,
            args_str=args.args,
            host=args.host,
            serial_port=args.serial_port,
            qmp_port=args.qmp_port,
            timeout=args.timeout,
            make_clean=not args.no_clean,
            parse_tests=args.test,
            remote=args.remote,
        )
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
