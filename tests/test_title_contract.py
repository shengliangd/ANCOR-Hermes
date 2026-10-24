import pytest

from ancor.store import Store


def test_title_is_required_single_line_and_copied_on_confirmation(tmp_path):
    store = Store(tmp_path / 'store.db')
    create = Store.create_draft.__wrapped__

    with pytest.raises(TypeError):
        create(store, 'brief')
    for title in ('  ', 'two\nlines', 'x' * 81):
        with pytest.raises(ValueError):
            create(store, 'brief', title=title)

    draft = create(store, 'brief', title='  Stable label  ')
    assert draft['title'] == 'Stable label'
    requirement = store.confirm(draft['id'], 'human:title')
    assert requirement['title'] == 'Stable label'
