#!/usr/bin/env python3
"""
Test runner for AmigaOS 4 — the script Claude calls to build, deploy, and test.

Workflow:
  1. (Optional) Build the project via Docker
  2. Copy binary to shared FAT drive (S:\\temp)
  3. Send command to AmigaOS guest via SerialShell TCP
  4. Capture and return output

Usage:
    python test_runner.py status
    python test_runner.py cmd "version"
    python test_runner.py run USB:myapp
    python test_runner.py deploy path/to/binary
    python test_runner.py build-and-run --project-dir projects/Foo --binary build/foo
    python test_runner.py test --project-dir projects/Foo --binary build/test_foo

Test output convention:
    Tests print lines matching one of these patterns:
      "Test N: name ... PASS"  /  "Test N: name ... FAIL"
      "  PASS: description"    /  "  FAIL: description"
    Summary line: "Results: N/M passed" or "Results: N tests, M passed, F failed"
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time

# Add parent dir so we can import serial_client
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from serial_client import SerialClient

DEFAULT_SHARED_DIR = r"S:\temp"
DEFAULT_SERIAL_HOST = "localhost"
DEFAULT_SERIAL_PORT = 4321
REPO_ROOT = r"W:\Code\amiga\antigravity"

# Docker build command template
DOCKER_CMD = (
    'wsl sh -c "docker run --rm '
    '-v /mnt/w/Code/amiga/antigravity:/src '
    '-w /src/{project_dir} '
    'walkero/amigagccondocker:os4-gcc11 '
    '{make_cmd}"'
)

# Patterns for parsing test output
# Matches: "Test 1: name ... PASS", "Test 2: name ... FAIL"
RE_TEST_RESULT = re.compile(
    r"^.*?(?:Test\s+\d+:\s+)?(.*?)\s*\.\.\.\s*(PASS|FAIL)\s*$", re.IGNORECASE
)
# Matches: "  PASS: description", "  FAIL: description"
RE_PASSFAIL_PREFIX = re.compile(
    r"^\s*(PASS|FAIL):\s*(.*)$", re.IGNORECASE
)
# Matches summary: "Results: 93/93 passed" or "Results: 93 tests, 93 passed, 0 failed"
RE_SUMMARY = re.compile(
    r"Results:\s*(\d+)[/\s]", re.IGNORECASE
)


class TestResult:
    def __init__(self, name: str, passed: bool):
        self.name = name
        self.passed = passed

    def __repr__(self):
        status = "PASS" if self.passed else "FAIL"
        return f"{status}: {self.name}"


def parse_test_output(output: str) -> tuple[list[TestResult], str]:
    """Parse test output for PASS/FAIL lines.

    Returns (list of TestResult, raw output).
    """
    results = []
    for line in output.splitlines():
        # Try "... PASS" / "... FAIL" format
        m = RE_TEST_RESULT.match(line)
        if m:
            name = m.group(1).strip() or line.strip()
            passed = m.group(2).upper() == "PASS"
            results.append(TestResult(name, passed))
            continue

        # Try "PASS: desc" / "FAIL: desc" format
        m = RE_PASSFAIL_PREFIX.match(line)
        if m:
            passed = m.group(1).upper() == "PASS"
            name = m.group(2).strip()
            results.append(TestResult(name, passed))
            continue

    return results, output


def print_test_summary(results: list[TestResult], raw_output: str):
    """Print structured test results."""
    if not results:
        print("\n--- Guest Output (no test results parsed) ---")
        print(raw_output)
        print("--- End ---")
        return

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    total = len(results)

    # Print failures first (most useful info)
    if failed:
        print(f"\n--- FAILURES ({failed}) ---")
        for r in results:
            if not r.passed:
                print(f"  FAIL: {r.name}")

    print(f"\n--- Results: {passed}/{total} passed", end="")
    if failed:
        print(f", {failed} failed ---")
    else:
        print(" ---")

    # Print full output for context
    print(f"\n--- Full Output ---\n{raw_output}\n--- End ---")


def docker_build(project_dir: str, make_clean: bool = True) -> bool:
    """Build a project using Docker cross-compiler."""
    print(f"Building {project_dir}...")

    if make_clean:
        cmd = DOCKER_CMD.format(project_dir=project_dir, make_cmd="make clean")
        print(f"  Clean: {cmd}")
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  Clean failed: {result.stderr}")
            # Non-fatal, continue

    cmd = DOCKER_CMD.format(project_dir=project_dir, make_cmd="make -j$(nproc) all")
    print(f"  Build: {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print(f"Build FAILED:\n{result.stderr}")
        return False

    print("Build OK.")
    return True


def deploy_binary(src_path: str, shared_dir: str = DEFAULT_SHARED_DIR) -> str:
    """Copy a binary to the shared FAT drive."""
    filename = os.path.basename(src_path)
    dst_path = os.path.join(shared_dir, filename)
    print(f"Deploying {src_path} -> {dst_path}")
    shutil.copy2(src_path, dst_path)
    return f"USB:{filename}"


def deploy_binary_tcp(src_path: str, guest_path: str,
                      host: str = DEFAULT_SERIAL_HOST,
                      port: int = DEFAULT_SERIAL_PORT) -> str:
    """Upload a binary to the guest over TCP (no USB drive needed)."""
    size = os.path.getsize(src_path)
    print(f"Uploading {src_path} -> {guest_path} ({size} bytes via TCP)")
    client = SerialClient(host, port)
    try:
        client.connect(timeout=5)
        client.upload_file(src_path, guest_path)
        print(f"Upload OK: {guest_path}")
        return guest_path
    finally:
        client.close()


def run_on_guest(guest_cmd: str, host: str = DEFAULT_SERIAL_HOST,
                 port: int = DEFAULT_SERIAL_PORT, timeout: float = 60) -> str:
    """Send a command to the AmigaOS guest and return output."""
    client = SerialClient(host, port)
    try:
        client.connect(timeout=5)
        output = client.send_command(guest_cmd, timeout=timeout)
        return output
    finally:
        client.close()


def check_status(host: str, port: int) -> bool:
    """Check if the guest SerialShell is reachable."""
    client = SerialClient(host, port)
    try:
        client.connect(timeout=5)
        # If connect() succeeds, the server sent READY — it's alive
        print("Guest status: ALIVE")
        return True
    except Exception as e:
        print(f"Guest status: UNREACHABLE ({e})")
        return False
    finally:
        client.close()


def make_guest_path(guest_dest: str, filename: str) -> str:
    """Build a full guest path from a destination and filename.

    If guest_dest looks like a directory (ends with : or /), append the filename.
    If guest_dest is empty, default to T:<filename>.
    Otherwise use guest_dest as-is (full path).
    """
    if not guest_dest:
        return f"T:{filename}"
    if guest_dest.endswith(":") or guest_dest.endswith("/"):
        return guest_dest + filename
    return guest_dest


def do_deploy(src_path: str, guest_cmd: str, shared_dir: str,
              tcp_deploy: bool, host: str, port: int) -> str:
    """Deploy a binary — either via USB FAT or TCP upload. Returns guest path."""
    filename = os.path.basename(src_path)
    if tcp_deploy:
        guest_path = make_guest_path(guest_cmd, filename)
        return deploy_binary_tcp(src_path, guest_path, host, port)
    else:
        return deploy_binary(src_path, shared_dir)


def do_test(project_dir: str, binary: str, guest_cmd: str, args_str: str,
            host: str, port: int, shared_dir: str, timeout: float,
            make_clean: bool, tcp_deploy: bool = False) -> bool:
    """Full test cycle: build, deploy, run, parse results. Returns True if all pass."""
    # Step 1: Build
    if not docker_build(project_dir, make_clean=make_clean):
        return False

    # Step 2: Deploy
    src_path = os.path.join(REPO_ROOT, project_dir, binary)
    if not os.path.isfile(src_path):
        print(f"Binary not found after build: {src_path}")
        return False
    guest_path = do_deploy(src_path, guest_cmd, shared_dir,
                           tcp_deploy, host, port)

    # Step 3: Run on guest
    cmd = guest_cmd or guest_path
    if args_str:
        cmd += " " + args_str

    print(f"\nRunning on guest: {cmd}")
    output = run_on_guest(cmd, host, port, timeout)

    # Step 4: Parse and report
    results, raw = parse_test_output(output)
    print_test_summary(results, raw)

    if not results:
        # No parseable results — treat as failure
        print("\nWARNING: No PASS/FAIL lines detected in output.")
        return False

    failed = sum(1 for r in results if not r.passed)
    return failed == 0


def main():
    parser = argparse.ArgumentParser(description="AmigaOS 4 test runner")
    parser.add_argument("--host", default=DEFAULT_SERIAL_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_SERIAL_PORT)
    parser.add_argument("--shared-dir", default=DEFAULT_SHARED_DIR)

    sub = parser.add_subparsers(dest="action")

    # run: run a binary already on the guest
    p_run = sub.add_parser("run", help="Run a binary on the guest")
    p_run.add_argument("binary", help="Guest path (e.g. USB:myapp)")
    p_run.add_argument("--args", default="")
    p_run.add_argument("--timeout", type=float, default=60)

    # cmd: send a raw shell command
    p_cmd = sub.add_parser("cmd", help="Send a shell command")
    p_cmd.add_argument("command")
    p_cmd.add_argument("--timeout", type=float, default=30)

    # build-and-run: full cycle (no test parsing)
    p_bar = sub.add_parser("build-and-run", help="Build, deploy, and run")
    p_bar.add_argument("--project-dir", required=True,
                       help="Relative path from repo root (e.g. projects/AmigaNVMeDevice)")
    p_bar.add_argument("--binary", required=True,
                       help="Relative path to built binary within project (e.g. build/nvme.device)")
    p_bar.add_argument("--guest-cmd", default="",
                       help="Command to run on guest (default: USB:<binary-name>)")
    p_bar.add_argument("--args", default="")
    p_bar.add_argument("--timeout", type=float, default=60)
    p_bar.add_argument("--no-clean", action="store_true")
    p_bar.add_argument("--tcp-deploy", action="store_true",
                       help="Deploy via TCP upload instead of USB FAT drive")

    # test: full cycle with test result parsing
    p_test = sub.add_parser("test", help="Build, deploy, run, and parse test results")
    p_test.add_argument("--project-dir", required=True,
                        help="Relative path from repo root (e.g. projects/AmigaBlockDevLibrary)")
    p_test.add_argument("--binary", required=True,
                        help="Relative path to test binary (e.g. build/test_blockdev)")
    p_test.add_argument("--guest-cmd", default="",
                        help="Command to run on guest (default: USB:<binary-name>)")
    p_test.add_argument("--args", default="")
    p_test.add_argument("--timeout", type=float, default=120)
    p_test.add_argument("--no-clean", action="store_true")
    p_test.add_argument("--tcp-deploy", action="store_true",
                        help="Deploy via TCP upload instead of USB FAT drive")

    # deploy: just copy to shared dir (USB) or upload via TCP
    p_dep = sub.add_parser("deploy", help="Deploy a binary to the guest")
    p_dep.add_argument("src", help="Host path to binary")
    p_dep.add_argument("--tcp", action="store_true",
                       help="Upload via TCP instead of USB FAT")
    p_dep.add_argument("--guest-path", default="",
                       help="Guest destination path for TCP upload (default: T:<filename>)")

    # status: check if guest is alive
    sub.add_parser("status", help="Check if guest SerialShell is reachable")

    args = parser.parse_args()
    if not args.action:
        parser.print_help()
        sys.exit(1)

    if args.action == "status":
        ok = check_status(args.host, args.port)
        sys.exit(0 if ok else 1)

    elif args.action == "cmd":
        output = run_on_guest(args.command, args.host, args.port, args.timeout)
        print(output)

    elif args.action == "run":
        cmd = args.binary
        if args.args:
            cmd += " " + args.args
        output = run_on_guest(cmd, args.host, args.port, args.timeout)
        print(output)

    elif args.action == "deploy":
        if args.tcp:
            filename = os.path.basename(args.src)
            guest_path = make_guest_path(args.guest_path, filename)
            deploy_binary_tcp(args.src, guest_path, args.host, args.port)
            print(f"Deployed to {guest_path}")
        else:
            guest_path = deploy_binary(args.src, args.shared_dir)
            print(f"Deployed to {guest_path}")

    elif args.action == "build-and-run":
        # Step 1: Build
        if not docker_build(args.project_dir, make_clean=not args.no_clean):
            sys.exit(1)

        # Step 2: Deploy
        src_path = os.path.join(REPO_ROOT, args.project_dir, args.binary)
        if not os.path.isfile(src_path):
            print(f"Binary not found after build: {src_path}")
            sys.exit(1)
        guest_path = do_deploy(src_path, args.guest_cmd, args.shared_dir,
                               args.tcp_deploy, args.host, args.port)

        # Step 3: Run
        guest_cmd = args.guest_cmd or guest_path
        if args.args:
            guest_cmd += " " + args.args

        print(f"\nRunning on guest: {guest_cmd}")
        output = run_on_guest(guest_cmd, args.host, args.port, args.timeout)
        print(f"\n--- Guest Output ---\n{output}\n--- End ---")

    elif args.action == "test":
        all_passed = do_test(
            project_dir=args.project_dir,
            binary=args.binary,
            guest_cmd=args.guest_cmd,
            args_str=args.args,
            host=args.host,
            port=args.port,
            shared_dir=args.shared_dir,
            timeout=args.timeout,
            make_clean=not args.no_clean,
            tcp_deploy=args.tcp_deploy,
        )
        sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
