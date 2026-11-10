"""Locate an explicitly configured Hermes checkout for optional native tests."""
import os
from pathlib import Path

import pytest


def native_source_or_skip():
    value = os.environ.get('ANCOR_HERMES_SOURCE') or os.environ.get('ANCOR_NATIVE_HERMES')
    if not value:
        pytest.skip('set ANCOR_HERMES_SOURCE to a compatible Hermes checkout')
    source = Path(value).expanduser()
    if not source.is_dir():
        pytest.skip(f'Hermes source unavailable: {source}')
    return source


def native_python_or_skip(source):
    value = os.environ.get('ANCOR_NATIVE_PYTHON')
    python = Path(value).expanduser() if value else source / '.venv/bin/python'
    if not python.is_file():
        pytest.skip('set ANCOR_NATIVE_PYTHON to the Hermes test interpreter')
    return python
