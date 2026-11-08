import os

DEBUG: bool = os.environ.get(key="TUSSILAGO_DEBUG", default="0") == "1"
