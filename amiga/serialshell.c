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
 * Protocol:
 *   - Host connects to TCP port 4321
 *   - Server sends "SERIALSHELL_READY\n"
 *   - Host sends a line terminated by \n:
 *     - Regular command: executed synchronously, output + "___SERIALSHELL_DONE___\n"
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
 * Listener shutdown: SIGBREAKF_CTRL_C to the server task (e.g. `Break <cli> C`)
 * breaks the accept loop cleanly and closes the listen socket.
 */

#include <proto/exec.h>
#include <proto/dos.h>
#include <proto/bsdsocket.h>

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
#define SERIALSHELL_VERSION "1.2"
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
#define QUIT_CMD      "SERIALSHELL_QUIT"
#define UPLOAD_CMD    "SERIALSHELL_UPLOAD "
#define DOWNLOAD_CMD  "SERIALSHELL_DOWNLOAD "
#define RUNCONSOLE_CMD "SERIALSHELL_RUNCONSOLE "
#define DONE_MARKER   "___SERIALSHELL_DONE___\n"
#define TEMP_OUTPUT   "T:serialshell_out.txt"
#define CONSOLE_OUTPUT "RAM:serialshell_console.txt"

/* Send a string over a socket */
static void send_str(LONG sock, const char *str)
{
    int len = strlen(str);
    while (len > 0) {
        int sent = ISocket->send(sock, (APTR)str, len, 0);
        if (sent <= 0) break;
        str += sent;
        len -= sent;
    }
}

/* Send exactly n bytes over a socket. Returns 0 on success, -1 on error. */
static int send_all(LONG sock, const void *data, int n)
{
    const char *p = (const char *)data;
    while (n > 0) {
        int sent = ISocket->send(sock, (APTR)p, n, 0);
        if (sent <= 0) return -1;
        p += sent;
        n -= sent;
    }
    return 0;
}

/* Receive exactly n bytes from a socket. Returns 0 on success, -1 on error. */
static int recv_all(LONG sock, void *data, int n)
{
    char *p = (char *)data;
    while (n > 0) {
        int got = ISocket->recv(sock, p, n, 0);
        if (got <= 0) return -1;
        p += got;
        n -= got;
    }
    return 0;
}

/* Send the contents of a file over a socket, then delete the file */
static void send_file(LONG sock, const char *path)
{
    BPTR fh = IDOS->Open(path, MODE_OLDFILE);
    if (fh) {
        char buf[SEND_BUFSIZE];
        int32 n;
        while ((n = IDOS->Read(fh, buf, sizeof(buf))) > 0) {
            if (send_all(sock, buf, n) < 0) {
                IDOS->Close(fh);
                IDOS->Delete(path);
                return;
            }
        }
        IDOS->Close(fh);
        IDOS->Delete(path);
    }
}

/* Read one line from socket (up to \n or buffer full).
 * Returns number of bytes read, 0 on disconnect, -1 on error. */
static int recv_line(LONG sock, char *buf, int bufsize)
{
    int pos = 0;
    while (pos < bufsize - 1) {
        char ch;
        int n = ISocket->recv(sock, &ch, 1, 0);
        if (n <= 0) return n;
        if (ch == '\n') break;
        if (ch == '\r') continue;
        buf[pos++] = ch;
    }
    buf[pos] = '\0';
    return pos;
}

/* Handle SERIALSHELL_UPLOAD <path> <size>
 * Receive <size> bytes from socket and write to <path> */
static void handle_upload(LONG sock, const char *args, char *buf)
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
        send_str(sock, "SERIALSHELL_UPLOAD_FAIL bad syntax\n");
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
        send_str(sock, "SERIALSHELL_UPLOAD_FAIL bad size\n");
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
        send_str(sock, resp);
        /* Must still drain the incoming bytes to keep protocol in sync */
        long remaining = size;
        while (remaining > 0) {
            int chunk = remaining > (long)XFER_BUFSIZE ? XFER_BUFSIZE : (int)remaining;
            if (recv_all(sock, buf, chunk) < 0) return;
            remaining -= chunk;
        }
        return;
    }

    /* Receive and write data using buffered FWrite */
    long remaining = size;
    int ok = 1;
    while (remaining > 0) {
        int chunk = remaining > (long)XFER_BUFSIZE ? XFER_BUFSIZE : (int)remaining;
        if (recv_all(sock, buf, chunk) < 0) {
            ok = 0;
            break;
        }
        if (IDOS->FWrite(fh, buf, 1, chunk) != (int32)chunk) {
            ok = 0;
            /* Drain remaining to keep protocol in sync */
            remaining -= chunk;
            while (remaining > 0) {
                int c = remaining > (long)XFER_BUFSIZE ? XFER_BUFSIZE : (int)remaining;
                if (recv_all(sock, buf, c) < 0) break;
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
        send_str(sock, "SERIALSHELL_UPLOAD_OK\n");
    } else {
        IDOS->Printf("SerialShell: Upload failed: %s\n", path);
        snprintf(resp, sizeof(resp),
                 "SERIALSHELL_UPLOAD_FAIL write error for %s\n", path);
        send_str(sock, resp);
    }
}

/* Handle SERIALSHELL_DOWNLOAD <path>
 * Send file size, then raw bytes, then DONE marker */
static void handle_download(LONG sock, const char *path, char *buf)
{
    char header[512];

    IDOS->Printf("SerialShell: Download %s\n", path);

    /* Get file size via ExamineObjectTags */
    struct ExamineData *exd = IDOS->ExamineObjectTags(
        EX_StringNameInput, path,
        TAG_END);
    if (!exd) {
        send_str(sock, "SERIALSHELL_FILE 0\n");
        send_str(sock, DONE_MARKER);
        return;
    }
    long filesize = (long)exd->FileSize;
    IDOS->FreeDosObject(DOS_EXAMINEDATA, exd);

    /* Open and send */
    BPTR fh = IDOS->Open(path, MODE_OLDFILE);
    if (!fh) {
        send_str(sock, "SERIALSHELL_FILE 0\n");
        send_str(sock, DONE_MARKER);
        return;
    }

    snprintf(header, sizeof(header), "SERIALSHELL_FILE %ld\n", filesize);
    send_str(sock, header);

    long sent_total = 0;
    int32 n;
    while ((n = IDOS->Read(fh, buf, XFER_BUFSIZE)) > 0) {
        if (send_all(sock, buf, n) < 0) {
            IDOS->Close(fh);
            return;
        }
        sent_total += n;
    }

    IDOS->Close(fh);

    IDOS->Printf("SerialShell: Download complete: %s (%ld bytes)\n",
                  path, sent_total);
    send_str(sock, DONE_MARKER);
}

static void handle_client(LONG client_sock)
{
    char cmdbuf[CMD_BUFSIZE];
    char execbuf[CMD_BUFSIZE + 256];

    /* Per-socket recv/send timeouts. Without these a silent client can
     * wedge the single-threaded listener indefinitely. On timeout the
     * underlying recv/send returns -1, which recv_all/recv_line/send_all
     * already treat as a fatal per-client error — the loop breaks,
     * socket is closed, and the listener resumes accepting. */
    struct timeval rcv_to = { .tv_sec = 30, .tv_usec = 0 };
    struct timeval snd_to = { .tv_sec = 30, .tv_usec = 0 };
    int keepalive = 1;
    ISocket->setsockopt(client_sock, SOL_SOCKET, SO_RCVTIMEO,
                        &rcv_to, sizeof(rcv_to));
    ISocket->setsockopt(client_sock, SOL_SOCKET, SO_SNDTIMEO,
                        &snd_to, sizeof(snd_to));
    ISocket->setsockopt(client_sock, SOL_SOCKET, SO_KEEPALIVE,
                        &keepalive, sizeof(keepalive));

    /* Allocate transfer buffer on heap to avoid stack overflow */
    char *xferbuf = malloc(XFER_BUFSIZE);
    if (!xferbuf) {
        IDOS->Printf("SerialShell: Out of memory for transfer buffer\n");
        ISocket->CloseSocket(client_sock);
        return;
    }

    send_str(client_sock, READY_MSG);

    for (;;) {
        int n = recv_line(client_sock, cmdbuf, CMD_BUFSIZE);
        if (n <= 0) break;  /* disconnect or error */

        /* Check for quit command */
        if (strcmp(cmdbuf, QUIT_CMD) == 0) {
            send_str(client_sock, "SERIALSHELL_SHUTDOWN\n");
            break;
        }

        /* Check for upload command */
        if (strncmp(cmdbuf, UPLOAD_CMD, strlen(UPLOAD_CMD)) == 0) {
            handle_upload(client_sock, cmdbuf + strlen(UPLOAD_CMD), xferbuf);
            continue;
        }

        /* Check for download command */
        if (strncmp(cmdbuf, DOWNLOAD_CMD, strlen(DOWNLOAD_CMD)) == 0) {
            handle_download(client_sock, cmdbuf + strlen(DOWNLOAD_CMD), xferbuf);
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

            IDOS->Delete(CONSOLE_OUTPUT);

            /* Build a script that runs the command with output redirected.
               The script runs inside a real shell (via Execute) which
               handles the >file redirect at the shell level. */
            BPTR scriptfh = IDOS->Open("T:serialshell_runcmd.sh", MODE_NEWFILE);
            if (scriptfh) {
                IDOS->FPrintf(scriptfh, "%s >%s\n", cmd, CONSOLE_OUTPUT);
                IDOS->Close(scriptfh);

                /* Run the script in its own console via SystemTags async.
                   The console provides real I/O for clib4 programs. */
                BPTR infh = IDOS->Open("NIL:", MODE_OLDFILE);
                BPTR outfh = IDOS->Open("NIL:", MODE_NEWFILE);
                IDOS->SystemTags(
                    "Execute T:serialshell_runcmd.sh",
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
                        EX_StringNameInput, CONSOLE_OUTPUT, TAG_END);
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

            send_file(client_sock, CONSOLE_OUTPUT);
            send_str(client_sock, DONE_MARKER);
            continue;
        }

        IDOS->Printf("SerialShell: Executing: %s\n", cmdbuf);

        /* Build a redirected command:
         *   cmd >T:serialshell_out.txt
         * Then read the output file and send it back.
         * NOTE: This uses synchronous SystemTags which blocks until
         * the command AND all its children exit.  For programs that
         * spawn persistent children (clib4 -athread=native), use
         * SERIALSHELL_RUNCONSOLE instead. */
        snprintf(execbuf, sizeof(execbuf),
                 "%s >%s", cmdbuf, TEMP_OUTPUT);

        /* Delete any stale output file */
        IDOS->Delete(TEMP_OUTPUT);

        /* Execute the command synchronously.
         * Per dos.doc: passing ZERO for SYS_Input and SYS_Output
         * causes the function to use "NIL:" internally (V53.65+).
         * NP_WindowPtr=-1 suppresses DOS requesters (e.g. "Please
         * insert volume FOO:") so a bad path fails the IO call
         * instead of hanging the shell waiting for user input. */
        IDOS->SystemTags(execbuf,
            SYS_Input,    ZERO,
            SYS_Output,   ZERO,
            NP_WindowPtr, (APTR)-1,
            TAG_END);

        /* Send output back over TCP */
        send_file(client_sock, TEMP_OUTPUT);

        /* Send end-of-output marker so host knows we're done */
        send_str(client_sock, DONE_MARKER);
    }

    free(xferbuf);
    ISocket->CloseSocket(client_sock);
}

int main(int argc, char **argv)
{
    LONG listen_sock = -1;
    struct sockaddr_in addr;
    int optval = 1;

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

    if (ISocket->listen(listen_sock, 2) < 0) {
        IDOS->Printf("SerialShell: listen() failed\n");
        ISocket->CloseSocket(listen_sock);
        return 20;
    }

    IDOS->Printf("SerialShell: Listening on port %ld (CTRL-C to stop)\n",
                 (long)LISTEN_PORT);

    /* Accept loop — handle one client at a time, then wait for next.
     * WaitSelect() lets us block on both the listen socket and
     * SIGBREAKF_CTRL_C, giving a clean shutdown path that the plain
     * accept() call (which ignores task signals) does not. */
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
            IDOS->Delay(50);  /* 1 second */
            continue;
        }

        IDOS->Printf("SerialShell: Client connected\n");
        handle_client(client_sock);
        IDOS->Printf("SerialShell: Client disconnected\n");
    }

    ISocket->CloseSocket(listen_sock);
    return 0;
}
