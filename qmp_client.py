#!/usr/bin/env python3
"""
QMP (QEMU Machine Protocol) client for controlling QEMU.

Connects to QEMU's QMP socket to send machine-level commands
like reset, quit, savevm, loadvm, etc.

Usage (CLI):
    python qmp_client.py reset
    python qmp_client.py quit
    python qmp_client.py status
    python qmp_client.py command query-status

Usage (Python API):
    from qmp_client import QMPClient
    qmp = QMPClient()
    qmp.connect()
    qmp.reset()
    qmp.close()
"""

import argparse
import json
import socket
import sys
import time

DEFAULT_HOST = "localhost"
DEFAULT_QMP_PORT = 4322
RECV_CHUNK = 4096


class QMPClient:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_QMP_PORT):
        self.host = host
        self.port = port
        self.sock: socket.socket | None = None

    def connect(self, timeout: float = 5.0):
        """Connect to QMP and perform capability negotiation."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect((self.host, self.port))

        # QMP sends a greeting on connect
        greeting = self._recv_json()
        if "QMP" not in greeting:
            raise RuntimeError(f"Unexpected QMP greeting: {greeting}")

        # Must send qmp_capabilities to enter command mode
        resp = self._execute("qmp_capabilities")
        return resp

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def _send_json(self, obj: dict):
        data = json.dumps(obj) + "\n"
        self.sock.sendall(data.encode("utf-8"))

    def _recv_json(self) -> dict:
        """Read one JSON object from the socket."""
        buf = ""
        while True:
            data = self.sock.recv(RECV_CHUNK)
            if not data:
                raise ConnectionError("QMP connection closed")
            buf += data.decode("utf-8")
            # QMP sends one JSON object per line
            if "\n" in buf:
                line = buf[:buf.index("\n")]
                return json.loads(line)

    def _execute(self, command: str, **arguments) -> dict:
        """Send a QMP command and return the response."""
        msg = {"execute": command}
        if arguments:
            msg["arguments"] = arguments
        self._send_json(msg)

        # Read response — skip async events (they have "event" key)
        while True:
            resp = self._recv_json()
            if "event" not in resp:
                return resp

    def reset(self) -> dict:
        """Reset the guest (like pressing the reset button)."""
        return self._execute("system_reset")

    def quit(self) -> dict:
        """Quit QEMU entirely."""
        return self._execute("quit")

    def stop(self) -> dict:
        """Pause the guest CPU."""
        return self._execute("stop")

    def cont(self) -> dict:
        """Resume the guest CPU."""
        return self._execute("cont")

    def status(self) -> dict:
        """Query the guest run state."""
        return self._execute("query-status")

    def command(self, cmd: str, **kwargs) -> dict:
        """Send an arbitrary QMP command."""
        return self._execute(cmd, **kwargs)


def main():
    parser = argparse.ArgumentParser(description="QMP client for QEMU control")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_QMP_PORT)

    sub = parser.add_subparsers(dest="action")
    sub.add_parser("reset", help="Reset the guest")
    sub.add_parser("quit", help="Quit QEMU")
    sub.add_parser("stop", help="Pause the guest")
    sub.add_parser("cont", help="Resume the guest")
    sub.add_parser("status", help="Query guest run state")

    p_cmd = sub.add_parser("command", help="Send arbitrary QMP command")
    p_cmd.add_argument("cmd", help="QMP command name")

    args = parser.parse_args()
    if not args.action:
        parser.print_help()
        sys.exit(1)

    qmp = QMPClient(args.host, args.port)
    try:
        qmp.connect()

        if args.action == "reset":
            resp = qmp.reset()
            print(f"Reset: {resp}")
        elif args.action == "quit":
            resp = qmp.quit()
            print(f"Quit: {resp}")
        elif args.action == "stop":
            resp = qmp.stop()
            print(f"Stop: {resp}")
        elif args.action == "cont":
            resp = qmp.cont()
            print(f"Continue: {resp}")
        elif args.action == "status":
            resp = qmp.status()
            print(json.dumps(resp, indent=2))
        elif args.action == "command":
            resp = qmp.command(args.cmd)
            print(json.dumps(resp, indent=2))

    except ConnectionRefusedError:
        print(f"Cannot connect to QMP at {args.host}:{args.port} — is QEMU running with -qmp?",
              file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        qmp.close()


if __name__ == "__main__":
    main()
