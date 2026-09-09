"""Zero-copy file transmission over a socket via ``os.sendfile``.

This backs the ASGI ``http.response.zerocopysend`` extension. anyio's socket
``send`` fully drains to the OS before returning - on asyncio because the stream
protocol sets ``set_write_buffer_limits(0)``, on trio because ``send_all`` writes
everything - so once the response headers have been sent it is safe to hand the
raw socket to ``os.sendfile`` without the file bytes racing ahead of buffered
header bytes.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import anyio
import anyio.to_thread

if TYPE_CHECKING:
    import socket
    from collections.abc import AsyncIterator

_READ_CHUNK = 64 * 1024

# A single sendfile call is capped so a huge file cannot monopolise the socket;
# the loop simply calls again for what is left.
_MAX_SENDFILE_CHUNK = 2**30

#: True when the platform can transmit a file descriptor with a zero-copy syscall.
have_sendfile = hasattr(os, "sendfile")


async def sendfile(sock: socket.socket, in_fd: int, offset: int | None, count: int | None) -> int:
    """Send bytes of ``in_fd`` to ``sock`` with ``os.sendfile``, returning the count sent.

    ``offset`` is where to start reading; ``None`` means the file's current position
    (read once here, since ``os.sendfile`` with an explicit offset does not advance the
    file). ``count`` is how many bytes to send; ``None`` means until end of file.

    The socket is non-blocking (anyio owns it), so ``EAGAIN`` is awaited on writability
    rather than blocking the event loop.
    """
    if offset is None:
        # os.sendfile with an explicit offset never advances the file position, so read
        # the current position and start there - matching "from the current position".
        offset = os.lseek(in_fd, 0, os.SEEK_CUR)

    sock_fd = sock.fileno()
    sent_total = 0
    while count is None or sent_total < count:
        to_send = (
            _MAX_SENDFILE_CHUNK if count is None else min(_MAX_SENDFILE_CHUNK, count - sent_total)
        )
        try:
            sent = os.sendfile(sock_fd, in_fd, offset, to_send)
        except (BlockingIOError, InterruptedError):
            await anyio.wait_writable(sock)
            continue
        if sent == 0:
            break  # End of file reached before count - the app under-declared its length.
        offset += sent
        sent_total += sent
    return sent_total


def _open_and_stat(path: str) -> tuple[int, int]:
    """Open ``path`` read-only and return its size.

    Runs in a worker thread: ``os.open`` walks the path and locates the inode, which
    can block on a cold page cache or a slow/network filesystem, and so must not run
    on the event loop. The returned descriptor is the caller's to close; the size is
    read from the already-open descriptor's in-memory inode metadata, which never
    touches disk.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        return fd, os.fstat(fd).st_size
    except BaseException:
        os.close(fd)
        raise


async def open_file(path: str) -> tuple[int, int]:
    """Open ``path`` read-only off the event loop, returning ``(fd, size)``.

    Backs the ``http.response.pathsend`` extension, whose ``os.open`` would otherwise
    block the event loop on the path lookup. The size is read here too so the caller
    can honor ``count`` defaults without a second syscall.
    """
    return await anyio.to_thread.run_sync(_open_and_stat, path)


def _pread(in_fd: int, count: int, offset: int) -> bytes:
    """Read ``count`` bytes of ``in_fd`` at ``offset`` without moving the file position.

    ``os.pread`` does this directly; where it is unavailable (Windows) a duplicated
    descriptor is seeked and read instead, so the caller's - which for zerocopysend is
    the application's - file position is left untouched.
    """
    if hasattr(os, "pread"):
        return os.pread(in_fd, count, offset)
    dup_fd = os.dup(in_fd)
    try:
        os.lseek(dup_fd, offset, os.SEEK_SET)
        return os.read(dup_fd, count)
    finally:
        os.close(dup_fd)


async def read_file_chunks(in_fd: int, offset: int, count: int) -> AsyncIterator[bytes]:
    """Yield ``count`` bytes of ``in_fd`` from ``offset`` in chunks, reading off-thread.

    The fallback for where ``os.sendfile`` cannot be used - an encrypted (TLS) stream,
    HTTP/2 and HTTP/3, or a platform without ``sendfile`` - so the same file is honoured
    by reading it and sending it through the normal path.
    """
    remaining = count
    position = offset
    while remaining > 0:
        data = await anyio.to_thread.run_sync(_pread, in_fd, min(_READ_CHUNK, remaining), position)
        if not data:
            break  # End of file reached before count.
        yield data
        position += len(data)
        remaining -= len(data)
