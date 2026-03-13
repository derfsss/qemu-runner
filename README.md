# QEMU Runner — AmigaOS 4 Dev Cycle

Automated QEMU lifecycle + cross-compile + deploy + run + capture for AmigaOS 4 development on Windows.

Build your AmigaOS 4 program, upload it to the guest, run it, and capture the output — all in one command.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│ Windows Host                                                  │
│                                                               │
│  ┌──────────────┐     ┌──────────────────────────────────┐   │
│  │ Terminal /    │────▶│ dev_cycle.py                     │   │
│  │ Claude Code   │     │  start/stop/reset/build-run      │   │
│  └──────────────┘     └──────┬────────────┬──────────────┘   │
│                              │            │                   │
│              ┌───────────────┘            │                   │
│              ▼                            ▼                   │
│  ┌────────────────────┐     ┌─────────────────────────┐      │
│  │ qemu_manager.py    │     │ serial_client.py        │      │
│  │  launches QEMU     │     │  TCP commands + files    │      │
│  │  auto-restart      │     │  upload / download       │      │
│  └────────┬───────────┘     └───────────┬─────────────┘      │
│           │                             │ TCP :4321           │
│  ┌────────┴───────────┐                 │                     │
│  │ qmp_client.py      │                 │                     │
│  │  QMP :4322         │                 │                     │
│  │  reset/quit/status │                 │                     │
│  └────────────────────┘                 │                     │
│           │ QEMU subprocess             │                     │
├───────────┼─────────────────────────────┼─────────────────────┤
│  ┌────────▼─────────────────────────────▼──────────────┐     │
│  │ QEMU AmigaOS 4 Guest                               │     │
│  │                                                      │     │
│  │  SerialShell (C:SerialShell)                        │     │
│  │    TCP :4321 listener via bsdsocket.library         │     │
│  │    - Execute commands, return output                 │     │
│  │    - Binary file upload/download                     │     │
│  └──────────────────────────────────────────────────────┘     │
└──────────────────────────────────────────────────────────────┘
```

Also supports **remote mode** for real AmigaOS 4 hardware on the network (no QEMU/QMP needed — just SerialShell).

## Quick Start

```bash
# 1. Start QEMU, wait for AmigaOS to boot
py dev_cycle.py start --wait

# 2. Build a program, upload it, run it, check test output
py dev_cycle.py build-run \
  --project-dir projects/myapp \
  --binary build/myapp \
  --test

# 3. Stop QEMU when done
py dev_cycle.py stop
```

## Installation

### Prerequisites

| Requirement | Notes |
|-------------|-------|
| **Windows, Linux, or macOS** | Cross-platform process management (auto-detected) |
| **Python 3.12+** | On Windows: native install with `py` launcher (not MSYS2) |
| **Docker** | For cross-compilation with `walkero/amigagccondocker:os4-gcc11`. On Windows, runs via WSL; on Linux/macOS, runs natively. |
| **QEMU** | Custom build with AmigaOne/Pegasos2 PPC support |
| **AmigaOS 4.1** | Installed in a QEMU disk image |

> **Windows note:** Use native Windows Python (`py` launcher), not MSYS2 Python. MSYS2 cannot launch Windows executables (like QEMU) via `subprocess.Popen` due to path resolution issues.

### Step 1: Clone the repo

```bash
git clone https://github.com/derfsss/qemu-runner.git
cd qemu-runner
```

### Step 2: Pull the Docker cross-compiler

```bash
wsl sh -c "docker pull walkero/amigagccondocker:os4-gcc11"
```

### Step 3: Build SerialShell (the guest-side TCP listener)

SerialShell is a small AmigaOS 4 program that listens on TCP port 4321 and executes commands sent by the host.

```bash
# Adjust the -v mount to match where you cloned the repo
wsl sh -c "docker run --rm -v /mnt/c/path/to/qemu-runner:/src \
  -w /src/amiga \
  walkero/amigagccondocker:os4-gcc11 make clean"

wsl sh -c "docker run --rm -v /mnt/c/path/to/qemu-runner:/src \
  -w /src/amiga \
  walkero/amigagccondocker:os4-gcc11 make all"
```

This produces the `amiga/serialshell` binary (PPC ELF).

### Step 4: Install SerialShell on the AmigaOS guest

Transfer `serialshell` to the guest (e.g. via a shared FAT drive, USB image, or manual upload) and copy it to the system path:

```
Copy <source>:serialshell SYS:C/SerialShell
```

### Step 5: Configure AmigaOS to auto-start SerialShell

Create `S:SerialShell-Startup`:
```
C:SerialShell
```

Add this line to `S:User-Startup`:
```
NewShell "CON:0/400/640/200/SerialShell/AUTO/CLOSE" FROM S:SerialShell-Startup
```

This launches SerialShell in a visible console window with scrollback on the Workbench. You can see connection activity and debug output there.

### Step 6: Configure QEMU

Your QEMU config needs two additions for the host tools to communicate with the guest:

1. **QMP** (QEMU Machine Protocol) — for machine control (reset, quit, status):
   ```
   -qmp tcp:localhost:4322,server,nowait
   ```

2. **Port forwarding** — so the host can reach SerialShell inside the guest:
   ```
   hostfwd=tcp::4321-:4321
   ```
   Add this to the `-netdev user,...` argument in your QEMU config.

If you use a Kyvos-style JSON config, add a `"qmp"` key:
```json
{
  "args": {
    "qmp": "-qmp tcp:localhost:4322,server,nowait",
    ...
  }
}
```

### Step 7: Configure paths in dev_cycle.py

Edit the constants at the top of `dev_cycle.py` to match your setup:

```python
REPO_ROOT = r"C:\path\to\your\project\root"       # or "/home/user/projects/root" on Linux

# DOCKER_CMD is auto-detected:
#   Windows: wraps docker in "wsl sh -c ..." (set the -v mount to the WSL path of REPO_ROOT)
#   Linux/macOS: runs docker natively (uses REPO_ROOT directly)

DEFAULT_CONFIG = r"C:\path\to\your\qemu\config_dev.json"
DEFAULT_QEMU_PATH = r"C:\path\to\qemu-system-ppc.exe"  # or "/usr/bin/qemu-system-ppc"
```

## Usage

All commands use `py dev_cycle.py` on Windows or `python3 dev_cycle.py` on Linux/macOS.

### QEMU Lifecycle

```bash
# Start QEMU with auto-restart manager, wait for AmigaOS to boot
py dev_cycle.py start --wait

# Start without waiting (returns immediately after QEMU launches)
py dev_cycle.py start

# Check if QEMU and guest are running
py dev_cycle.py status

# Reboot AmigaOS (QEMU stays running), wait for SerialShell
py dev_cycle.py reset

# Stop QEMU and kill the auto-restart manager
py dev_cycle.py stop
```

The `start` command launches `qemu_manager.py` as a detached background process that automatically restarts QEMU if it crashes. The `stop` command kills the manager first (preventing auto-restart), then sends QMP quit to QEMU.

### Build, Deploy, and Run

The `build-run` command performs the full cycle: cross-compile via Docker, upload the binary to the guest, execute it, and capture the output.

```bash
# Build + upload + run, parse test output
py dev_cycle.py build-run \
  --project-dir path/to/project \
  --binary build/myapp \
  --test

# Skip make clean (incremental build)
py dev_cycle.py build-run \
  --project-dir path/to/project \
  --binary build/myapp \
  --test --no-clean

# Specify guest destination and arguments
py dev_cycle.py build-run \
  --project-dir path/to/project \
  --binary build/myapp \
  --guest-dest "SYS:C/" \
  --args "-v --debug" \
  --timeout 120
```

**Options:**
| Flag | Default | Description |
|------|---------|-------------|
| `--project-dir` | *(required)* | Path to the project directory (relative to REPO_ROOT) |
| `--binary` | *(required)* | Path to the built binary (relative to project dir) |
| `--guest-dest` | `T:` | AmigaOS destination path (temp dir by default) |
| `--args` | *(none)* | Arguments to pass to the program on the guest |
| `--timeout` | 60 | Execution timeout in seconds |
| `--no-clean` | off | Skip `make clean` (incremental build) |
| `--test` | off | Parse output for PASS/FAIL test results |

### Remote Mode (Real Hardware)

For real AmigaOS 4 machines on the network, use `--remote` with `--host`:

```bash
# Check if SerialShell is running on the remote machine
py dev_cycle.py --remote --host 192.168.1.50 status

# Reboot the remote machine (sends 'reboot' via SerialShell)
py dev_cycle.py --remote --host 192.168.1.50 reset

# Build, upload, and run on real hardware
py dev_cycle.py --remote --host 192.168.1.50 build-run \
  --project-dir path/to/project \
  --binary build/myapp \
  --test
```

In remote mode, `start` and `stop` are not available (the host can't launch/kill QEMU on a remote machine). The `reset` command sends `reboot` via SerialShell instead of using QMP.

### Direct Commands

You can also use the individual tools directly:

```bash
# Send a shell command to the guest
py serial_client.py cmd "version"

# Upload a file
py serial_client.py upload local_file.bin "T:remote_file"

# Download a file
py serial_client.py download "SYS:C/SerialShell" local_copy.bin

# Interactive console (type commands, see output)
py serial_client.py interactive

# QMP control
py qmp_client.py status
py qmp_client.py reset
py qmp_client.py quit
```

### Python API

```python
from serial_client import SerialClient
from qmp_client import QMPClient

# Execute a command on the guest
client = SerialClient("localhost", 4321)
client.connect()
output = client.send_command("echo hello", timeout=10)
print(output)

# Upload a file
client.upload_file("local.bin", "T:remote.bin")

# Download a file
client.download_file("SYS:C/Dir", "dir_binary.bin")
client.close()

# Reset QEMU via QMP
qmp = QMPClient("localhost", 4322)
qmp.connect()
qmp.reset()
qmp.close()
```

## SerialShell Protocol

SerialShell uses a simple text+binary protocol over TCP port 4321:

1. Server sends `SERIALSHELL_READY\n` on connect
2. Client sends a command line terminated by `\n`
3. Server executes it, sends output, then `___SERIALSHELL_DONE___\n`
4. Special commands:
   - `SERIALSHELL_UPLOAD <path> <size>\n` + `<size>` raw bytes — file upload
   - `SERIALSHELL_DOWNLOAD <path>\n` — server sends `SERIALSHELL_FILE <size>\n` + raw bytes + done marker
   - `SERIALSHELL_QUIT\n` — clean disconnect

## Test Output Convention

Programs should print test results in one of these formats:
```
Test 1: description ... PASS
Test 2: description ... FAIL
  PASS: description
  FAIL: description
Results: N/M passed
```

`dev_cycle.py --test` parses these and reports a summary with pass/fail counts.

## Files

| File | Purpose |
|------|---------|
| `dev_cycle.py` | Main orchestrator — start/stop/reset/build-run |
| `qemu_manager.py` | Launches QEMU, auto-restarts on crash |
| `qmp_client.py` | QMP client for QEMU machine control (reset/quit/status) |
| `serial_client.py` | TCP client for SerialShell (commands + file transfer) |
| `test_runner.py` | Legacy test runner (superseded by dev_cycle.py) |
| `amiga/serialshell.c` | AmigaOS 4 TCP listener (guest-side, uses bsdsocket.library) |
| `amiga/hello.c` | Simple test program for workflow validation |
| `amiga/Makefile` | Cross-compile Makefile for guest binaries |

## Troubleshooting

| Problem | Solution |
|---------|----------|
| **"Guest: UNREACHABLE"** | QEMU may not be running, or AmigaOS hasn't finished booting. Run `py dev_cycle.py start --wait`. |
| **Upload hangs on large files** | TCP throughput varies with QEMU's emulated NIC. Files up to 16MB work but speeds fluctuate (50–2500 KB/s). |
| **SerialShell crash (DSI)** | Reboot the guest with `py dev_cycle.py reset`. The AmigaOS console window shows the crash log. |
| **QEMU won't start** | Check `qemu_manager.log` in the qemu-runner directory. |
| **Auto-restart won't stop** | Run `py dev_cycle.py stop` — this kills the manager before quitting QEMU. |
| **Python can't find QEMU** | Use native Windows Python (`py` launcher), not MSYS2 Python. MSYS2 can't resolve Windows paths in subprocess. |
| **`Permission denied` on QEMU temp files** | Ensure `qemu_manager.py` sets `cwd` to the QEMU install directory. |
