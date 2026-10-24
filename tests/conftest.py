import pytest

from ancor.store import Store


@pytest.fixture(autouse=True)
def default_test_requirement_title(monkeypatch):
    """Keep legacy test fixtures concise; title-enforcement tests use __wrapped__."""
    original = Store.create_draft

    def create(store, brief, replacement_of=None, summary=None, *, title=None):
        return original(store, brief, replacement_of, summary,
                        title=title or 'Test requirement')

    create.__wrapped__ = original
    monkeypatch.setattr(Store, 'create_draft', create)
