import os

from anyio import Path as AsyncPath

DATA_DIR: AsyncPath = AsyncPath("/var/lib/tussilago")
VM_DATA_DIR: AsyncPath = DATA_DIR / "vms"
KERNEL_DIR: AsyncPath = DATA_DIR / "kernels"
DEBUG: bool = os.environ.get("TUSSILAGO_DEBUG", "0") == "1"
