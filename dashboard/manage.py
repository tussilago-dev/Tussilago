#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""

from __future__ import annotations

import os
import sys


def main() -> None:
    """Run administrative tasks.

    Raises:
        ImportError: If Django is not installed or not available on the PYTHONPATH.
    """
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        from django.core.management import execute_from_command_line  # ruff: ignore[import-outside-top-level]
    except ImportError as exc:
        msg = "Couldn't import Django. Run 'uv run python manage.py' instead."
        raise ImportError(msg) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
