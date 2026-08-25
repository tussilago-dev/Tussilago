# Firecracker automation for cloud images + initialization via cloud-init

[2020-12-29 by Alvaro Hernandez](https://ongres.com/blog/automation-to-run-vms-based-on-vanilla-cloud-images-on-firecracker/)

[src](https://gitlab.com/ongresinc/blog-posts-src/-/tree/master/202012-firecracker_cloud_image_automation?ref_type=heads)

Download Firecracker and set permissions for your user to access the `/dev/kvm` special device. See
[Getting Started with Firecracker](https://github.com/firecracker-microvm/firecracker/blob/master/docs/getting-started.md)
for more information.

```bash
sudo setfacl -m u:${USER}:rw /dev/kvm
[ $(stat -c "%G" /dev/kvm) = kvm ] && sudo usermod -aG kvm ${USER}
```

[01-setup\_host.sh](https://gitlab.com/ongresinc/blog-posts-src/-/blob/master/202012-firecracker_cloud_image_automation/01-setup_host.sh).
This script creates the host bridge where all VMs will be connected and creates the `iptables`
rules to NAT outgoing traffic from the VMs to Internet. Modify the `$EGRESS_IFACE` variable if the device would not be
correctly detected.

```bash
#!/bin/bash


source variables


sudo ip link add name $FIRECRACKER_BRIDGE type bridge
sudo ip addr add $VMS_NETWORK_PREFIX.1/24 dev $FIRECRACKER_BRIDGE
sudo ip link set dev $FIRECRACKER_BRIDGE up
sudo sysctl -w net.ipv4.ip_forward=1 > /dev/null
sudo iptables --table nat --append POSTROUTING --out-interface $EGRESS_IFACE -j MASQUERADE
sudo iptables --insert FORWARD --in-interface $FIRECRACKER_BRIDGE -j ACCEPT
```

[02-gen\_keypair.sh](https://gitlab.com/ongresinc/blog-posts-src/-/blob/master/202012-firecracker_cloud_image_automation/02-gen_keypair.sh).
Generates the SSH keypair. The private key will be kept on the host to SSH the VMs. The public
key will be inserted into the `authorized_keys` of the `fc` user in all VMs. Note that the more modern and
secure [EdDSA](https://medium.com/risan/upgrade-your-ssh-key-to-ed25519-c6e8d60d3c54) keys have been used.

```bash
#!/bin/bash

source variables

mkdir -p $KEYPAIR_DIR
ssh-keygen -t ed25519 -q -N "" -f $KEYPAIR_DIR/$DEFAULT_KP

```

[03-download\_generate\_image.sh](https://gitlab.com/ongresinc/blog-posts-src/-/blob/master/202012-firecracker_cloud_image_automation/03-download_generate_image.sh).
All the user configuration will be performed via cloud-init. However, because of how Firecracker works, cloud image as
downloaded cannot be used directly. First of all, Firecracker doesn’t use a disk image with partitions, but just the root filesystem.
Also, the kernel and initrd need to be provided separately (as Firecracker doesn’t support system emulation for
bootloaders). Finally, the kernel image cannot be compressed. This script just downloads a `.tar.xz`-ed cloud image,
copies it into an `ext4` filesystem backed by a file, downloads the initrd and kernel, and uncompresses the kernel.

```bash
#!/bin/bash

## This script downloads and generates a suitable ext4 image from existing cloud
## images. For simplicity it currently only downloads from Ubuntu images, but it
## should not be a big effort to adapt to other cloud images.

source variables

function download() {
 echo "Downloading $2..."

 curl -s -o $1 $2
}

function download_if_not_present() {
 [ -f $1 ] || download $1 $2
}

function generate_image() {
 echo "Generating $IMAGE_ROOTFS..."

 truncate -s $IMAGE_SIZE $IMAGE_ROOTFS
 mkfs.ext4 $IMAGE_ROOTFS > /dev/null 2>&1

 local tmppath=/tmp/.$RANDOM-$RANDOM
 mkdir $tmppath
 sudo mount $IMAGE_ROOTFS -o loop $tmppath
 sudo tar -xf images/$UBUNTU_VERSION/download/$image_tar --directory $tmppath
 sudo umount $tmppath
 rmdir $tmppath
}

function extract_vmlinux() {
 echo "Extracting vmlinux to $KERNEL_IMAGE..."

 local extract_linux=/tmp/.$RANDOM-$RANDOM
 curl -s -o $extract_linux https://raw.githubusercontent.com/torvalds/linux/master/scripts/extract-vmlinux
 chmod +x $extract_linux
 $extract_linux images/$UBUNTU_VERSION/download/$kernel > $KERNEL_IMAGE
 rm $extract_linux
}


# Download components
mkdir -p images/$UBUNTU_VERSION/download

image_tar=$UBUNTU_VERSION-server-cloudimg-amd64-root.tar.xz
download_if_not_present \
 images/$UBUNTU_VERSION/download/$image_tar \
 https://cloud-images.ubuntu.com/$UBUNTU_VERSION/current/$image_tar

kernel=$UBUNTU_VERSION-server-cloudimg-amd64-vmlinuz-generic
download_if_not_present \
 images/$UBUNTU_VERSION/download/$kernel \
 https://cloud-images.ubuntu.com/$UBUNTU_VERSION/current/unpacked/$kernel

initrd=$UBUNTU_VERSION-server-cloudimg-amd64-initrd-generic
download_if_not_present \
 images/$UBUNTU_VERSION/download/$initrd \
 https://cloud-images.ubuntu.com/$UBUNTU_VERSION/current/unpacked/$initrd


# Generate image, kernel and link initrd
[ -f $IMAGE_ROOTFS ] || generate_image

[ -f $INITRD ] || ln -s download/$initrd $INITRD

[ -f $KERNEL_IMAGE ] || extract_vmlinux
```

[04-launch\_vms.sh](https://gitlab.com/ongresinc/blog-posts-src/-/blob/master/202012-firecracker_cloud_image_automation/04-launch_vms.sh).
The main script, that launches N virtual machines. It launches one Firecracker process per VM, proceeds to configure the
VM and the metadata via Firecracker’s HTTP API, and finally boots into it. Note that several requests are needed to configure
all the elements of the VM, including: instance configuration (CPU, RAM), disk devices, net devices, boot source and
of course the metadata that will be used by cloud-init. All configuration used can be found in the
[conf/](https://gitlab.com/ongresinc/blog-posts-src/-/tree/master/202012-firecracker_cloud_image_automation/conf)
directory.

```bash
#!/bin/bash


source variables

function curl_args() {
 curl --unix-socket $socket \
  -H 'Accept: application/json' \
  -H 'Content-Type: application/json' \
  $*
}

function firecracker_http_file() {
 curl_args -X $1 'http://localhost/'$2 --data-binary "@"$3
}

function create_tap() {
 local device=$1

 ip addr show $device > /dev/null 2>&1
 if [ $? -ne 0 ]
 then
  sudo ip tuntap add dev $device mode tap
         sudo ip link set dev $device up
 fi
}

function create_vm_taps() {
 local tap_metadata=$1
 local tap_main=$2

 create_tap $tap_metadata
 create_tap $tap_main

 sudo ip link set $tap_main master $FIRECRACKER_BRIDGE
}

function script_exit() {
 echo -ne "\n\t$1\n\n" >&2
 exit 1
}


function launch_vm() {
 local instance_number=$1
 local tmpfile=/tmp/.$RANDOM-$RANDOM
 local socket=$FIRECRACKER_SOCKET.$instance_number
 local instance_id=`printf "id%05d%05d" $RANDOM $RANDOM`
 local log_file=/tmp/.$instance_id.log
 
 # Start firecracker daemon
 (
  rm -f $socket
  touch $log_file
  $FIRECRACKER --api-sock $socket --log-path $log_file --level Debug &> /dev/null &
  pid=$!
  mkdir -p $FIRECRACKER_PID_DIR
  echo $pid > $FIRECRACKER_PID_DIR/$pid
  echo "Started Firecracker with pid=$pid, logs: $log_file"
 )
 
 # Wait for API server to start
 while [ ! -e $socket ]; do
     sleep 0.1s
 done
 
 # VM config
 cat conf/firecracker/instance-config.json | \
  ./tmpl.sh __INSTANCE_VCPUS__ $VM_VCPUS | \
  ./tmpl.sh __INSTANCE_RAM_MB__ $(( $VM_RAM_GB * 1024 )) \
  > $tmpfile
 firecracker_http_file PUT 'machine-config' $tmpfile
 
 # Drives
 mkdir -p disks
 root_fs="./disks/"$( basename $IMAGE_ROOTFS).$instance_id
 cp $IMAGE_ROOTFS $root_fs
 cat conf/firecracker/drives.json | \
  ./tmpl.sh __ROOT_FS__ $root_fs \
  > $tmpfile
 firecracker_http_file PUT 'drives/rootfs' $tmpfile
 
 # Networking
 tap_number_base=$(( ($instance_number - 1) * 2 ))
 tap_metadata="tap"`printf "%02d" $tap_number_base`
 tap_main="tap"`printf "%02d" $(( $tap_number_base + 1 ))`
 create_vm_taps $tap_metadata $tap_main
 
 cat conf/firecracker/network_interfaces.eth0.json | \
  ./tmpl.sh __TAP_METADATA__ $tap_metadata \
  > $tmpfile
 
 firecracker_http_file PUT 'network-interfaces/eth0' $tmpfile
 
 mac_octet=`printf '%02x' $(( $instance_number + 1 ))`
 cat conf/firecracker/network_interfaces.eth1.json | \
  ./tmpl.sh __MAC_OCTET__ $mac_octet | \
  ./tmpl.sh __TAP_MAIN__ $tap_main \
  > $tmpfile
 firecracker_http_file PUT 'network-interfaces/eth1' $tmpfile
 
 # Boot configuration
 instance_ip=$VMS_NETWORK_PREFIX"."$(( $instance_number + 1 ))
 network_config_base64=$( \
  cat conf/cloud-init/network_config.yaml | \
  ./tmpl.sh __INSTANCE_IP__ $instance_ip | \
  ./tmpl.sh __MAC_OCTET__ $mac_octet | \
  ./tmpl.sh __GATEWAY__ $VMS_NETWORK_PREFIX".1" | \
  gzip --stdout - | \
  base64 -w 0
 )
 cat conf/firecracker/boot-source.json | \
  ./tmpl.sh __KERNEL_IMAGE__ $KERNEL_IMAGE | \
  ./tmpl.sh __INSTANCE_ID__ $instance_id | \
  ./tmpl.sh __NETWORK_CONFIG__ $network_config_base64 | \
  ./tmpl.sh __INITRD__ $INITRD \
  > $tmpfile
 firecracker_http_file PUT 'boot-source' $tmpfile
 
 # Metadata
 cat conf/cloud-init/meta-data.yaml | \
  ./tmpl.sh __INSTANCE_ID__ $instance_id | \
  ./tmpl.sh __HOSTNAME__ $instance_id | \
  jq --raw-input --slurp '{ "latest": { "meta-data": . }}' \
  > $tmpfile
 firecracker_http_file PUT 'mmds' $tmpfile
 
 # User data
 cat conf/cloud-init/user-data.yaml | \
  ./tmpl.sh __SSH_PUB_KEY__ "`cat $KEYPAIR_DIR/$DEFAULT_KP.pub`" | \
  jq --raw-input --slurp '{ "latest": { "user-data": . }}' \
  > $tmpfile
 firecracker_http_file PATCH 'mmds' $tmpfile
 
 # Cleanup
 rm $tmpfile
 
 # Start VM
 firecracker_http_file PUT 'actions' conf/firecracker/instance-start.json
 [ $? -eq 0 ] && echo "Instace $instance_id started. SSH with ssh -i $KEYPAIR_DIR/$DEFAULT_KP fc@$instance_ip"
 
}


# Main
for i in `seq 1 $NUMBER_VMS`
do
 (
  launch_vm $i
 )&
done
wait
```

Finally, some other scripts are available for cleanup. Please note that they will delete by default not only the
disks of the VMs, but also the downloaded images and even key pairs.

```bash
#!/bin/bash


source variables


for i in `find $FIRECRACKER_PID_DIR -type f`
do
 kill `cat $i`
 rm $i
done
```

```bash
#!/bin/bash


source variables


instance_count=0
for i in `seq 0 $(( $NUMBER_VMS - 1 ))`
do
 tap_metadata_id=`printf "%02d" $(( $i * 2 ))`
 tap_main_id=`printf "%02d" $(( $i * 2 + 1 ))`
 
 sudo ip link delete tap${tap_metadata_id}
 sudo ip link delete tap${tap_main_id}
done

sudo ip link delete $FIRECRACKER_BRIDGE

rm -rf disks
rm -rf images
rm -rf keypairs
[ -f ansible/inventories/eks/hosts.yaml ] && rm ansible/inventories/eks/hosts.yaml
```

Once you launch the VMs, you should get an output similar to:

```sh
$ ./04-launch_vms.sh
Started Firecracker with pid=63249, logs: /tmp/.id-14533-12478.log
Instace id-14533-12478 started. SSH with ssh -i keypairs/kp fc@172.26.0.2

Started Firecracker with pid=63561, logs: /tmp/.id-3522-29474.log
Instace id-3522-29474 started. SSH with ssh -i keypairs/kp fc@172.26.0.3

Started Firecracker with pid=63969, logs: /tmp/.id-3900-27804.log
Instace id-3900-27804 started. SSH with ssh -i keypairs/kp fc@172.26.0.4

Started Firecracker with pid=64435, logs: /tmp/.id-3526-18108.log
Instace id-3526-18108 started. SSH with ssh -i keypairs/kp fc@172.26.0.5
```

## Final remarks

During the process, I came up with some questions, mostly for the Firecracker and cloud-init communities. Feel free to
drop some comments if you have an opinion!

- Firecracker: it would be great to think of a “Firecracker” image format that could group the uncompressed (or
[bzImage](https://github.com/firecracker-microvm/firecracker/issues/175)) kernel image, the initrd (optional) and the
root filesystem (plus metadata about the image). Also it would be great if a tap device would not be necessary to
connect to the MMDS service.

- cloud-init. I was unable to configure the network via passing the network configuration as part of the metadata. I
believe this is not possible with `nocloud` datasource. While surprising, it still makes sense to do this even if using
`nocloud-net`, as a device to access the metadata could have been setup via kernel command line argument, and then expect to
configure the rest of the interfaces through the metadata service. You can do this with other datasources (e.g. `Ec2`).

**This post and the referenced source code should allow you to easily use Firecracker, and use a wide catalog of pre-built OS**
**images, without having to go through several and tedious steps**. Being these VMs so lightweight and fast to start, this
setup is convenient for quickly fire VMs on your local environment (i.e. laptop) for quick experimentation. Moreover,
Firecracker allows you to overprovision CPUs, so you can launch more VM cores than you really have.

Enjoy the ride!
