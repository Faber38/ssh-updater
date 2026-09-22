"""Bounded SSH process reads. Closing a channel never proves remote termination."""
import asyncio
import codecs
from dataclasses import dataclass

READ_SIZE = 8192
CAPTURE_LIMIT = 4 * 1024 * 1024
STREAM_LIMIT = 16 * 1024 * 1024
STREAM_TIMEOUT = 3600
IDLE_TIMEOUT = 300


class RemoteWaitError(OSError):
    pass


class RemoteTimeoutError(RemoteWaitError):
    pass


@dataclass(frozen=True)
class CommandExit:
    status: int


async def output(conn, command, *, timeout, limit, idle_timeout=IDLE_TIMEOUT):
    """Yield (stdout/stderr, text), then a typed exit result; bound both streams.

    No terminate/kill signal and no PTY are used. On interruption we only close
    the channel; the remote command may continue or react to the disconnect.
    """
    proc = None
    readers = []
    queue = asyncio.Queue(maxsize=8)

    async def read(stream, name):
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        try:
            while data := await stream.read(READ_SIZE):
                await queue.put((name, decoder.decode(data), len(data)))
            tail = decoder.decode(b'', final=True)
            if tail:
                await queue.put((name, tail, 0))
        except Exception as exc:
            await queue.put(exc)
        finally:
            # On cancellation the consumer no longer needs an EOF marker.
            if not asyncio.current_task().cancelling():
                await queue.put((name, None, 0))

    try:
        async with asyncio.timeout(timeout):
            try:
                # Keep deadlines in this task. Python 3.11 wait_for() can
                # swallow outer cancellation when its child just completed.
                async with asyncio.timeout(idle_timeout):
                    proc = await conn.create_process(
                        command, encoding=None, window=65536, request_pty=False)
            except TimeoutError as exc:
                raise RemoteTimeoutError('Zeitlimit bei der Remote-Prozessanforderung.') from exc
            readers = [asyncio.create_task(read(proc.stdout, 'stdout')),
                       asyncio.create_task(read(proc.stderr, 'stderr'))]
            done = total = 0
            while done < 2:
                try:
                    async with asyncio.timeout(idle_timeout):
                        item = await queue.get()
                except TimeoutError as exc:
                    raise RemoteTimeoutError('Keine Remote-Ausgabe innerhalb des Zeitlimits.') from exc
                if isinstance(item, Exception):
                    raise item
                name, text, size = item
                if text is None:
                    done += 1
                    continue
                total += size
                if total > limit:
                    raise RemoteWaitError('Remote-Ausgabelimit überschritten.')
                yield name, text
            try:
                async with asyncio.timeout(idle_timeout):
                    await proc.wait_closed()
            except TimeoutError as exc:
                raise RemoteTimeoutError('Zeitlimit beim Warten auf den Remote-Kanalschluss.') from exc
            if proc.exit_status is None or proc.exit_status < 0:
                raise RemoteWaitError('Kein bestätigter Remote-Exitcode empfangen.')
            yield CommandExit(proc.exit_status)
    except TimeoutError as exc:
        raise RemoteTimeoutError('Zeitüberschreitung beim lokalen Warten.') from exc
    finally:
        for task in readers:
            task.cancel()
        if readers:
            await asyncio.gather(*readers, return_exceptions=True)
        if proc is not None:
            proc.close()


async def capture(conn, command, timeout):
    parts = {'stdout': [], 'stderr': []}
    status = None
    async for event in output(conn, command, timeout=timeout, limit=CAPTURE_LIMIT):
        if isinstance(event, CommandExit):
            status = event.status
        else:
            name, text = event
            parts[name].append(text)
    if status is None:
        raise RemoteWaitError('Kein bestätigter Remote-Exitcode empfangen.')
    return status, ''.join(parts['stdout']), ''.join(parts['stderr'])


async def stream(conn, command, timeout=STREAM_TIMEOUT):
    pending = ''
    async for event in output(conn, command, timeout=timeout or STREAM_TIMEOUT, limit=STREAM_LIMIT):
        if isinstance(event, CommandExit):
            if pending:
                yield pending
            yield event
        else:
            pending += event[1]
            while pending:
                end = pending.find('\n', 0, 4096)
                if end >= 0:
                    yield pending[:end]
                    pending = pending[end + 1:]
                elif len(pending) >= 4096:
                    yield pending[:4096]
                    pending = pending[4096:]
                else:
                    break
