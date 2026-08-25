# Scaling Firecracker: Using OverlayFS to Save Disk Space

[2025-02-12 by Jonas Scholz](https://e2b.dev/blog/scaling-firecracker-using-overlayfs-to-save-disk-space)

If you want to run a lot of [Firecracker](https://firecracker-microvm.github.io/) instances like [E2B](https://e2b.dev/docs) does, you will quickly realize that copying the root filesystem for each instance is not the best idea. Even with small root filesystems like [Alpine Linux](https://www.alpinelinux.org/), you will probably have a few hundred megabytes of data to copy for each instance. Now multiply that with a few thousand instances and you will run into some serious space issues. Of course there is a Linux solution for this problem: OverlayFS. This blog post will explain OverlayFS and the copy-on-write (COW) technique and how to use it with Firecracker!

## OverlayFS and Copy-on-Write (COW)

[OverlayFS](https://docs.kernel.org/filesystems/overlayfs.html) is a Linux filesystem that lets you layer one filesystem on top of another, creating a merged view of both.

Imagine you have two directories: one that you can read from and write to (the upper directory), and another that you can only read from (the lower directory). The upper directory starts completely empty and will only contain files that are modified or added after mounting. When you combine these using OverlayFS, it creates a merged directory that looks like all the files from both directories are in one place. This is super handy because you don't need to copy all the files from the lower directory; you just reference them. This is what you see in the diagram, where files 'a.txt', 'b.txt', and 'c.txt' from both directories appear together in the merged directory, even though they might only exist in one of the directories.

And that's what we want to use with Firecracker! The lower directory is the read-only root filesystem (for example, the Alpine Linux root filesystem), and the upper directory is the writable layer that we can modify (for example, the user's code).

![A diagram illustrating an Overlay Filesystem, showing how files from a read-only lower filesystem and a read and write upper filesystem combine to form a single merged filesystem.](https://cdn.prod.website-files.com/6731db4b7372e95e7d18a926/67ad04be127a11f65baa4a84_aemvjraiwmejvrpia.png)

When you want to write or modify a file from the lower directory, OverlayFS uses a technique called [copy-on-write](https://en.wikipedia.org/wiki/Copy-on-write). Instead of changing the original file, it makes a copy of that file in the upper directory and applies the changes to this copy. So, if you modify 'b.txt' which comes from the lower directory, a copy of 'b.txt' is made in the upper directory where the changes are made, leaving the original in the lower directory untouched. This ensures the integrity of the base system and reduces the amount of data that needs to be copied.

Now that we understand the basics, let's see how we can use OverlayFS in Firecracker!

## How to Use OverlayFS in Firecracker

At this point I assume you already have a basic Firecracker setup running. If not, I'd recommend following the [Firecracker Init Lab](https://github.com/alexellis/firecracker-init-lab) or the [official getting started guide](https://github.com/firecracker-microvm/firecracker/blob/main/docs/getting-started.md) to get started.

### Step 1: Create read-only Root Filesystem

We'll start by creating a read-only root filesystem, which I assume you already have at `rootfs.ext4`.

First, create a new directory to modify the root filesystem and then mount the root filesystem to it:

```bash
mksquashfs /tmp/rootfs rootfs.ext4 -noappend
```

After mounting it we need to add a few new directories and most importantly, the `overlay-init`

```bash
umount /tmp/rootfs
rm -rf /tmp/rootfs
```

The overlay-init script is a simple script that sets up the overlay filesystem and then calls the actual init process. You can either build that yourself or use a [slightly modified version](https://github.com/njapke/overlayfs-in-firecracker/blob/main/overlay-init) from containerd:

```bash
#!/bin/sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may
# not use this file except in compliance with the License. A copy of the
# License is located at
#
#       http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is distributed
# on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.

# Parameters:
# 1. rw_root -- path where the read/write root is mounted
# 2. work_dir -- path to the overlay workdir (must be on same filesystem as rw_root)
# Overlay will be set up on /mnt, original root on /mnt/rom
pivot() {
    local rw_root work_dir
    rw_root="$1"
    work_dir="$2"
    /bin/mount \
 -o noatime,lowerdir=/,upperdir=${rw_root},workdir=${work_dir} \
 -t overlay "overlayfs:${rw_root}" /mnt
    pivot_root /mnt /mnt/rom
}

# Overlay is configured under /overlay
# Global variable $overlay_root is expected to be set to either:
# "ram", which configures a tmpfs as the rw overlay layer (this is
# the default, if the variable is unset)
# - or -
# A block device name, relative to /dev, in which case it is assumed
# to contain an ext4 filesystem suitable for use as a rw overlay
# layer. e.g. "vdb"
do_overlay() {
    local overlay_dir="/overlay"
    if [ "$overlay_root" = ram ] ||
           [ -z "$overlay_root" ]; then
        /bin/mount -t tmpfs -o noatime,mode=0755 tmpfs /overlay
    else
        /bin/mount -t ext4 "/dev/$overlay_root" /overlay
    fi
    mkdir -p /overlay/root /overlay/work
    pivot /overlay/root /overlay/work
}

# If we're given an overlay, ensure that it really exists. Panic if not.
if [ -n "$overlay_root" ] &&
       [ "$overlay_root" != ram ] &&
       [ ! -b "/dev/$overlay_root" ]; then
    echo -n "FATAL: "
    echo "Overlay root given as $overlay_root but /dev/$overlay_root does not exist"
    exit 1
fi

do_overlay

# invoke the actual system init program and proceed with the boot
# process.
exec /sbin/init $@
```

Theres not a lot of magic going on here, the overlay-init script just sets up the overlay filesystem and then calls the actual init process.

Once thats done, we are going to create a [Squashfs](https://docs.kernel.org/filesystems/squashfs.html) image of the rootfs directory. Squashfs is a compressed read-only filesystem that is very fast to load and can be mounted as a read-only filesystem.

```bash
mksquashfs /tmp/rootfs rootfs.ext4 -noappend
```

Finally, you can unmount the rootfs and remove the temporary directory:

```bash
umount /tmp/rootfsrm -rf /tmp/rootfs
```

### Step 2: Create Writable Overlay

Now if you want to persist the overlay filesystem you will have to create a new ext4 image. If you want a 5GB large filesystem, you can create it with the following command:

```javascript
dd if=/dev/zero of=overlay.ext4 bs=1M count=5120
```

The cool thing here is that this is a sparse file. If you run `ls -lh overlay.ext4` you will see that it says `5.0G`. Only if you run `du -h overlay.ext4` you will see that its actually 0 bytes big. Only when you add files to it, it will start taking up space!

Now we need to format the filesystem with ext4:

```bash
mkfs.ext4 overlay.ext4
```

That's it! You will have to repeat the step for your overlay filesystem for each instance, but not for the root filesystem. Next we can actually configure Firecracker to use the overlay filesystem.

### Step 3: Run Firecracker with OverlayFS

Configuring your Firecracker instance to use OverlayFS is pretty straightforward. Instead of only specifying the root drive, you're going to add the overlay drive as well.

The config for your first drive will look like this:

```json
{
  "drive_id": "rootfs",
  "path_on_host": "rootfs.img",
  "is_root_device": true,
  "partuuid": null,
  "is_read_only": true,
  "cache_type": "Unsafe",
  "rate_limiter": null
}
```

And your second drive will look like this:

```json
{
  "drive_id": "overlayfs",
  "path_on_host": "overlay.ext4",
  "is_root_device": false,
  "partuuid": null,
  "is_read_only": false,
  "cache_type": "Unsafe",
  "rate_limiter": null
}
```

Lastly, you need to tell your kernel to use the overlay-init and set the overlay\_root. For that, simply add

```bash
init=/sbin/overlay-init overlay_root=/vdb
```

to the kernel args. `vdb` is the identifier for your second drive (vda would be the first read-only root drive. If you have more than two drives it might be `vdc`, `vdd`, etc.). If you don't want to have persistent storage, you can use a ramdisk instead of a disk image with `overlay_root=ram`.

## Conclusion

Now, instead of copying a root filesystem of a few hundred megabytes for each instance, you can use OverlayFS to share the same root filesystem across multiple instances. This saves a lot of space and time, making it easier to manage a lot of instances, and especially useful for running a lot of small instances like **E2B** does.

## Sources

- [OverlayFS](https://en.wikipedia.org/wiki/OverlayFS)
- [Squashfs](https://docs.kernel.org/filesystems/squashfs.html)
- [Copy-on-write](https://en.wikipedia.org/wiki/Copy-on-write)
- [Firecracker with OverlayFS](https://github.com/firecracker-microvm/firecracker/discussions/3061)
- [OverlayFS in Firecracker](https://github.com/njapke/overlayfs-in-firecracker)
- [Firecracker Demo](https://github.com/firecracker-microvm/firecracker-demo)
