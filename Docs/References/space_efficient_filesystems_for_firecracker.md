# Space Efficient Filesystems for Firecracker

[2023-08-12 by Parandrus](https://parandrus.dev/devicemapper/)

Using device mapper we can use a layered approach to share common filesystem parts across VMs and provide a copy-on-write layer for each VM.

## Why Firecracker?

At work I have been building a [Firecracker](https://firecracker-microvm.github.io/) based orchestrator to run customer code pulled straight from git, aka untrusted code. It is basically a CI/CD system for a specific type of workloads. These workloads maintain a lot of internal state that is not easily serializable and typically have a fairly long cold start time.

Initially these workloads were running as Kubernetes pods, which gave us isolation[1](https://parandrus.dev/devicemapper/#fn-1), but to ensure they could respond quickly when required, we left the pods running in an idle state for a long time, even when nothing was actively happening. This worked well enough, but was not very cost efficient as CPU and memory remained tied up by idle workloads.

Moving to Firecracker helps us in two ways: it provides strong security and isolation, and also allows us to hibernate and resume VMs. As soon as a workload becomes idle it can be hibernated, with its memory stored in a snapshot on disk, and then resumed on demand within milliseconds when it needs to handle an incoming request for work.

## Disk Storage for Firecracker

Hibernated workloads consume no memory or CPU, but still require disk space for their memory snapshot and their filesystem. The memory snapshot contains the memory of the VM[2](https://parandrus.dev/devicemapper/#fn-2). Each VM also needs its own filesystem containing a complete operating system and the applications running in it. Unlike with containers this is a filesystem with a pre-defined capacity - if you want your VM to have 20 GB of storage, you need to pre-allocate and format a 20 GB filesystem. This storage adds up quickly, especially if you keep hibernated VMs around for a long time. During times of low activity disk space becomes our limiting factor in scaling down the nodes in our clusters.

## Reducing Storage Requirements with Layers

While memory snapshots are unique to each VM[3](https://parandrus.dev/devicemapper/#fn-3), a large part of the filesystems are identical for all VMs and never change. Linux container runtimes such as Docker use layers[4](https://parandrus.dev/devicemapper/#fn-4) to share parts of the filesystem across containers, with a copy-on-write layer on top of the shared layers for each individual container. Leveraging [device mapper](https://www.kernel.org/doc/html/latest/admin-guide/device-mapper/index.html) we can implement something similar for our Firecracker VMs. Additionally we can virtually expand the filesystem so that we don’t have allocate free space ahead of time.

We achieve all of this using [linear](https://docs.kernel.org/admin-guide/device-mapper/linear.html), [zero](https://docs.kernel.org/admin-guide/device-mapper/zero.html) and [snapshot](https://docs.kernel.org/admin-guide/device-mapper/snapshot.html) device mapper targets arranged in two layers. We will also use [loop devices](https://man7.org/linux/man-pages/man8/losetup.8.html) to create devices from files on the disk of our host machine.

[![Overlay and base device mapper devices forming our two layers](https://parandrus.dev/static/fc81ed25ce4223ad66b0ef5c3b8f8acd/c5bb3/dm-layers.png)](https://parandrus.dev/static/fc81ed25ce4223ad66b0ef5c3b8f8acd/de766/dm-layers.png)_Overlay and base device mapper devices forming our two layers._

## The First Layer: Shared OS and application code

First we create a loop device `BASE_LOOP` pointing to our shared filesystem, which is the filesystem that contains the operating system, as well as our shared application code, and a second loop device `OVERLAY_LOOP` for the copy-on-write overlay. The base loop device is read only.

```bash
ID=vm-1
BASE_LOOP=$(losetup --find --show --read-only /opt/rootfs/main-0-g230da2f.ext4)
OVERLAY_FILE=/opt/overlays/overlay-${ID}
touch $OVERLAY_FILE
truncate --size=5368709120 $OVERLAY_FILE
OVERLAY_LOOP=$(losetup --find --show $OVERLAY_FILE)
```

By using [truncate](https://man7.org/linux/man-pages/man2/truncate.2.html) we create a _sparse_ file. In the example below, `stat` reports a size of 5GB, but no blocks are allocated yet on disk:

```shell
touch overlay-vm-1
truncate -s 5368709120 overlay-vm-1
stat -f "blocks: %b, size: %z" overlay-vm-1
blocks: 0, size: 5368709120
```

We’ll need the sizes of the base and overlay devices:

```bash
BASE_SZ=$(blockdev --getsz $BASE_LOOP)
OVERLAY_SZ=$(blockdev --getsz $OVERLAY_LOOP)
```

We can now create our first device mapper device by using `dmsetup`:

```bash
dmsetup create base-$ID <<EOF
0 $BASE_SZ linear $BASE_LOOP 0
$BASE_SZ $OVERLAY_SZ zero
EOF
```

Here we tell Device mapper to map the range from `0` to `BASE_SZ` bytes to the base loop device using a `linear` target, meaning bytes map linearly from the new device to the base loop device. A read for byte 123 gets mapped to byte 123 in the base loop device. For the range from `BASE_SZ` to `OVERLAY_SZ` we setup a `zero` target. Any reads in this range return a 0, and writes are discarded. This ensures that the first layer has a total size of `OVERLAY_SZ`.

We now have something like this:

[![The base layer built from the linear and zero targets. Reads to the bytes in the range of the linear target go to the underlying loop device.](https://parandrus.dev/static/4689843b3a87b7517ed079d16cc1ddb7/c5bb3/dm-layer-1.png)](https://parandrus.dev/static/4689843b3a87b7517ed079d16cc1ddb7/776d3/dm-layer-1.png)_The base layer in detail._

## The Second Layer: Copy-on-Write

Next we create the overlay layer:

```bash
dmsetup create overlay-$ID <<EOF
0 $OVERLAY_SZ snapshot /dev/mapper/base-$ID $OVERLAY_LOOP P 8
EOF
```

This maps the range from `0` to `OVERLAY_SZ`, which is the entire disk size, using a `snapshot` target. The first argument `/dev/mapper/base-$ID` is the snapshot origin and the 2nd argument `$OVERLAY_LOOP` is the copy-on-write device. Reads first go to the copy-on-write device and fall through to the origin if nothing is present. Writes always go to the copy-on-write device.

[![The complete setup. Writes go to the copy-on-write layer or fall through to the base layer](https://parandrus.dev/static/fafb3b8902b3be656a7b275fad00f9ca/c5bb3/dm-layers-2.png)](https://parandrus.dev/static/fafb3b8902b3be656a7b275fad00f9ca/89557/dm-layers-2.png)_The complete setup. Writes go to the copy-on-write layer or fall through to the base layer._

## Conclusion and a Possible Improvement

We can now point Firecacker to `/dev/mapper/overlay-${ID}` and start our VM! Our setup ensures that we only use disk space on the host machine for data actually written within each VM. All the space used for the operating system itself and our application code is shared across all VMs.

Additionally we do not have to pre-allocate disk space for the free space available within each VM. However this is only true for disk space that has never been used. If blocks are written in the copy-on-write layer and then later deleted, the blocks remain allocated. The sparse file we created for the device loop used by the `snapshot` target of the copy-on-write layer does not shrink: If you create a 1GB file inside the VM, and then delete it, the backing file will still show that 1GB growth in size. Maybe it is possible to recreate the “holes” in the sparse file again?

1. [gVisor](https://gvisor.dev/) helps with that by providing a strong sandbox for pods beyond cgroups, namespaces, et al. [↩](https://parandrus.dev/devicemapper/#fnref-1)
2. [This post](https://codesandbox.io/blog/how-we-clone-a-running-vm-in-2-seconds) on the CodeSandbox blog goes into optimizations around memory snapshots [↩](https://parandrus.dev/devicemapper/#fnref-2)
3. Unless you do fun things like [cloning running VMs](https://codesandbox.io/blog/cloning-microvms-using-userfaultfd) [↩](https://parandrus.dev/devicemapper/#fnref-3)
4. via [OverlayFS](https://en.wikipedia.org/wiki/OverlayFS) [↩](https://parandrus.dev/devicemapper/#fnref-4)
