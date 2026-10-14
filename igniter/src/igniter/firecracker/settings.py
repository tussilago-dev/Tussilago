from typing import TYPE_CHECKING

from anyio import Path as AsyncPath
from platformdirs import site_bin_path
from platformdirs import site_cache_path
from platformdirs import site_data_path
from platformdirs import site_log_path

if TYPE_CHECKING:
    import pathlib
    from pathlib import Path

DATA_DIR: AsyncPath = AsyncPath("/var/lib/tussilago")
LOG_DIR: Path = site_log_path(appname="Tussilago", appauthor=False, ensure_exists=True)
JAILER_ROOT: AsyncPath = AsyncPath("/srv/jailer")
FIRECRACKER_PATH = "/usr/local/bin/firecracker"
JAILER_PATH = "/usr/local/bin/jailer"
VM_DATA_DIR: AsyncPath = DATA_DIR / "vms"
KERNEL_DIR: AsyncPath = DATA_DIR / "kernels"
CACHE_LOCATION: pathlib.Path = site_cache_path(appname="Tussilago", appauthor=False, ensure_exists=True)
SITE_DATA_LOCATION: pathlib.Path = site_data_path(appname="Tussilago", appauthor=False, ensure_exists=True)
INSTALL_DIR: Path = site_bin_path()
