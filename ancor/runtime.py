"""Low-cost coordinator: callbacks own persistence; successful exits never ack events."""
import asyncio
import fcntl
import json
import os
import signal
import time
from pathlib import Path


class Coordinator:
    def __init__(self, lock_path, *, pending, snapshot, scan, batch_size=32,
                 command=None, clock=time.monotonic, retry_base=5, retry_max=300,
                 started=lambda: None):
        self.started = started
        if retry_base <= 0 or retry_max < retry_base:
            raise ValueError('invalid retry bounds')
        self.clock, self.retry_base, self.retry_max = clock, retry_base, retry_max
        self.next_attempt, self.failures, self.last_error = 0, 0, None
        if batch_size < 1:
            raise ValueError('batch_size must be positive')
        self.lock_path = Path(lock_path)
        self.pending, self.snapshot, self.scan = pending, snapshot, scan
        self.batch_size = batch_size
        self.command = command or (lambda prompt: ['hermes', '-p', 'ancor-leader', '--cli', 'chat', '-q', prompt])

    async def tick(self):
        """One wake/poll; callbacks are synchronous and called under the global lock.

        Retry state is process-local. Caller schedules ticks; no idle model calls.
        Ack belongs to the leader's explicit store tool, never exit-code inference.
        """
        if self.clock() < self.next_attempt:
            return 'backoff'
        try:
            result = await self._tick()
        except Exception as exc:
            self.failures += 1
            self.last_error = str(exc)
            self.next_attempt = self.clock() + min(self.retry_max, self.retry_base * 2 ** min(self.failures - 1, 20))
            return 'failed'
        if result == 'ran':
            self.failures, self.last_error = 0, None
            # Unacknowledged batches must not produce a tight successful-run loop.
            self.next_attempt = self.clock() + self.retry_base
        return result

    async def _tick(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 'busy'
            self.scan()
            events = list(self.pending(self.batch_size))[:self.batch_size]
            if not events:
                return 'idle'
            prompt = 'Process these ANCOR events; explicitly acknowledge only handled event IDs.\n' + json.dumps({'events': events, 'snapshot': self.snapshot()}, separators=(',', ':'), ensure_ascii=False)
            # flock follows the shared open-file description: a surviving leader
            # must retain ownership after a host crash. Close, never LOCK_UN, here.
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(*self.command(prompt), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, close_fds=True, pass_fds=(lock.fileno(),), start_new_session=True))
            try:
                proc = await asyncio.shield(spawn)
                await proc.wait()
            except asyncio.CancelledError:
                # Also cover cancellation while subprocess creation is in flight.
                # Keep the lock until reaping finishes, even on repeated cancels.
                async def reap():
                    child = await spawn
                    # Descendants share this session's process group. Kill it even
                    # if the leader has already exited, then reap our direct child.
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await child.wait()
                cleanup = asyncio.create_task(reap())
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                cleanup.result()
                raise
            if proc.returncode:
                raise RuntimeError(f'leader exited {proc.returncode}')
            # A successful command is the generic runtime's readiness fallback.
            # Production records readiness from the child's first business tool,
            # not this callback or subprocess creation.
            self.started()
            return 'ran'
