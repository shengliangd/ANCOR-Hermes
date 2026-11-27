"""Confirm that a native Hermes session has no executing or queued admissions."""
from __future__ import annotations

import asyncio
from pathlib import Path
import threading


async def stop_native_session(session_id: str, *, profile: str = 'ancor-exec', timeout: float = 12) -> bool:
    from hermes_cli.gateway_client import GatewayClientError, connect_gateway
    from hermes_constants import (get_default_hermes_root, set_hermes_home_override,
                                  reset_hermes_home_override)
    root = Path(get_default_hermes_root())
    home = root if profile == 'default' else root / 'profiles' / profile
    token = set_hermes_home_override(home)
    try:
        async with asyncio.timeout(timeout):
            async with connect_gateway() as client:
                while True:
                    try:
                        snapshot = await client.rpc('session.resume', session_id=session_id)
                    except GatewayClientError as exc:
                        if str(exc) == 'not_found':
                            try:
                                snapshot = await client.rpc('session.resume', title=session_id)
                            except GatewayClientError as titled_exc:
                                if str(titled_exc) == 'not_found':
                                    return True
                                raise
                        else:
                            raise
                    session_id = snapshot['stored_session_id']
                    pending = snapshot.get('pending', [])
                    if not snapshot.get('running') and not pending:
                        return True
                    for admission in pending:
                        if admission['status'] == 'unknown':
                            await client.rpc('prompt.resolve_unknown', session_id=session_id,
                                admission_id=admission['admission_id'],
                                execution_generation=admission['execution_generation'])
                        else:
                            await client.rpc('prompt.cancel', session_id=session_id,
                                             admission_id=admission['admission_id'])
                    if snapshot.get('running'):
                        try:
                            await client.rpc('session.interrupt', session_id=session_id,
                                             execution_generation=snapshot['execution_generation'])
                        except GatewayClientError as exc:
                            if str(exc) != 'stale_generation':
                                raise
                    await asyncio.sleep(.1)
    finally:
        reset_hermes_home_override(token)


def confirm_session_stopped(session_id: str, profile: str = 'ancor-exec') -> bool:
    """Usable from synchronous tool handlers, including on an asyncio thread.

    A fresh thread avoids changing the caller's Hermes runtime/profile scope.
    Failure retains the durable stop intent for the host to retry.
    """
    result = []
    def run():
        try:
            result.append(asyncio.run(stop_native_session(session_id, profile=profile)))
        except Exception:
            result.append(False)
    thread = threading.Thread(target=run, daemon=True, name='ancor-session-stop')
    thread.start()
    thread.join(15)
    return bool(result and result[0])
