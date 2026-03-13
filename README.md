# QEMU AmigaOS 4 Dev Cycle

Automated QEMU lifecycle + build + deploy + run + capture for AmigaOS 4 development.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│ Windows 11 Host                                              │
│                                                              │
│  ┌──────────────┐     ┌──────────────────────────────────┐  │
│  │ Claude Code   │────▶│ dev_cycle.py                     │  │
│  │ (any session) │     │  start/stop/reset/build-run      │  │
│  └──────────────┘     └──────┬────────────┬──────────────┘  │
│                              │            │                  │
│              ┌───────────────┘            │                  │
│              ▼                            ▼                  │
│  ┌────────────────────┐     ┌─────────────────────────┐     │
│  │ qemu_manager.py    │     │ serial_client.py        │     │
│  │  launches QEMU     │     │  TCP commands + files    │     │
│  │  auto-restart      │     │  upload / download       │     │
│  └────────┬───────────┘     └───────────┬─────────────┘     │
│           │                             │ TCP :4321          │
│  ┌────────┴───────────┐                 │                    │
│  │ qmp_client.py      │                 │                    │
│  │  QMP :4322         │                 │                    │
│  │  reset/quit/status │                 │                    │
│  └────────────────────┘                 │                    │
│           │ QEMU subprocess             │                    │
├───────────┼─────────────────────────────┼────────────────────┤
│  ┌────────▼─────────────────────────────▼──────────────┐    │
│  │ QEMU AmigaOS 4 Guest                               │    │
│  │                                                      │    │
│  │  SerialShell (C:SerialShell)                        │    │
│  │    TCP :4321 listener via bsdsocket.library         │    │
│  │    - Execute commands, return output                 │    │
│  │    - Binary file upload/download                     │    │
│  │                                                      │    │
│  │  USB: ◀──▶ S:\temp (FAT drive, optional)           │    │
│  └──────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────┘
```

## Setup (one-time)

### 1. Prerequisites

- **Windows 11** with Python 3.12+ (`py` launcher)
- **WSL + Docker** with `walkero/amigagccondocker:os4-gcc11` image
- **QEMU** (custom build with AmigaOne support)
- **AmigaOS 4.1** installed in QEMU disk image

### 2. Build SerialShell (guest-side TCP listener)

```bash
wsl sh -c "docker run --rm -v /mnt/w/Code/amiga/antigravity:/src \
  -w /src/projects/tools/qemu-runner/amiga \
  walkero/amigagccondocker:os4-gcc11 make clean"

wsl sh -c "docker run --rm -v /mnt/w/Code/amiga/antigravity:/src \
  -w /src/projects/tools/qemu-runner/amiga \
  walkero/amigagccondocker:os4-gcc11 make all"
```

Copy the built `serialshell` binary to `S:\temp\`, then inside AmigaOS:
```
Copy USB:serialshell SYS:C/SerialShell
```

### 3. Configure AmigaOS startup

Create `S:SerialShell-Startup`:
```
C:SerialShell
```

Add to `S:User-Startup`:
```
NewShell "CON:0/400/640/200/SerialShell/AUTO/CLOSE" FROM S:SerialShell-Startup
```

This launches SerialShell in a visible console window with scrollback.

### 4. QEMU config

Use `config_dev.json` (not the base `config.json`) which adds QMP support:
```
E:\Emulators\QEMU\QEMU_Machines\base_a1\config_dev.json
```

Key additions over the base config:
- `"qmp": "-qmp tcp:localhost:4322,server,nowait"` — QMP machine control
- `hostfwd=tcp::4321-:4321` in network args — SerialShell port forwarding

## Usage

All commands use `py dev_cycle.py` (Windows Python).

### QEMU Lifecycle

```bash
# Start QEMU with auto-restart, wait for AmigaOS to boot
py dev_cycle.py start --wait

# Start without waiting (returns immediately)
py dev_cycle.py start

# Check if QEMU and guest are running
py dev_cycle.py status

# Reboot AmigaOS (QEMU stays running), wait for SerialShell
py dev_cycle.py reset

# Stop QEMU and kill auto-restart manager
py dev_cycle.py stop
```

### Build, Deploy, and Run

```bash
# Build + upload + run a program, parse test output
py dev_cycle.py build-run \
  --project-dir projects/tools/qemu-runner/amiga \
  --binary hello \
  --test

# Build + run without make clean
py dev_cycle.py build-run \
  --project-dir projects/AmigaBlockDevLibrary \
  --binary build/test_blockdev \
  --test --no-clean

# Specify guest destination and arguments
py dev_cycle.py build-run \
  --project-dir projects/MyProject \
  --binary build/myapp \
  --guest-dest "SYS:C/" \
  --args "-v" \
  --timeout 120
```

### Direct Commands

```bash
# Send a shell command to the guest
py serial_client.py cmd "version"

# Upload a file
py serial_client.py upload local_file.bin "T:remote_file"

# Download a file
py serial_client.py download "SYS:C/SerialShell" local_copy.bin

# Interactive console
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

## Protocol

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

`dev_cycle.py --test` parses these and reports a summary.

## Files

| File | Purpose |
|------|---------|
| `dev_cycle.py` | Main orchestrator — start/stop/reset/build-run |
| `qemu_manager.py` | Launches QEMU, auto-restarts on crash |
| `qmp_client.py` | QMP client for QEMU machine control (reset/quit/status) |
| `serial_client.py` | TCP client for SerialShell (commands + file transfer) |
| `test_runner.py` | Legacy test runner (superseded by dev_cycle.py) |
| `amiga/serialshell.c` | AmigaOS 4 TCP listener (guest-side) |
| `amiga/hello.c` | Simple test program for workflow validation |
| `amiga/Makefile` | Cross-compile Makefile for guest binaries |
| `config_dev.json` | QEMU config with QMP enabled (in QEMU_Machines/base_a1/) |

## Troubleshooting

- **"Guest: UNREACHABLE"** — QEMU may not be running, or AmigaOS hasn't finished booting. Try `py dev_cycle.py start --wait`.
- **Upload stuck on large files** — TCP throughput varies due to QEMU's emulated RTL8139 NIC. Files up to 16MB work but speeds fluctuate (50-2500 KB/s).
- **SerialShell crash (DSI)** — If SerialShell crashes, reboot the guest with `py dev_cycle.py reset`. The console window in AmigaOS shows the log.
- **QEMU won't start** — Check `qemu_manager.log` in the qemu-runner directory.
- **Auto-restart won't stop** — Run `py dev_cycle.py stop` which kills the manager before quitting QEMU.
