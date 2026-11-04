from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import tempfile
from pathlib import Path

from anyio import Path as AsyncPath

logger: logging.Logger = logging.getLogger(__name__)


def require_command(command: str, label: str | None = None) -> str:
    """Return the path to a command if it exists, otherwise raise FileNotFoundError.

    Raises:
        FileNotFoundError: if the command is not found in PATH.
    """
    path: str | None = shutil.which(command)
    if path is None:
        msg: str = f"{label or command} is not installed or not in PATH"
        raise FileNotFoundError(msg)
    return path


async def _run(*args: str | Path | AsyncPath) -> None:
    command: list[str] = [str(arg) for arg in args]
    logger.debug("Running: %s", " ".join(command))

    await asyncio.to_thread(
        subprocess.run,
        command,
        check=True,
    )


def _export_container_sync(
    container_id: str,
    destination: str,
    docker_path: str,
    tar_path: str,
) -> None:
    logger.info("Exporting container %s to %s", container_id, destination)

    exporter: subprocess.Popen[bytes] = subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true]
        [docker_path, "export", container_id],
        stdout=subprocess.PIPE,
    )

    if exporter.stdout is None:
        exporter.kill()
        exporter.wait()
        msg: str = "Failed to capture stdout from docker export"
        raise RuntimeError(msg)

    extractor: subprocess.Popen[bytes] | None = None

    try:
        extractor = subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true]
            [
                tar_path,
                "--numeric-owner",
                "--same-owner",
                "--same-permissions",
                "--xattrs",
                "--acls",
                "-xpf",
                "-",
                "-C",
                destination,
            ],
            stdin=exporter.stdout,
        )

        exporter.stdout.close()

        extract_status: int = extractor.wait()
        export_status: int = exporter.wait()

    except BaseException:
        if extractor is not None and extractor.poll() is None:
            extractor.kill()

        if exporter.poll() is None:
            exporter.kill()

        if not exporter.stdout.closed:
            exporter.stdout.close()

        if extractor is not None:
            extractor.wait()

        exporter.wait()
        raise

    logger.debug("Container export finished (docker=%d, tar=%d)", export_status, extract_status)

    if extract_status != 0:
        logger.error("Failed to extract container %s", container_id)
        raise subprocess.CalledProcessError(extract_status, [tar_path, "-xpf", "-"])

    if export_status != 0:
        logger.error("Docker export failed for container %s", container_id)
        raise subprocess.CalledProcessError(
            export_status,
            [docker_path, "export", container_id],
        )


def _create_sparse_file(path: Path, size_bytes: int) -> None:
    with path.open("xb") as file:
        file.truncate(size_bytes)


async def build_base_rootfs(
    dockerfile: AsyncPath,
    output: AsyncPath,
    *,
    context: AsyncPath | None = None,
    size_bytes: int = 2 * 1024**3,
    image_tag: str = "tussilago/arch-rootfs:base",
    platform: str = "linux/amd64",
) -> AsyncPath:
    """Build a Dockerfile and turn its filesystem into a bootable ext4 image.

    Raises:
        FileExistsError: if the output file already exists.
        ValueError: if size_bytes is less than 512 MiB or not divisible by 4096.
        RuntimeError: if docker create returns an empty container ID.
    """
    docker_path: str = require_command("docker", "Docker")
    tar_path: str = require_command("tar")
    mkfs_path: str = require_command("mkfs.ext4")

    dockerfile = await dockerfile.resolve(strict=True)
    context = await (context or dockerfile.parent).resolve(strict=True)
    output = await output.resolve()

    if await output.exists():
        msg: str = f"Refusing to overwrite older base image: {output}"
        raise FileExistsError(msg)

    if size_bytes < 512 * 1024**2 or size_bytes % 4096:
        msg: str = "size_bytes must be at least 512 MiB and divisible by 4096"
        raise ValueError(msg)

    await output.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Building base rootfs from %s", dockerfile)

    await _run(
        docker_path,
        "build",
        "--pull",
        "--platform",
        platform,
        "--file",
        dockerfile,
        "--tag",
        image_tag,
        context,
    )

    logger.info("Docker image built: %s", image_tag)

    container_id: str = await asyncio.to_thread(
        subprocess.check_output,
        [docker_path, "create", "--platform", platform, image_tag],
        text=True,
    )
    container_id = container_id.strip()

    if not container_id:
        msg: str = "docker create returned an empty container ID"
        raise RuntimeError(msg)

    logger.debug("Created temporary container: %s", container_id)

    try:
        temp_dir = Path(
            await asyncio.to_thread(
                tempfile.mkdtemp,
                prefix="tussilago-rootfs-",
                dir=str(output.parent),
            ),
        )

        try:
            root_dir: Path = temp_dir / "root"
            root_dir.mkdir()  # ruff: ignore[blocking-path-method-in-async-function]
            image_file: Path = temp_dir / "rootfs.ext4"

            await asyncio.to_thread(
                _export_container_sync,
                container_id,
                str(root_dir),
                docker_path,
                tar_path,
            )

            logger.info("Creating base rootfs image (%d MiB)", size_bytes // 1024**2)

            await asyncio.to_thread(
                _create_sparse_file,
                image_file,
                size_bytes,
            )

            await _run(
                mkfs_path,
                "-F",
                "-m",
                "0",
                "-L",
                "tussilago-root",
                "-U",
                "random",
                "-E",
                "lazy_itable_init=0,lazy_journal_init=0",
                "-d",
                root_dir,
                image_file,
            )

            await AsyncPath(image_file).chmod(0o644)

            # Hard link the image file to the output path.
            await asyncio.to_thread(os.link, image_file, output)

            logger.info("Base rootfs created: %s", output)

        finally:
            await asyncio.to_thread(
                shutil.rmtree,
                temp_dir,
                ignore_errors=True,
            )

    finally:
        await asyncio.to_thread(
            subprocess.run,
            [docker_path, "rm", "--force", container_id],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    return output


async def create_vm_rootfs(
    base_image: AsyncPath,
    destination: AsyncPath,
    *,
    size_bytes: int | None = None,
) -> AsyncPath:
    """Create a private writable rootfs clone for one Firecracker VM.

    Uses reflinks where supported, falls back to a sparse file copy, assigns a
    unique ext4 UUID, and optionally grows the filesystem.

    Raises:
        FileExistsError: if the destination file already exists.
        ValueError: if size_bytes is less than the base image size or not divisible by 4096.
        RuntimeError: if resize2fs is required but not found.
    """
    cp_path: str = require_command("cp")
    tune2fs_path: str = require_command("tune2fs")

    base_image = await base_image.resolve(strict=True)
    destination = await destination.resolve()

    base_size: int = (await base_image.stat()).st_size
    target_size: int = size_bytes if size_bytes is not None else base_size

    if await destination.exists():
        raise FileExistsError(destination)

    if target_size < base_size:
        msg: str = "Shrinking rootfs images is not supported"
        raise ValueError(msg)

    if target_size % 4096:
        msg: str = "size_bytes must be divisible by 4096"
        raise ValueError(msg)

    resize2fs_path: str | None = None
    if target_size > base_size:
        resize2fs_path = require_command("resize2fs")

    await destination.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Cloning rootfs %s -> %s", base_image, destination)

    temp_dir = Path(
        await asyncio.to_thread(
            tempfile.mkdtemp,
            prefix="tussilago-vm-rootfs-",
            dir=str(destination.parent),
        ),
    )

    try:
        temporary_image: Path = temp_dir / "rootfs.ext4"

        await _run(
            cp_path,
            "--reflink=auto",
            "--sparse=always",
            "--",
            base_image,
            temporary_image,
        )

        logger.debug("Rootfs cloned (%d MiB)", base_size // 1024**2)

        if target_size > base_size:
            logger.info(
                "Growing rootfs to %d MiB",
                target_size // 1024**2,
            )

            await asyncio.to_thread(
                os.truncate,
                str(temporary_image),
                target_size,
            )

            if resize2fs_path is None:
                msg = "resize2fs_path is None despite target_size > base_size"
                raise RuntimeError(msg)

            await _run(resize2fs_path, temporary_image)

        # Cloned ext4 filesystems must not share a UUID.
        await _run(tune2fs_path, "-U", "random", temporary_image)

        logger.debug("Assigned new ext4 UUID to %s", temporary_image)

        await AsyncPath(temporary_image).chmod(0o600)

        # Publish only after the image is ready. Never overwrite another VM's image.
        await asyncio.to_thread(os.link, temporary_image, destination)

        logger.info("VM rootfs created: %s", destination)

    finally:
        await asyncio.to_thread(
            shutil.rmtree,
            temp_dir,
            ignore_errors=True,
        )

    return destination
