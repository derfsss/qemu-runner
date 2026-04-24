/*
 * SerialShell — AmigaOS 4 TCP command listener
 *
 * Listens on a TCP port for commands from the host,
 * executes them via SystemTags(), and sends output back
 * over the TCP connection. Also supports binary file
 * transfer (upload/download) over the same connection.
 *
 * Build:
 *   make serialshell         (preferred — stamps DD.MM.YYYY build date into $VER)
 *   ppc-amigaos-gcc -O2 -o serialshell serialshell.c -lauto
 *                            (fallback — build date falls back to __DATE__ format)
 *
 * Install:
 *   Copy to SYS:C/ and add to S:User-Startup:
 *     Run >NIL: C:SerialShell
 *
 * Protocol (unchanged since v1.0):
 *   - Host connects to TCP port 4321
 *   - Server sends "SERIALSHELL_READY\n"
 *   - Host sends a line terminated by \n:
 *     - Regular command: executed synchronously via SystemTags; output is
 *       captured to a per-command temp file, streamed back with a 64 KiB cap,
 *       followed by "___SERIALSHELL_DONE___\n".
 *     - "SERIALSHELL_UPLOAD <path> <size>\n": receive <size> bytes, write to <path>
 *       Server replies "SERIALSHELL_UPLOAD_OK\n" or "SERIALSHELL_UPLOAD_FAIL <msg>\n"
 *     - "SERIALSHELL_DOWNLOAD <path>\n": server sends "SERIALSHELL_FILE <size>\n"
 *       followed by <size> raw bytes, then "___SERIALSHELL_DONE___\n"
 *     - "SERIALSHELL_RUNCONSOLE <command>\n": runs the command in its own console
 *       window via Execute + SYS_Asynch, captured to a file; server sends the
 *       file contents followed by "___SERIALSHELL_DONE___\n". Required for
 *       programs whose child threads block synchronous SystemTags (e.g. clib4
 *       -athread=native, GDB).
 *     - "SERIALSHELL_QUIT\n": clean disconnect
 *
 * Concurrency (v1.3):
 *   The parent task multiplexes `accept()` against `SIGBREAKF_CTRL_C` via
 *   WaitSelect(). Each accepted connection is released from the parent's
 *   bsdsocket fd table with ReleaseSocket(UNIQUE_ID), handed to a freshly
 *   spawned handler process via CreateNewProcTags, and the parent resumes
 *   accepting immediately. Up to MAX_CHILDREN handlers run in parallel; a
 *   connection arriving past the cap gets a one-line "server busy" reply and
 *   is closed. One wedged command can no longer block the listener.
 *
 * Listener shutdown: SIGBREAKF_CTRL_C to the server task (e.g. `Break <cli> C`)
 * breaks the accept loop cleanly and closes the listen socket. In-flight
 * handler children are NOT waited on — they terminate naturally when their
 * client disconnects.
 */

#include <proto/exec.h>
#include <proto/dos.h>
#include <proto/bsdsocket.h>

#include <exec/exec.h>
#include <dos/dostags.h>

#include <sys/socket.h>
#include <netinet/in.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>

#ifdef SERIALSHELL_AMIUPDATE
#include "amiupdate.h"
#endif

/* AmigaOS version string — visible via the 'Version' command.
 * SERIALSHELL_DATE is supplied by the Makefile (`date +%d.%m.%Y`) so the
 * $VER cookie always carries the actual build date in the DD.MM.YYYY format
 * the Amiga `Version` command expects. __DATE__ is kept as a fallback for
 * ad-hoc compiles without the Makefile — format will differ but builds
 * still succeed. */
#define SERIALSHELL_VERSION "1.3"
#ifndef SERIALSHELL_DATE
#define SERIALSHELL_DATE __DATE__
#endif
static const char __attribute__((used)) verstag[] =
    "\0$VER: SerialShell " SERIALSHELL_VERSION " (" SERIALSHELL_DATE ")";

#define LISTEN_PORT   4321
#define CMD_BUFSIZE   4096
#define RECV_BUFSIZE  4096
#define SEND_BUFSIZE  8192
#define XFER_BUFSIZE  65536
#define READY_MSG     "SERIALSHELL_READY\n"
#define BUSY_MSG      "SERIALSHELL_BUSY server at capacity, try again\n"
#define QUIT_CMD      "SERIALSHELL_QUIT"
#define UPLOAD_CMD    "SERIALSHELL_UPLOAD "
#define DOWNLOAD_CMD  "SERIALSHELL_DOWNLOAD "
#define RUNCONSOLE_CMD "SERIALSHELL_RUNCONSOLE "
#define DONE_MARKER   "___SERIALSHELL_DONE___\n"
/* Output/script path buffer size. Paths are built per-command inside
 * handle_client with the Task pointer and a sequence number, so an
 * orphan from a timed-out command can't lock out the next command or
 * a concurrent handler from writing its own redirect target. */
#define HANDLER_PATH_MAX   80

/* Cap per-command output sent over the network. Anything beyond this
 * is truncated with a marker line. Protects flaky NICs from getting
 * hammered by accidental `List ALL SYS:` style commands. Applies to
 * regular cmd output and SERIALSHELL_RUNCONSOLE captures; binary
 * SERIALSHELL_DOWNLOAD transfers are NOT capped (caller knew the
 * size up-front). */
#define MAX_CMD_OUTPUT_BYTES 65536L
#define TRUNCATE_MARKER      "\n[OUTPUT TRUNCATED AT 65536 BYTES]\n"

/* Spawn-per-connection (v1.3) limits. MAX_CHILDREN caps simultaneous
 * handler processes so a flood of connections can't DoS the box; above
 * the cap the connection is rejected with BUSY_MSG. */
#define MAX_CHILDREN      8
#define CLIENT_STACK_SIZE 65536

/* Live count of in-flight handler children. Mutated by parent and by each
 * child at termination; protected by Forbid/Permit. */
static volatile int in_flight_children = 0;

/* Context handed from parent to a spawned client handler. The parent
 * AllocVec's this, fills sock_id from ReleaseSocket(), plants it in the
 * child's pr_UserData via NP_UserData, and the child FreeVec's it at the
 * end of client_proc_entry. */
struct ClientCtx {
    LONG sock_id;  /* id returned by ISocket->ReleaseSocket(sock, UNIQUE_ID) */
};

/* ------------------------------------------------------------------ */
/* Socket helpers                                                      */
/* ------------------------------------------------------------------ */
/* All helpers take `si` — the SocketIFace to dispatch through — so a
 * spawned handler can use its OWN per-task bsdsocket.library interface
 * (required by ObtainSocket semantics) instead of the parent's. */

/* Send a string over a socket */
static void send_str(struct SocketIFace *si, LONG sock, const char *str)
{
    int len = strlen(str);
    while (len > 0) {
        int sent = si->send(sock, (APTR)str, len, 0);
        if (sent <= 0) break;
        str += sent;
        len -= sent;
    }
}

/* Send exactly n bytes over a socket. Returns 0 on success, -1 on error. */
static int send_all(struct SocketIFace *si, LONG sock, const void *data, int n)
{
    const char *p = (const char *)data;
    while (n > 0) {
        int sent = si->send(sock, (APTR)p, n, 0);
        if (sent <= 0) return -1;
        p += sent;
        n -= sent;
    }
    return 0;
}

/* Receive exactly n bytes from a socket. Returns 0 on success, -1 on error. */
static int recv_all(struct SocketIFace *si, LONG sock, void *data, int n)
{
    char *p = (char *)data;
    while (n > 0) {
        int got = si->recv(sock, p, n, 0);
        if (got <= 0) return -1;
        p += got;
        n -= got;
    }
    return 0;
}

/* Send the contents of a file over a socket, then delete the file.
 * Caps the total bytes sent at MAX_CMD_OUTPUT_BYTES; anything beyond
 * is replaced with a single TRUNCATE_MARKER line. The on-disk file
 * is still deleted regardless of how much we sent. */
static void send_file(struct SocketIFace *si, LONG sock, const char *path)
{
    BPTR fh = IDOS->Open(path, MODE_OLDFILE);
    if (fh) {
        char buf[SEND_BUFSIZE];
        int32 n;
        long sent_total = 0;
        int truncated = 0;
        while ((n = IDOS->Read(fh, buf, sizeof(buf))) > 0) {
            long room = MAX_CMD_OUTPUT_BYTES - sent_total;
            if (room <= 0) { truncated = 1; break; }
            int chunk = (n > room) ? (int)room : (int)n;
            if (send_all(si, sock, buf, chunk) < 0) {
                IDOS->Close(fh);
                IDOS->Delete(path);
                return;
            }
            sent_total += chunk;
            if (chunk < n) { truncated = 1; break; }
        }
        IDOS->Close(fh);
        IDOS->Delete(path);
        if (truncated) {
            send_str(si, sock, TRUNCATE_MARKER);
            IDOS->Printf("SerialShell: output capped at %ld bytes\n",
                         (long)MAX_CMD_OUTPUT_BYTES);
        }
    }
}

/* Read one line from socket (up to \n or buffer full).
 * Returns number of bytes read, 0 on disconnect, -1 on error. */
static int recv_line(struct SocketIFace *si, LONG sock, char *buf, int bufsize)
{
    int pos = 0;
    while (pos < bufsize - 1) {
        char ch;
        int n = si->recv(sock, &ch, 1, 0);
        if (n <= 0) return n;
        if (ch == '\n') break;
        if (ch == '\r') continue;
        buf[pos++] = ch;
    }
    buf[pos] = '\0';
    return pos;
}

/* ------------------------------------------------------------------ */
/* Upload / Download                                                   */
/* ------------------------------------------------------------------ */

/* Handle SERIALSHELL_UPLOAD <path> <size>
 * Receive <size> bytes from socket and write to <path> */
static void handle_upload(struct SocketIFace *si, LONG sock,
                          const char *args, char *buf)
{
    char path[256];
    long size = 0;
    char resp[512];

    /* Parse "<path> <size>" */
    const char *space = NULL;
    int i;
    for (i = strlen(args) - 1; i >= 0; i--) {
        if (args[i] == ' ') {
            space = &args[i];
            break;
        }
    }

    if (!space || space == args) {
        send_str(si, sock, "SERIALSHELL_UPLOAD_FAIL bad syntax\n");
        return;
    }

    /* Copy path */
    int pathlen = (int)(space - args);
    if (pathlen >= (int)sizeof(path)) pathlen = sizeof(path) - 1;
    memcpy(path, args, pathlen);
    path[pathlen] = '\0';

    /* Parse size */
    size = atol(space + 1);
    if (size <= 0) {
        send_str(si, sock, "SERIALSHELL_UPLOAD_FAIL bad size\n");
        return;
    }

    IDOS->Printf("SerialShell: Upload %s (%ld bytes)\n", path, size);

    /* Delete existing file first to ensure clean overwrite */
    IDOS->Delete(path);

    /* Open file for writing with large buffer for performance */
    BPTR fh = IDOS->Open(path, MODE_NEWFILE);
    if (!fh) {
        snprintf(resp, sizeof(resp),
                 "SERIALSHELL_UPLOAD_FAIL cannot open %s\n", path);
        send_str(si, sock, resp);
        /* Must still drain the incoming bytes to keep protocol in sync */
        long remaining = size;
        while (remaining > 0) {
            int chunk = remaining > (long)XFER_BUFSIZE ? XFER_BUFSIZE : (int)remaining;
            if (recv_all(si, sock, buf, chunk) < 0) return;
            remaining -= chunk;
        }
        return;
    }

    /* Receive and write data using buffered FWrite */
    long remaining = size;
    int ok = 1;
    while (remaining > 0) {
        int chunk = remaining > (long)XFER_BUFSIZE ? XFER_BUFSIZE : (int)remaining;
        if (recv_all(si, sock, buf, chunk) < 0) {
            ok = 0;
            break;
        }
        if (IDOS->FWrite(fh, buf, 1, chunk) != (int32)chunk) {
            ok = 0;
            /* Drain remaining to keep protocol in sync */
            remaining -= chunk;
            while (remaining > 0) {
                int c = remaining > (long)XFER_BUFSIZE ? XFER_BUFSIZE : (int)remaining;
                if (recv_all(si, sock, buf, c) < 0) break;
                remaining -= c;
            }
            break;
        }
        remaining -= chunk;
    }

    IDOS->Close(fh);  /* flushes the buffer */

    if (ok) {
        /* Make the file executable */
        IDOS->SetProtection(path, 0);
        IDOS->Printf("SerialShell: Upload complete: %s\n", path);
        send_str(si, sock, "SERIALSHELL_UPLOAD_OK\n");
    } else {
        IDOS->Printf("SerialShell: Upload failed: %s\n", path);
        snprintf(resp, sizeof(resp),
                 "SERIALSHELL_UPLOAD_FAIL write error for %s\n", path);
        send_str(si, sock, resp);
    }
}

/* Handle SERIALSHELL_DOWNLOAD <path>
 * Send file size, then raw bytes, then DONE marker */
static void handle_download(struct SocketIFace *si, LONG sock,
                            const char *path, char *buf)
{
    char header[512];

    IDOS->Printf("SerialShell: Download %s\n", path);

    /* Get file size via ExamineObjectTags */
    struct ExamineData *exd = IDOS->ExamineObjectTags(
        EX_StringNameInput, path,
        TAG_END);
    if (!exd) {
        send_str(si, sock, "SERIALSHELL_FILE 0\n");
        send_str(si, sock, DONE_MARKER);
        return;
    }
    long filesize = (long)exd->FileSize;
    IDOS->FreeDosObject(DOS_EXAMINEDATA, exd);

    /* Open and send */
    BPTR fh = IDOS->Open(path, MODE_OLDFILE);
    if (!fh) {
        send_str(si, sock, "SERIALSHELL_FILE 0\n");
        send_str(si, sock, DONE_MARKER);
        return;
    }

    snprintf(header, sizeof(header), "SERIALSHELL_FILE %ld\n", filesize);
    send_str(si, sock, header);

    long sent_total = 0;
    int32 n;
    while ((n = IDOS->Read(fh, buf, XFER_BUFSIZE)) > 0) {
        if (send_all(si, sock, buf, n) < 0) {
            IDOS->Close(fh);
            return;
        }
        sent_total += n;
    }

    IDOS->Close(fh);

    IDOS->Printf("SerialShell: Download complete: %s (%ld bytes)\n",
                  path, sent_total);
    send_str(si, sock, DONE_MARKER);
}

/* ------------------------------------------------------------------ */
/* Default-command exec                                                */
/* ------------------------------------------------------------------ */
/* Runs `cmd` redirected to out_path and waits for it synchronously.
 *
 * Historical note: the hardening plan's step 4 proposed an async
 * SYS_Asynch + timer.device + NP_NotifyOnDeathSignalBit watchdog so
 * runaway commands could be bounded to a wall-clock timeout. A working
 * prototype existed briefly in the 2026-04-24 development branch, but
 * NP_NotifyOnDeathSignalBit did not fire reliably on the A1222 for
 * shell commands that spawn children (copy, delete behaved differently
 * than echo, list) — the handler waited the full timer interval even
 * for fast commands. The watchdog was ABANDONED because spawn-per-
 * connection (step 3) already prevents a hung command from affecting
 * the listener: a runaway only ever wedges its own handler, which
 * cleans up when the client disconnects. The watchdog would have been
 * a nicety (prompt timeout signalling to the client) rather than a
 * correctness fix, and not worth the AOS4-specific debugging burden.
 *
 * Returns 0 — retained as int for caller API stability.
 */
static int exec_with_watchdog(const char *cmd, const char *out_path)
{
    char execbuf[CMD_BUFSIZE + 256];
    snprintf(execbuf, sizeof(execbuf), "%s >%s", cmd, out_path);
    IDOS->Delete(out_path);

    IDOS->SystemTags(execbuf,
        SYS_Input,    ZERO,
        SYS_Output,   ZERO,
        NP_WindowPtr, (APTR)-1,
        TAG_END);
    return 0;
}

/* ------------------------------------------------------------------ */
/* Per-client loop                                                     */
/* ------------------------------------------------------------------ */

static void handle_client(struct SocketIFace *si, LONG client_sock)
{
    char cmdbuf[CMD_BUFSIZE];

    /* Per-handler + per-command file naming. Including the task pointer
     * isolates concurrent handlers from each other; the per-command
     * sequence ensures that if a command times out with its orphan
     * shell still holding the output file open (AOS4 exclusive write
     * lock), the NEXT command in this handler picks a fresh filename
     * and isn't blocked. */
    struct Task *me_task = IExec->FindTask(NULL);
    int cmd_seq = 0;

    /* Per-socket recv/send timeouts. Without these a silent client can
     * wedge this handler indefinitely. On timeout the underlying recv/send
     * returns -1, which recv_all/recv_line/send_all already treat as a
     * fatal per-client error — the loop breaks and the handler exits.
     *
     * Recv timeout is the *idle* limit between client lines, NOT the
     * total command time — long-running commands aren't affected
     * because their output drains via send_file (with its own SO_SNDTIMEO).
     * Keep it short so a dropped client frees the handler quickly. */
    struct timeval rcv_to = { .tv_sec = 10, .tv_usec = 0 };
    struct timeval snd_to = { .tv_sec = 30, .tv_usec = 0 };
    int keepalive = 1;
    si->setsockopt(client_sock, SOL_SOCKET, SO_RCVTIMEO,
                   &rcv_to, sizeof(rcv_to));
    si->setsockopt(client_sock, SOL_SOCKET, SO_SNDTIMEO,
                   &snd_to, sizeof(snd_to));
    si->setsockopt(client_sock, SOL_SOCKET, SO_KEEPALIVE,
                   &keepalive, sizeof(keepalive));

    /* Allocate transfer buffer on heap to avoid stack overflow */
    char *xferbuf = malloc(XFER_BUFSIZE);
    if (!xferbuf) {
        IDOS->Printf("SerialShell: Out of memory for transfer buffer\n");
        si->CloseSocket(client_sock);
        return;
    }

    send_str(si, client_sock, READY_MSG);

    for (;;) {
        int n = recv_line(si, client_sock, cmdbuf, CMD_BUFSIZE);
        if (n <= 0) break;  /* disconnect or error */

        /* Bump the per-command sequence and build fresh paths. */
        cmd_seq++;
        char temp_output[HANDLER_PATH_MAX];
        char console_output[HANDLER_PATH_MAX];
        char runcmd_script[HANDLER_PATH_MAX];
        snprintf(temp_output,    sizeof(temp_output),
                 "T:serialshell_out_%p_%d.txt",     me_task, cmd_seq);
        snprintf(console_output, sizeof(console_output),
                 "RAM:serialshell_console_%p_%d.txt", me_task, cmd_seq);
        snprintf(runcmd_script,  sizeof(runcmd_script),
                 "T:serialshell_runcmd_%p_%d.sh",   me_task, cmd_seq);

        /* Check for quit command */
        if (strcmp(cmdbuf, QUIT_CMD) == 0) {
            send_str(si, client_sock, "SERIALSHELL_SHUTDOWN\n");
            break;
        }

        /* Check for upload command */
        if (strncmp(cmdbuf, UPLOAD_CMD, strlen(UPLOAD_CMD)) == 0) {
            handle_upload(si, client_sock,
                          cmdbuf + strlen(UPLOAD_CMD), xferbuf);
            continue;
        }

        /* Check for download command */
        if (strncmp(cmdbuf, DOWNLOAD_CMD, strlen(DOWNLOAD_CMD)) == 0) {
            handle_download(si, client_sock,
                            cmdbuf + strlen(DOWNLOAD_CMD), xferbuf);
            continue;
        }

        /* Check for runconsole command:
         *   SERIALSHELL_RUNCONSOLE <command>
         * Runs a command in its own console window with output captured
         * to a file.  For programs that use clib4's -athread=native
         * (like GDB) which spawn persistent child processes that block
         * synchronous SystemTags. */
        if (strncmp(cmdbuf, RUNCONSOLE_CMD, strlen(RUNCONSOLE_CMD)) == 0) {
            const char *cmd = cmdbuf + strlen(RUNCONSOLE_CMD);

            IDOS->Printf("SerialShell: RunConsole: %s\n", cmd);

            IDOS->Delete(console_output);

            /* Build a script that runs the command with output redirected.
               The script runs inside a real shell (via Execute) which
               handles the >file redirect at the shell level. */
            BPTR scriptfh = IDOS->Open(runcmd_script, MODE_NEWFILE);
            if (scriptfh) {
                IDOS->FPrintf(scriptfh, "%s >%s\n", cmd, console_output);
                IDOS->Close(scriptfh);

                /* Run the script in its own console via SystemTags async.
                   The console provides real I/O for clib4 programs. */
                BPTR infh = IDOS->Open("NIL:", MODE_OLDFILE);
                BPTR outfh = IDOS->Open("NIL:", MODE_NEWFILE);
                char exec_cmd[HANDLER_PATH_MAX + 16];
                snprintf(exec_cmd, sizeof(exec_cmd),
                         "Execute %s", runcmd_script);
                IDOS->SystemTags(
                    exec_cmd,
                    SYS_Input,    infh,
                    SYS_Output,   outfh,
                    SYS_Asynch,   TRUE,
                    NP_WindowPtr, (APTR)-1,  /* suppress DOS requesters */
                    TAG_END);

                /* Poll until output file exists and stabilizes */
                int32 prev_size = -1;
                int stable_count = 0;
                for (int i = 0; i < 120; i++) {  /* max 60s */
                    IDOS->Delay(25);  /* 500ms */
                    struct ExamineData *exd = IDOS->ExamineObjectTags(
                        EX_StringNameInput, console_output, TAG_END);
                    if (exd) {
                        int32 cur_size = (int32)exd->FileSize;
                        IDOS->FreeDosObject(DOS_EXAMINEDATA, exd);
                        if (cur_size == prev_size && cur_size > 0) {
                            stable_count++;
                            if (stable_count >= 4)  /* stable for 2s */
                                break;
                        } else {
                            stable_count = 0;
                        }
                        prev_size = cur_size;
                    }
                }
            }

            send_file(si, client_sock, console_output);
            IDOS->Delete(runcmd_script);  /* best-effort cleanup */
            send_str(si, client_sock, DONE_MARKER);
            continue;
        }

        IDOS->Printf("SerialShell: Executing: %s\n", cmdbuf);

        /* Synchronous exec (see exec_with_watchdog note). A hung command
         * wedges THIS handler but never the listener — step 3 ensures
         * new connections get their own handler processes. */
        exec_with_watchdog(cmdbuf, temp_output);

        send_file(si, client_sock, temp_output);

        /* Send end-of-output marker so host knows we're done */
        send_str(si, client_sock, DONE_MARKER);
    }

    free(xferbuf);
    si->CloseSocket(client_sock);
}

/* ------------------------------------------------------------------ */
/* Step 3: per-connection handler process                              */
/* ------------------------------------------------------------------ */
/* Entry point for spawned handler processes. Each handler opens its
 * OWN bsdsocket.library interface (required: bsdsocket fd tables are
 * per-task on AOS4), calls ObtainSocket to pull the socket released by
 * the parent into this task's fd table, runs handle_client to service
 * the connection, and tears everything down. */
static int32 client_proc_entry(STRPTR args, int32 arglen, struct ExecBase *eb)
{
    (void)args; (void)arglen; (void)eb;

    struct Process *me = (struct Process *)IExec->FindTask(NULL);
    /* NP_UserData on AOS4 sets tc_UserData of the underlying Task, not
     * a pr_UserData on Process (that field doesn't exist on AOS4). */
    struct ClientCtx *ctx = (struct ClientCtx *)me->pr_Task.tc_UserData;

    struct Library *sb = IExec->OpenLibrary("bsdsocket.library", 4);
    struct SocketIFace *si = NULL;
    if (sb) {
        si = (struct SocketIFace *)IExec->GetInterface(sb, "main", 1, NULL);
    }

    if (si && ctx) {
        LONG sock = si->ObtainSocket(ctx->sock_id, AF_INET, SOCK_STREAM, 0);
        if (sock >= 0) {
            IDOS->Printf("SerialShell: handler %p serving client\n", me);
            handle_client(si, sock);
            IDOS->Printf("SerialShell: handler %p done\n", me);
        } else {
            IDOS->Printf("SerialShell: child ObtainSocket(%ld) failed\n",
                         (long)ctx->sock_id);
        }
    } else {
        IDOS->Printf("SerialShell: child failed to init (sb=%p ctx=%p)\n",
                     sb, ctx);
    }

    if (si) IExec->DropInterface((struct Interface *)si);
    if (sb) IExec->CloseLibrary(sb);

    if (ctx) IExec->FreeVec(ctx);

    IExec->Forbid();
    in_flight_children--;
    IExec->Permit();

    return 0;
}

/* ------------------------------------------------------------------ */
/* Listener                                                            */
/* ------------------------------------------------------------------ */

int main(int argc, char **argv)
{
    LONG listen_sock = -1;
    struct sockaddr_in addr;
    int optval = 1;

    (void)argc; (void)argv;

#ifdef SERIALSHELL_AMIUPDATE
    SetAmiUpdateENVVariable("SerialShell");
#endif

    IDOS->Printf("SerialShell " SERIALSHELL_VERSION
                 " (" SERIALSHELL_DATE ")"
                 ": Starting TCP listener on port %ld\n",
                 (long)LISTEN_PORT);

    /* Create listening socket */
    listen_sock = ISocket->socket(AF_INET, SOCK_STREAM, 0);
    if (listen_sock < 0) {
        IDOS->Printf("SerialShell: socket() failed\n");
        return 20;
    }

    /* Allow address reuse */
    ISocket->setsockopt(listen_sock, SOL_SOCKET, SO_REUSEADDR,
                        &optval, sizeof(optval));

    /* Bind to all interfaces */
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(LISTEN_PORT);
    addr.sin_addr.s_addr = INADDR_ANY;

    if (ISocket->bind(listen_sock, (struct sockaddr *)&addr,
                      sizeof(addr)) < 0) {
        IDOS->Printf("SerialShell: bind() failed\n");
        ISocket->CloseSocket(listen_sock);
        return 20;
    }

    /* Listen backlog sized well above MAX_CHILDREN so burst-connect
     * storms get queued rather than ECONNREFUSED'd before the parent
     * can accept. Roadshow caps silently at SOMAXCONN. */
    if (ISocket->listen(listen_sock, 32) < 0) {
        IDOS->Printf("SerialShell: listen() failed\n");
        ISocket->CloseSocket(listen_sock);
        return 20;
    }

    IDOS->Printf("SerialShell: Listening on port %ld (CTRL-C to stop,"
                 " up to %d concurrent clients)\n",
                 (long)LISTEN_PORT, MAX_CHILDREN);

    /* Accept loop — spawn a fresh handler process for each accepted
     * connection. WaitSelect() lets us block on both the listen socket
     * and SIGBREAKF_CTRL_C so a `Break <cli> C` cleanly shuts us down. */
    for (;;) {
        fd_set rfds;
        ULONG sigs = SIGBREAKF_CTRL_C;

        FD_ZERO(&rfds);
        FD_SET(listen_sock, &rfds);

        LONG sel = ISocket->WaitSelect(listen_sock + 1, &rfds, NULL, NULL,
                                       NULL, &sigs);
        if (sel < 0) {
            IDOS->Printf("SerialShell: WaitSelect() failed\n");
            IDOS->Delay(50);
            continue;
        }
        if (sigs & SIGBREAKF_CTRL_C) {
            IDOS->Printf("SerialShell: CTRL-C received, shutting down\n");
            break;
        }
        if (!FD_ISSET(listen_sock, &rfds))
            continue;

        struct sockaddr_in client_addr;
        socklen_t client_len = sizeof(client_addr);

        LONG client_sock = ISocket->accept(listen_sock,
                                           (struct sockaddr *)&client_addr,
                                           &client_len);
        if (client_sock < 0) {
            IDOS->Printf("SerialShell: accept() failed\n");
            IDOS->Delay(50);
            continue;
        }

        IDOS->Printf("SerialShell: Client connected\n");

        /* Check the child budget. At cap, reject with BUSY_MSG. */
        IExec->Forbid();
        int busy = (in_flight_children >= MAX_CHILDREN);
        if (!busy) in_flight_children++;
        IExec->Permit();

        if (busy) {
            IDOS->Printf("SerialShell: at capacity (%d), rejecting\n",
                         MAX_CHILDREN);
            ISocket->send(client_sock, (APTR)BUSY_MSG,
                          strlen(BUSY_MSG), 0);
            ISocket->CloseSocket(client_sock);
            continue;
        }

        /* Release the socket from the parent's fd table so the handler
         * can ObtainSocket it into its own. UNIQUE_ID (-1) asks
         * bsdsocket to allocate a fresh id. */
        LONG sock_id = ISocket->ReleaseSocket(client_sock, (LONG)-1);
        if (sock_id == -1) {
            IDOS->Printf("SerialShell: ReleaseSocket failed — "
                         "serving inline\n");
            handle_client(ISocket, client_sock);
            IExec->Forbid(); in_flight_children--; IExec->Permit();
            continue;
        }

        struct ClientCtx *ctx = IExec->AllocVecTags(
            sizeof(*ctx),
            AVT_Type, MEMF_SHARED,
            TAG_END);
        if (!ctx) {
            LONG sock_back = ISocket->ObtainSocket(sock_id, AF_INET,
                                                    SOCK_STREAM, 0);
            if (sock_back >= 0) handle_client(ISocket, sock_back);
            IExec->Forbid(); in_flight_children--; IExec->Permit();
            continue;
        }
        ctx->sock_id = sock_id;

        /* Give the handler its own explicit NIL: I/O streams. Without
         * these, the child inherits the listener's Input/Output from
         * `Run >NIL:`, and SystemTags-ed shell commands (copy, delete)
         * end up blocking on a broken handle chain. NP_CloseInput/Output
         * TRUE makes the handler close them on exit. */
        BPTR child_in  = IDOS->Open("NIL:", MODE_OLDFILE);
        BPTR child_out = IDOS->Open("NIL:", MODE_NEWFILE);
        BPTR child_err = IDOS->Open("NIL:", MODE_NEWFILE);
        struct Process *child = IDOS->CreateNewProcTags(
            NP_Entry,       client_proc_entry,
            NP_Name,        (APTR)"SerialShell.client",
            NP_StackSize,   CLIENT_STACK_SIZE,
            NP_Child,       TRUE,
            NP_UserData,    ctx,
            NP_Input,       child_in,
            NP_Output,      child_out,
            NP_Error,       child_err,
            NP_CloseInput,  TRUE,
            NP_CloseOutput, TRUE,
            NP_CloseError,  TRUE,
            TAG_END);

        if (!child) {
            /* Spawn failed: reclaim the socket into this task so we
             * can at least service the client inline, free the ctx,
             * release the budget slot. */
            IDOS->Printf("SerialShell: CreateNewProcTags failed — "
                         "serving inline\n");
            if (child_in)  IDOS->Close(child_in);
            if (child_out) IDOS->Close(child_out);
            if (child_err) IDOS->Close(child_err);
            LONG sock_back = ISocket->ObtainSocket(sock_id, AF_INET,
                                                    SOCK_STREAM, 0);
            IExec->FreeVec(ctx);
            if (sock_back >= 0) handle_client(ISocket, sock_back);
            IExec->Forbid(); in_flight_children--; IExec->Permit();
            continue;
        }

        /* Parent immediately loops back to WaitSelect. The child owns
         * ctx + the socket; it'll FreeVec/CloseSocket/decrement the
         * counter in client_proc_entry. */
    }

    ISocket->CloseSocket(listen_sock);

    /* Best-effort: wait briefly for in-flight handlers to drain so we
     * don't exit with orphan children still writing to log. If they
     * don't drain in time, we exit anyway — AOS4 will clean them up
     * when their sockets eventually close. */
    for (int i = 0; i < 20 && in_flight_children > 0; i++) {
        IDOS->Delay(50);  /* 1s */
    }
    if (in_flight_children > 0) {
        IDOS->Printf("SerialShell: exiting with %d handler(s) still running\n",
                     in_flight_children);
    }
    return 0;
}
