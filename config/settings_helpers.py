from __future__ import annotations

import logging
import os
from pathlib import Path

from platformdirs import user_data_path

logger: logging.Logger = logging.getLogger("tussilago.settings")


def get_data_dir() -> Path:
    """Get the data directory for the application.

    Seeks the environment variable `TUSSILAGO_DATA_DIR` first.
        If not set, it falls back to the default data directory provided by platformdirs.

    Returns:
        Path: The path to the data directory.
    """
    if os.environ.get("TUSSILAGO_DATA_DIR"):
        data_dir: Path = Path(str(os.environ.get("TUSSILAGO_DATA_DIR")))
        logger.info("Using data directory from environment variable: %s", data_dir)
    else:
        data_dir: Path = user_data_path(
            appname="tussilago",
            appauthor=False,
            roaming=True,
        )
        logger.info("Using default data directory: %s", data_dir)

    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir
