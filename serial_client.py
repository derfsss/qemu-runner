#!/usr/bin/env python3
"""
TCP client for communicating with AmigaOS 4 SerialShell listener.

SerialShell runs on the guest as a TCP server (port 4321). Protocol:
  - On connect, server sends "SERIALSHELL_READY\n"
  - Client sends a command line terminated by \n
  - Server executes it, sends output, then "___SERIALSHELL_DONE___\n"
  - Client sends "SERIALSHELL_QUIT\n" to disconnect cleanly

Usage (CLI):
    python serial_client.py cmd "echo hello"
    python serial_client.py run "USB:test.exe" --timeout 30
    python serial_client.py wait
    python serial_client.py interactive

Usage (Python API):
    from serial_client import SerialClient
    client = SerialClient()
    client.connect()
    output = client.send_command("echo hello", timeout=10)
    print(output)
    client.close()
"""

import argparse
import os
import socket
import sys
import time

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 4321
DEFAULT_TIMEOUT = 30
RECV_CHUNK = 4096

# Touch file used by qemu_manager idle timeout — updated on each connection
_ACTIVITY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              ".last_activity")
READY_MARKER = "SERIALSHELL_READY\n"
DONE_MARKER = "___SERIALSHELL_DONE___\n"
QUIT_CMD = "SERIALSHELL_QUIT\n"
UPLOAD_CMD = "SERIALSHELL_UPLOAD"
DOWNLOAD_CMD = "SERIALSHELL_DOWNLOAD"
UPLOAD_OK = "SERIALSHELL_UPLOAD_OK"
UPLOAD_FAIL = "SERIALSHELL_UPLOAD_FAIL"
FILE_HEADER = "SERIALSHELL_FILE"


def _touch_activity():
    """Update the activity timestamp file for idle timeout tracking."""
    try:
        with open(_ACTIVITY_FILE, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


class SerialClient:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host = host
        self.port = port
        self.sock: socket.socket | None = None
        self._buf = ""  # Receive buffer for leftover data

    def connect(self, timeout: float = 10.0, retries: int = 3,
                retry_interval: float = 10.0):
        """Connect to the SerialShell TCP server and wait for READY.

        Retries on connection refused / timeout, waiting retry_interval
        seconds between attempts.
        """
        last_err = None
        for attempt in range(1, retries + 1):
            try:
                self._try_connect(timeout)
                return
            except (ConnectionRefusedError, ConnectionError,
                    TimeoutError, OSError) as e:
                last_err = e
                if attempt < retries:
                    time.sleep(retry_interval)

        raise last_err

    def _try_connect(self, timeout: float):
        """Single connection attempt — connect and wait for READY."""
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect((self.host, self.port))
        self._buf = ""

        # Wait for the READY marker from the server
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                remaining = deadline - time.time()
                self.sock.settimeout(min(2.0, max(0.1, remaining)))
                data = self.sock.recv(RECV_CHUNK)
                if not data:
                    raise ConnectionError("Server closed connection during handshake")
                self._buf += data.decode("latin-1", errors="replace")
                if READY_MARKER in self._buf:
                    _touch_activity()
                    # Discard everything up to and including the READY marker
                    idx = self._buf.index(READY_MARKER) + len(READY_MARKER)
                    self._buf = self._buf[idx:]
                    return
            except socket.timeout:
                continue

        raise TimeoutError(f"Did not receive READY within {timeout}s")

    def close(self):
        """Send quit command and close the connection."""
        if self.sock:
            try:
                self.sock.sendall(QUIT_CMD.encode("latin-1"))
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
            self._buf = ""

    def wait_for_ready(self, timeout: float = 120.0) -> bool:
        """Wait for the SerialShell to become reachable (retries connection)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                remaining = deadline - time.time()
                self.connect(timeout=min(5.0, remaining))
                return True
            except (ConnectionRefusedError, TimeoutError, OSError):
                time.sleep(1)
                continue
        return False

    def send_command(self, cmd: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        """
        Send a command to SerialShell and return the output.

        The server executes the command, sends output, then the DONE marker.
        """
        if not self.sock:
            raise RuntimeError("Not connected")

        # Send command (one line, terminated by \n)
        line = cmd.strip() + "\n"
        self.sock.sendall(line.encode("latin-1"))

        # Read until we see the DONE marker or timeout
        deadline = time.time() + timeout
        while time.time() < deadline:
            if DONE_MARKER in self._buf:
                idx = self._buf.index(DONE_MARKER)
                output = self._buf[:idx]
                self._buf = self._buf[idx + len(DONE_MARKER):]
                return output.strip()

            try:
                remaining = deadline - time.time()
                self.sock.settimeout(min(2.0, max(0.1, remaining)))
                data = self.sock.recv(RECV_CHUNK)
                if not data:
                    # Server closed connection
                    output = self._buf.strip()
                    self._buf = ""
                    return f"[CONNECTION CLOSED]\n{output}" if output else "[CONNECTION CLOSED]"
                self._buf += data.decode("latin-1", errors="replace")
            except socket.timeout:
                continue

        # Timeout — return what we have
        output = self._buf.strip()
        self._buf = ""
        return f"[TIMEOUT after {timeout}s]\n{output}" if output else f"[TIMEOUT after {timeout}s]"

    def upload_file(self, local_path: str, guest_path: str,
                    timeout: float = 120.0) -> bool:
        """Upload a file from the host to the guest.

        Sends: SERIALSHELL_UPLOAD <guest_path> <size>\n
        Then sends <size> raw bytes.
        Server replies SERIALSHELL_UPLOAD_OK or SERIALSHELL_UPLOAD_FAIL.
        Returns True on success.
        """
        if not self.sock:
            raise RuntimeError("Not connected")

        file_size = os.path.getsize(local_path)
        header = f"{UPLOAD_CMD} {guest_path} {file_size}\n"
        # Use the full timeout for sending — large files block in sendall
        # while the emulated guest drains the TCP buffer slowly
        self.sock.settimeout(timeout)
        self.sock.sendall(header.encode("latin-1"))

        # Send file data
        with open(local_path, "rb") as f:
            remaining = file_size
            while remaining > 0:
                chunk = f.read(min(8192, remaining))
                if not chunk:
                    break
                self.sock.sendall(chunk)
                remaining -= len(chunk)

        # Wait for response
        deadline = time.time() + timeout
        while time.time() < deadline:
            # Check buffer for response
            nl = self._buf.find("\n")
            if nl >= 0:
                line = self._buf[:nl]
                self._buf = self._buf[nl + 1:]
                if UPLOAD_OK in line:
                    return True
                if UPLOAD_FAIL in line:
                    msg = line.replace(UPLOAD_FAIL, "").strip()
                    raise RuntimeError(f"Upload failed: {msg}")
                # Unexpected line — keep reading
                continue

            try:
                remaining_t = deadline - time.time()
                self.sock.settimeout(min(2.0, max(0.1, remaining_t)))
                data = self.sock.recv(RECV_CHUNK)
                if not data:
                    raise ConnectionError("Server closed connection during upload")
                self._buf += data.decode("latin-1", errors="replace")
            except socket.timeout:
                continue

        raise TimeoutError(f"Upload response timeout after {timeout}s")

    def download_file(self, guest_path: str, local_path: str,
                      timeout: float = 120.0) -> int:
        """Download a file from the guest to the host.

        Sends: SERIALSHELL_DOWNLOAD <guest_path>\n
        Server replies: SERIALSHELL_FILE <size>\n followed by <size> raw bytes,
        then ___SERIALSHELL_DONE___\n.
        Returns the number of bytes downloaded.
        """
        if not self.sock:
            raise RuntimeError("Not connected")

        header = f"{DOWNLOAD_CMD} {guest_path}\n"
        self.sock.sendall(header.encode("latin-1"))

        # Read the SERIALSHELL_FILE <size> header
        deadline = time.time() + timeout
        file_size = None
        while time.time() < deadline:
            nl = self._buf.find("\n")
            if nl >= 0:
                line = self._buf[:nl]
                self._buf = self._buf[nl + 1:]
                if line.startswith(FILE_HEADER):
                    size_str = line[len(FILE_HEADER):].strip()
                    file_size = int(size_str)
                    break

            try:
                remaining_t = deadline - time.time()
                self.sock.settimeout(min(2.0, max(0.1, remaining_t)))
                data = self.sock.recv(RECV_CHUNK)
                if not data:
                    raise ConnectionError("Server closed connection during download")
                self._buf += data.decode("latin-1", errors="replace")
            except socket.timeout:
                continue

        if file_size is None:
            raise TimeoutError(f"Download header timeout after {timeout}s")

        if file_size == 0:
            # File not found or empty — still need to consume DONE marker
            self._wait_for_done(deadline)
            raise FileNotFoundError(f"Guest file not found or empty: {guest_path}")

        # The buffer may already contain some of the file data (as latin-1 text).
        # Convert what we have back to bytes, then read the rest from socket.
        raw_bytes = self._buf.encode("latin-1")
        self._buf = ""

        with open(local_path, "wb") as f:
            written = 0
            # Write from pre-buffered data
            if raw_bytes:
                to_write = min(len(raw_bytes), file_size)
                f.write(raw_bytes[:to_write])
                written += to_write
                # Put any excess back into the text buffer
                if len(raw_bytes) > file_size:
                    self._buf = raw_bytes[file_size:].decode("latin-1", errors="replace")

            # Read remaining from socket
            while written < file_size:
                remaining_t = deadline - time.time()
                if remaining_t <= 0:
                    raise TimeoutError(f"Download data timeout")
                self.sock.settimeout(min(2.0, max(0.1, remaining_t)))
                need = file_size - written
                data = self.sock.recv(min(RECV_CHUNK, need))
                if not data:
                    raise ConnectionError("Server closed during download")
                f.write(data)
                written += len(data)

        # Wait for DONE marker
        self._wait_for_done(deadline)
        return file_size

    def _wait_for_done(self, deadline: float):
        """Consume data until the DONE marker is found."""
        while time.time() < deadline:
            if DONE_MARKER in self._buf:
                idx = self._buf.index(DONE_MARKER)
                self._buf = self._buf[idx + len(DONE_MARKER):]
                return
            try:
                remaining = deadline - time.time()
                self.sock.settimeout(min(2.0, max(0.1, remaining)))
                data = self.sock.recv(RECV_CHUNK)
                if not data:
                    return
                self._buf += data.decode("latin-1", errors="replace")
            except socket.timeout:
                continue

    def send_raw(self, text: str):
        """Send raw text (no protocol framing)."""
        if not self.sock:
            raise RuntimeError("Not connected")
        self.sock.sendall(text.encode("latin-1"))

    def read_raw(self, timeout: float = 5.0) -> str:
        """Read raw data until timeout or silence."""
        if not self.sock:
            raise RuntimeError("Not connected")
        buf = self._buf
        self._buf = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.sock.settimeout(min(1.0, max(0.1, deadline - time.time())))
                data = self.sock.recv(RECV_CHUNK)
                if data:
                    buf += data.decode("latin-1", errors="replace")
            except socket.timeout:
                if buf:
                    break
                continue
        return buf


def cmd_interactive(client: SerialClient):
    """Interactive console — sends commands, prints output."""
    print(f"Connected to SerialShell at {client.host}:{client.port}")
    print("Type commands (Ctrl+C or 'quit' to exit):\n")
    try:
        while True:
            try:
                cmd = input("AmigaOS> ")
            except EOFError:
                break
            if cmd.strip().lower() in ("quit", "exit"):
                break
            if not cmd.strip():
                continue
            output = client.send_command(cmd)
            if output:
                print(output)
    except KeyboardInterrupt:
        print("\nDisconnected.")


def main():
    parser = argparse.ArgumentParser(description="TCP client for AmigaOS 4 SerialShell")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)

    sub = parser.add_subparsers(dest="action")

    p_cmd = sub.add_parser("cmd", help="Send a command and print output")
    p_cmd.add_argument("command", help="Shell command to send")
    p_cmd.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)

    p_run = sub.add_parser("run", help="Run a binary on the guest")
    p_run.add_argument("binary", help="Path to binary on guest (e.g. USB:test.exe)")
    p_run.add_argument("--args", default="", help="Arguments to pass")
    p_run.add_argument("--timeout", type=float, default=60)

    p_wait = sub.add_parser("wait", help="Wait for SerialShell to be reachable")
    p_wait.add_argument("--timeout", type=float, default=120)

    p_upload = sub.add_parser("upload", help="Upload a file to the guest")
    p_upload.add_argument("local", help="Local file path")
    p_upload.add_argument("guest", help="Guest file path (e.g. T:myapp)")
    p_upload.add_argument("--timeout", type=float, default=120)

    p_download = sub.add_parser("download", help="Download a file from the guest")
    p_download.add_argument("guest", help="Guest file path")
    p_download.add_argument("local", help="Local file path to save to")
    p_download.add_argument("--timeout", type=float, default=120)

    sub.add_parser("interactive", help="Interactive console")

    args = parser.parse_args()

    if not args.action:
        parser.print_help()
        sys.exit(1)

    client = SerialClient(args.host, args.port)

    try:
        if args.action == "wait":
            print(f"Waiting for SerialShell (timeout {args.timeout}s)...")
            if client.wait_for_ready(args.timeout):
                print("SerialShell is ready!")
                client.close()
                sys.exit(0)
            else:
                print("Timeout waiting for SerialShell.")
                sys.exit(1)

        client.connect()

        if args.action == "cmd":
            output = client.send_command(args.command, timeout=args.timeout)
            print(output)

        elif args.action == "run":
            cmd = args.binary
            if args.args:
                cmd += " " + args.args
            output = client.send_command(cmd, timeout=args.timeout)
            print(output)

        elif args.action == "upload":
            client.upload_file(args.local, args.guest, timeout=args.timeout)
            size = os.path.getsize(args.local)
            print(f"Uploaded {args.local} -> {args.guest} ({size} bytes)")

        elif args.action == "download":
            size = client.download_file(args.guest, args.local, timeout=args.timeout)
            print(f"Downloaded {args.guest} -> {args.local} ({size} bytes)")

        elif args.action == "interactive":
            cmd_interactive(client)

    except ConnectionRefusedError:
        print(f"Cannot connect to {args.host}:{args.port} — is QEMU running with SerialShell?",
              file=sys.stderr)
        sys.exit(1)
    except TimeoutError as e:
        print(f"Timeout: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        client.close()


if __name__ == "__main__":
    main()
