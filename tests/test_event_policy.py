from ancor.event_policy import (coalesce_native_events, is_initial_hold,
                                is_wake_event)


def event(identifier, kind, *, payload=None):
    return {'id': identifier, 'kind': kind, 'payload': payload or {}}


def test_heartbeat_backlog_collapses_to_one_cursor_watermark():
    rows = [event(identifier, 'heartbeat') for identifier in range(1, 151)]
    rows.append(event(151, 'completed'))
    rows.extend(event(identifier, 'heartbeat') for identifier in range(152, 250))

    selected = coalesce_native_events(rows, limit=100)

    assert [row['id'] for row in selected] == [151, 249]
    assert [row['kind'] for row in selected if is_wake_event(row)] == ['completed']


def test_actionable_events_are_not_hidden_by_a_heartbeat_window():
    rows = []
    for identifier in range(1, 202):
        rows.append(event(identifier, 'completed' if identifier % 2 else 'heartbeat'))

    selected = coalesce_native_events(rows, limit=100)

    assert len(selected) == 100
    assert all(row['kind'] == 'completed' for row in selected)
    assert selected[-1]['id'] == 199


def test_initial_hold_is_bookkeeping_but_a_real_block_wakes():
    hold = event(1, 'blocked', payload={
        'reason': 'initial_status', 'status': 'blocked'})
    blocked = event(2, 'blocked', payload={'reason': 'missing input'})

    assert is_initial_hold(hold)
    assert not is_wake_event(hold)
    assert is_wake_event(blocked)
    assert [row['id'] for row in coalesce_native_events([hold, blocked])] == [2]


def test_limit_must_be_positive():
    try:
        coalesce_native_events([], limit=0)
    except ValueError as exc:
        assert 'positive' in str(exc)
    else:
        raise AssertionError('non-positive limit accepted')
