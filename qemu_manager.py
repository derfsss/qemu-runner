#!/usr/bin/env python3
"""
QEMU AmigaOS 4 Manager — launches QEMU and auto-restarts on crash.

Reads a Kyvos-style config.json, builds the QEMU command line, and keeps
QEMU running. Guest communication is via SerialShell (TCP port 4321 over
QEMU user-mode networking hostfwd).

Usage:
    python qemu_manager.py <config.json> [--qemu-path ...]
    python qemu_manager.py E:\Emulators\QEMU\QEMU_Machines\base_a1\config.json
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import logging
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("qemu-manager")

DEFAULT_QEMU_PATH = r"E:\Emulators\QEMU\QEMU_Install\qemu-system-ppc.exe"


def _win_to_posix(path: str) -> str:
    """Convert a Windows path (E:\\foo) to MSYS2 POSIX path (/e/foo)."""
    if len(path) >= 2 and path[1] == ":":
        drive = path[0].lower()
        return "/" + drive + path[2:].replace("\\", "/")
    return path
RESTART_DELAY_SECS = 3
MAX_RAPID_CRASHES = 5          # if it crashes this many times within RAPID_WINDOW, stop
RAPID_WINDOW_SECS = 60


def load_config(config_path: str) -> dict:
    with open(config_path, "r") as f:
        return json.load(f)


def build_qemu_cmdline(config: dict, qemu_path: str,
                       display_override: str = "") -> list[str]:
    """Build QEMU command line from config.json args."""
    args = config.get("args", {})

    cmdline = [qemu_path]

    # Order doesn't matter for QEMU args, just iterate
    for key, value in args.items():
        if not value or not value.strip():
            continue

        # Override display backend if requested
        if key == "display" and display_override:
            cmdline.extend(["-display", display_override])
            continue

        # Split the value string into individual args (handles compound args like
        # "-drive if=none,id=hd0,... -device ide-hd,...")
        # We need to be careful with quoted paths
        parts = _split_arg_string(value)
        cmdline.extend(parts)

    return cmdline


def _split_arg_string(s: str) -> list[str]:
    """Split a QEMU arg string respecting quoted paths."""
    result = []
    current = []
    in_quote = False
    quote_char = None

    for ch in s:
        if ch in ('"', "'") and not in_quote:
            in_quote = True
            quote_char = ch
            # Don't include the quote itself
        elif ch == quote_char and in_quote:
            in_quote = False
            quote_char = None
        elif ch == ' ' and not in_quote:
            if current:
                result.append(''.join(current))
                current = []
        else:
            current.append(ch)

    if current:
        result.append(''.join(current))

    return result


class QemuManager:
    def __init__(self, config_path: str, qemu_path: str):
        self.config_path = config_path
        self.qemu_path = qemu_path
        self.process: subprocess.Popen | None = None
        self.should_run = True
        self.crash_times: list[float] = []

    def start(self):
        """Main loop: launch QEMU, restart on crash."""
        config = load_config(self.config_path)
        cmdline = build_qemu_cmdline(config, self.qemu_path)

        log.info("QEMU command line:")
        log.info("  %s", " ".join(cmdline))

        # Install signal handlers for clean shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        while self.should_run:
            log.info("Starting QEMU...")
            try:
                # Launch QEMU with cwd in QEMU dir for temp file access
                self.process = subprocess.Popen(
                    cmdline,
                    cwd=str(Path(cmdline[0]).parent),
                    stdin=subprocess.DEVNULL,
                    stdout=sys.stdout,
                    stderr=sys.stderr,
                )
                log.info("QEMU started (PID %d)", self.process.pid)

                # Wait for QEMU to exit
                returncode = self.process.wait()
                self.process = None

                if not self.should_run:
                    log.info("QEMU stopped by user request")
                    break

                log.warning("QEMU exited with code %d", returncode)

                # Track crash frequency
                now = time.time()
                self.crash_times.append(now)
                # Remove old entries outside the window
                self.crash_times = [t for t in self.crash_times
                                    if now - t < RAPID_WINDOW_SECS]

                if len(self.crash_times) >= MAX_RAPID_CRASHES:
                    log.error(
                        "QEMU crashed %d times in %d seconds — giving up.",
                        MAX_RAPID_CRASHES, RAPID_WINDOW_SECS
                    )
                    break

                log.info("Restarting in %d seconds...", RESTART_DELAY_SECS)
                time.sleep(RESTART_DELAY_SECS)

            except FileNotFoundError:
                log.error("QEMU binary not found: %s", self.qemu_path)
                break
            except Exception as e:
                log.error("Unexpected error: %s", e)
                if self.should_run:
                    time.sleep(RESTART_DELAY_SECS)

        log.info("QEMU Manager shutting down.")

    def stop(self):
        """Gracefully stop QEMU."""
        self.should_run = False
        if self.process and self.process.poll() is None:
            log.info("Sending quit to QEMU (PID %d)...", self.process.pid)
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log.warning("QEMU didn't exit, killing...")
                self.process.kill()

    def _signal_handler(self, signum, frame):
        log.info("Received signal %d, stopping...", signum)
        self.stop()


def main():
    parser = argparse.ArgumentParser(
        description="QEMU AmigaOS 4 Manager with auto-restart"
    )
    parser.add_argument(
        "config",
        help="Path to Kyvos config.json (e.g. E:\\Emulators\\QEMU\\QEMU_Machines\\base_a1\\config.json)"
    )
    parser.add_argument(
        "--qemu-path", default=DEFAULT_QEMU_PATH,
        help=f"Path to qemu-system-ppc binary (default: {DEFAULT_QEMU_PATH})"
    )
    args = parser.parse_args()

    if not os.path.isfile(args.config):
        log.error("Config file not found: %s", args.config)
        sys.exit(1)

    manager = QemuManager(args.config, args.qemu_path)
    manager.start()


if __name__ == "__main__":
    main()
