import asyncio
import json
import os
import pytest

EVENT = {'id': 1, 'kind': 'test.input', 'payload': {}}


async def wait_file(path):
    for _ in range(300):
        if path.exists():
            return
        await asyncio.sleep(.01)
    raise AssertionError('child never started')


def test_serialization_and_cancellation_reaps_before_unlock(tmp_path):
    async def scenario():
        pid_file = tmp_path / 'pid'
        command = lambda p: [sys.executable, '-c', 'import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)', str(pid_file)]
        options = dict(pending=lambda n: [EVENT], snapshot=lambda: {}, scan=lambda: None, command=command)
        first = Coordinator(tmp_path / 'lock', **options)
        second = Coordinator(tmp_path / 'lock', **options)
        task = asyncio.create_task(first.tick())
        await wait_file(pid_file)
        pid = int(pid_file.read_text())
        assert await second.tick() == 'busy'
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            os.kill(pid, 9)
            raise AssertionError('child survived cancellation')
        second.pending = lambda n: []
        assert await second.tick() == 'idle'
    asyncio.run(scenario())
import sys

from ancor.runtime import Coordinator


def test_lock_excludes_another_process_opening_same_path(tmp_path):
    import subprocess
    lock = tmp_path / 'lock'
    probe = 'import fcntl,sys; f=open(sys.argv[1], "a"); fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB)'
    result_file = tmp_path / 'result'
    script = 'import subprocess,sys,pathlib; p=subprocess.run([sys.executable,"-c",sys.argv[1],sys.argv[2]],capture_output=True); pathlib.Path(sys.argv[3]).write_text(str(p.returncode))'
    runner = Coordinator(lock, pending=lambda n: [EVENT], snapshot=lambda: {}, scan=lambda: None, command=lambda p: [sys.executable, '-c', script, probe, str(lock), str(result_file)])
    assert asyncio.run(runner.tick()) == 'ran'
    assert result_file.read_text() != '0'
    assert subprocess.run([sys.executable, '-c', probe, str(lock)], capture_output=True).returncode == 0


def test_cancellation_stops_grandchild_before_unlock(tmp_path):
    from pathlib import Path

    def running(pid):
        try:
            return Path(f'/proc/{pid}/stat').read_text().split(') ')[1][0] != 'Z'
        except (FileNotFoundError, ProcessLookupError):
            return False

    async def scenario():
        pid_file = tmp_path / 'grandchild'
        script = ('import subprocess,sys,time,pathlib; '
                  'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); '
                  'pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)')
        runner = Coordinator(tmp_path / 'lock', pending=lambda n: [EVENT],
                             snapshot=lambda: {}, scan=lambda: None,
                             command=lambda p: [sys.executable, '-c', script, str(pid_file)])
        task = asyncio.create_task(runner.tick())
        pid = None
        try:
            await wait_file(pid_file)
            pid = int(pid_file.read_text())
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            for _ in range(300):
                if not running(pid):
                    break
                await asyncio.sleep(.01)
            assert not running(pid), 'grandchild survived cancellation'
            runner.pending = lambda n: []
            assert await runner.tick() == 'idle'
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            if pid is not None and running(pid):
                os.kill(pid, 9)

    asyncio.run(scenario())


def test_host_crash_keeps_lock_until_coordinator_exits(tmp_path):
    import subprocess
    import fcntl

    lock_path = tmp_path / 'lock'
    pid_file = tmp_path / 'coordinator'
    release = tmp_path / 'release'
    coordinator = ('import os,pathlib,sys,time; '
              'pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); '
              '\nwhile not pathlib.Path(sys.argv[2]).exists(): time.sleep(.01)')
    host_script = (
        'import asyncio,sys; from ancor.runtime import Coordinator; '
        'c=Coordinator(sys.argv[1], pending=lambda n:[{"id":1,"kind":"test.input","payload":{}}], '
        'snapshot=lambda:{}, scan=lambda:None, '
        'command=lambda p:[sys.executable,"-c",sys.argv[3],sys.argv[2],sys.argv[4]]); '
        'asyncio.run(c.tick())')
    host = subprocess.Popen([sys.executable, '-c', host_script, str(lock_path),
                             str(pid_file), coordinator, str(release)])
    try:
        asyncio.run(wait_file(pid_file))
        host.kill()
        host.wait(timeout=5)
        os.kill(int(pid_file.read_text()), 0)
        with lock_path.open('a') as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release.touch()
            async def wait_unlocked():
                for _ in range(300):
                    try:
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        return
                    except BlockingIOError:
                        await asyncio.sleep(.01)
                raise AssertionError('lock survived coordinator exit')
            asyncio.run(wait_unlocked())
    finally:
        release.touch()
        if host.poll() is None:
            host.kill()
        host.wait(timeout=5)


def test_retry_backoff_preserves_pending(tmp_path):
    now = [0.0]
    runner = Coordinator(tmp_path / 'lock', pending=lambda n: [EVENT], snapshot=lambda: {}, scan=lambda: None, clock=lambda: now[0], retry_base=5, command=lambda p: [sys.executable, '-c', 'raise SystemExit(7)'])
    assert asyncio.run(runner.tick()) == 'failed'
    assert runner.last_error == 'coordinator exited 7'
    assert asyncio.run(runner.tick()) == 'backoff'
    now[0] = 5
    assert asyncio.run(runner.tick()) == 'failed'
    now[0] = 10
    assert asyncio.run(runner.tick()) == 'backoff'


def test_pending_bounded_and_not_automatically_acknowledged(tmp_path):
    output = tmp_path / 'prompt.json'
    events = [EVENT, {'id': 2, 'kind': 'test.input', 'payload': {}}]
    limits = []
    def pending(n):
        limits.append(n)
        return events[:n]
    runner = Coordinator(tmp_path / 'coordinator.lock', pending=pending, snapshot=lambda: {'revision': 3}, scan=lambda: None, batch_size=1, command=lambda prompt: [sys.executable, '-c', 'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])', str(output), prompt])
    assert asyncio.run(runner.tick()) == 'ran'
    assert limits == [1]
    assert json.loads(output.read_text().split('\n', 1)[1]) == {
        'events': [{'id': 1, 'kind': 'test.input', 'payload_preview': '{}'}],
        'snapshot': {'revision': 3}}
    assert len(events) == 2
