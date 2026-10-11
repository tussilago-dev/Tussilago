# Igniter

Igniter is the orchestrator for managing and deploying microVMs for Tussilago.

## Dependencies

- [extra/squashfs-tools](https://archlinux.org/packages/extra/x86_64/squashfs-tools/)

### Debugging as root

```bash
sudo uv run python -Xfrozen_modules=off -m debugpy --listen 127.0.0.1:5678 --wait-for-client -m litestar --app igniter.app:app run --debug
```

Attach your debugger to `127.0.0.1:5678` or Run And Debug with "Attach to Igniter" in VSCode.
