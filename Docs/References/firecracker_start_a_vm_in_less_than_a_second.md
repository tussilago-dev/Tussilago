# 2021-01-19: Trying to understand what a bridge is

Hello! Yesterday I spent a lot of time trying to understand what a bridge is so here are my notes. I’m only going to talk about bridges as applied to container / VM networking because that’s what I’m trying to do and also I’ve also never seen a bridge used for anything else.

Some things in this post are almost certainly wrong.

## why I’m doing this: trying to set up Firecracker networking

I’ve been setting up some VMs using AWS Firecracker (which is amazing and lets you start up VMs in like ONE SECOND, it’s extremely fast, and I will write about it in the future because I really love it). Yesterday morning I was trying to configure the VM’s networking so that I could connect to the outside internet from inside the VM. I was googling how to do it and there were all these references to “make a bridge”, so I figured I needed to use a bridge.

I’ve been avoiding understanding what a bridge is for years because it seemed confusing, but I copied and pasted some things from Github issues / various blog posts and none of them did what I wanted.

So I figured the “blindly copy and paste” approach wasn’t really going to work and decided OKAY TODAY IS THE DAY I WILL LEARN WHAT A BRIDGE IS.

I’m going to use a bunch of Docker examples in this post even though I’m not planning to use Docker because it’s already set up on my computer, it basically does what I want to do (but using a veth instead of a tap device), and I’m more familiar with it.

### some blog posts that helped me

I asked on Twitter for help understanding Docker container networking and people linked me to 2 really good blog posts that helped me:

* [container networking is simple](https://iximiuz.com/en/posts/container-networking-is-simple/) is a very clear explanation of how docker’s default container networking works with great examples
* [Tracing a packet journey using Linux tracepoints, perf and eBPF](https://blog.yadutaf.fr/2017/07/28/tracing-a-packet-journey-using-linux-tracepoints-perf-ebpf/) explains how to use `perf trace` an eBPF. The perf trace example in particular is super easy to use and it helped me feel more confident about what was going on

### a bridge is a layer 2 network interface

One thing I learned is that a bridge is a layer 2 (ethernet) device that can have an arbitrary number of network interfaces attached to it.

My understanding of how it works is:

1. You send a packet to the bridge
2. If the bridge has a network interface attached to it with a MAC address matching the destination MAC address on your packet, it sends it to that network interface
3. if there’s no matching interface or the packet is a broadcast packet (?) then it sends it to all of the interfaces

### tap devices and veths are also layer 2 devices

Docker uses veth pairs and the VM setup I’m using right now is using taps. These are also both layer 2 network interfaces. So they just sort of send packets on blindly and something else is responsible for setting the correct MAC addresses on packets.

### your computer finds container MAC addresses with ARP

One question I had was – if you’re sending a packet to a container and it needs to have the right MAC address on it to make it through the bridge, how does it know what the right MAC address is? I think the answer is the same as in a physical network: ARP!

So your computer sends an ARP request like “hey who’s 172.17.0.8” and the container sends an ARP reply back like “it’s me! here’s my MAC address”. The bridge gets involved here because it needs to send the ARP request to the container and the ARP reply back to the host.

I’m not 100% sure about this but I saw some ARP requests/replies being sent back and forth and it makes sense. I think this is really funny because – all of this information is on the same computer and it doesn’t feel like it should need to use ARP to look up MAC addresses, like it has all the information already! But I guess it just literally pretends like it’s a physical network.

### you need to set up the route table correctly

I think the most important thing with bridges is to set up the route tables correctly. So far my understanding is that there are 2 route table entries you need to set:

## route entry 1: on the host

The first route entry that needs to be set is on the host, to make sure that everything on the bridge subnet gets sent to the bridge. Here’s what that looks like in Docker’s default networking setup.

```bash
172.17.0.0/16 dev docker0 proto kernel scope link src 172.17.0.1 

```

## route entry 2: inside the container/VM

In the container/VM, the system’s _gateway_ needs to be set to the bridge

For example, in a Docker container, you’ll see this:

```bash
$ ip route list
default via 172.17.0.1 dev eth0 
172.17.0.0/16 dev eth0 proto kernel scope link src 172.17.0.2 

```

This `default via 172.17.0.1 dev eth0` line means that all packets going outside the container subnet should be first sent to 172.17.0.1, which is the `docker0` bridge (actually it’s a veth pair which leads to the docker0 bridge)

### you need an SNAT rule on the host

The third thing I need if I want the containers to be able to talk to the wider internet is an SNAT iptables rule on the host. Here’s what that looks like in Docker’s default setup.

```bash
$ sudo iptables-save
-A POSTROUTING -s 172.17.0.0/16 ! -o docker0 -j MASQUERADE

```

### now I can access the internet from my VM!

I’m still not 100% on bridges but I _did_ manage to configure a bridge so that when I ssh into my Firecracker VM I can access the internet! I am extremely delighted by this.

Right now I’m reusing the `docker0` bridge which I’ll probably stop doing, but it does work.

My understanding of all the steps to get a Docker-like setup where you can access a VM and it can access the outside internet are:

1. create the VM network interface (either a `tap` device or a veth) (`ip tuntap add dev "$TAP_DEV" mode tap`)
2. put the VM network interface behind the bridge (`sudo brctl addif docker0 $TAP_DEV`)
3. bring up the VM network interface (`ip link set dev "$TAP_DEV" up`)
4. set the VM’s gateway to be the bridge IP (via the kernel boot args, which I talk about below)
5. setup the host’s route table to route packets on the bridge’s subnet to the bridge (I think I’d do this when creating the bridge, which I didn’t do in this case because I’m reusing the docker bridge)
6. add an SNAT rule to the host to NAT packets coming out of the bridge (with `iptables`)
7. Change `/etc/resolv.conf` to say `nameserver 8.8.8.8` because I don’t have a working local resolver inside the VM

Here’s a snippet from my script that starts a firecracker VM. I modified it from the [firecracker-demo](https://github.com/firecracker-microvm/firecracker-demo) code.

I might post the whole thing later after I clean it up and stop using the docker bridge.

```bash
CONTAINER_IP=172.17.0.33
GATEWAY_IP=172.17.0.1
DOCKER_MASK_LONG=255.255.255.0
ip tuntap add dev "$TAP_DEV" mode tap
sudo brctl addif docker0 $TAP_DEV
ip link set dev "$TAP_DEV" up
# I'm not sure what these two are for exactly
sysctl -w net.ipv4.conf.${TAP_DEV}.proxy_arp=1 > /dev/null
sysctl -w net.ipv6.conf.${TAP_DEV}.disable_ipv6=1 > /dev/null

```

I also had to set `ip=${CONTAINER_IP}::${GATEWAY_IP}:${DOCKER_MASK_LONG}::eth0:off` in the kernel boot args. to set the VM’s gateway.

# 2021-01-20: Writing a Go program to manage Firecracker VMs

Hello! On Tuesday I spent more time working on figuring out how to run VMs with Firecracker for my SSH game project. They still start super fast and I’m really excited about them.

I got through 3 main things:

* learned 1 new thing about how linux bridges work
* figured out how to make my Ubuntu VMs boot fast
* wrote a small Go server to manage Firecracker VMs

## a linux bridge isn’t just a bridge

Every single time I say I’m confused about bridges someone will tell me “well, you see julia, a bridge is like a virtual switch”. This has never made any sense to me because I’ve never used a switch either, and also I felt like there was just something off about that explanation and that it didn’t explain the behavior I was seeing in a way I couldn’t articulate.

I think I finally learned something concrete about why bridges are confusing though! On Linux, when you create a bridge you get an network interface (like `docker0` for the Docker bridge). And that network interface has an ip address, and you can use that network interface/ip address as a gateway for containers/VMs you’re running.

But switches don’t have IP addresses! So if “a bridge is like a switch”, what’s going on? A bridge doesn’t really seems like it’s a switch! This analogy really seems to be breaking down. Someone on Twitter finally explained yesterday to me that when you create a bridge on Linux by default, you actually get 2 things:

1. a bridge (the kind that’s “like a switch”, that doesn’t have an ip address and just forwards packets blindly)
2. a network interface with the same name as the bridge, which has an IP address that you can use as a gateway.

They said that if you want, you can delete the network interface part of the bridge. I still haven’t experimented enough to work this out but I feel really good about this piece of information and like I can use it to properly understand what a Linux bridge is later.

Also I feel kind of vindicated in disbelieving this “a bridge is like a switch” explanation because I guess it’s technically true but it’s definitely missing a key piece of information for Linux bridges.

### fixed my Ubuntu VMs taking 2 minutes to boot

My Ubuntu Firecracker VMs had been taking 2-3 minutes to boot. They were hanging on a systemd step called “Load/Save Random Seed”, which apparently has something to do with kernel entropy.

I googled this and tried a lot of different things to fix it. Here are all the things that I tried that did not work:

1. add random.trust\_cpu=on to kernel boot args
2. set SYSTEMD\_RANDOM\_SEED\_CREDIT=true in systemd-random-seed.service
3. set SYSTEMD\_RANDOM\_SEED\_CREDIT=force in systemd-random-seed.service
4. install & enable haveged
5. install rng-tools
6. systemctl disable systemd-random-seed (though this really SHOULD have worked, I think I did something wrong there)

Finally I changed the timeout in the systemd-random-service file to 2 seconds, which worked! Now my VMs start fast. It’s extremely possible that I actually need this entropy generation for some reason (maybe to give `sshd` enough entropy so that it can generate session keys securely?) but I’ll cross that bridge when I come to it.

So now I can start an Ubuntu virtual machine in like 5 seconds! It’s really amazing. It’s probably possible to bring the boot time a bit more but I’m happy with that.

### wrote a Go program to manage Firecracker VMs

So far I’ve been starting VMs with the DigitalOcean API. So I wanted to write my own little API to create Firecracker VMs. It was pretty straightforward because I mostly just copied a bunch of code from this Firecracker command line tool called firectl: [https://github.com/firecracker-microvm/firectl](https://github.com/firecracker-microvm/firectl)

Here’s a gist with my (pretty messy) code so far: [firecracker-manager.go](https://gist.github.com/jvns/bb0a93e3b84a5e8344c6b24b57b2b490).

### [what my API looks like so far](#what-my-api-looks-like-so-far)

It totally works! I can start a VM with:

```bash
echo '{
    "root_image_path":  "/images/ubuntu.ext4",
    "kernel_path":    "/images/vmlinux"
}' | http post http://localhost:8080/create

```

and I can stop it with:

```bash
echo '{"id": "DE52E8A0-C624-18CB-F948-0B50C77C8F4A"}'  | http post localhost:8080/delete

```

It’s still missing some things, like:

* I should probably use the firecracker jailer for better security (like firectl does)
* right now I’m still writing the VM’s serial output to stdout
* I might make it REST-y and use a DELETE request to stop a VM

and probably lots more things I’m not thinking of right now

# January 21, 2021: Building VM images

I spent all day yesterday trying to build Ubuntu VM images that work with Firecracker without too much success.

## some miscellaneous things I learned about manipulating images

At some point I was trying to extract the filesystem from the Ubuntu cloud image to use with Firecracker. This did not work

* I can use `sfdisk` to view the partition table of a disk image
* I can use `dd` to extract `dd if=focal-server-cloudimg-amd64.img.orig.raw of=focal-image.ext4 skip=227328 count=4384735`
* Some Linux files are “sparse” which means that regions which are filled with 0s are collapsed. Very useful for disk images.
* I can use `fallocate -d` to turn a non-sparse file into a sparse file

### compiling the linux kernel isn’t that slow

The Firecracker instructions suggest building your own Linux kernel from source. This seemed intimidating to me because I hadn’t done it before but it was actually totally fine!

It only took me like 5 minutes to compile a Linux kernel from scratch (with `make -j12` on an AMD Ryzen 5). I was really surprised by this, I thought it would take like 2 hours.

I found out that the Linux kernel I compiled didn’t have the ISO filesystem support, so I needed to reconfigure it to include ISO filesystem support. This was very easy.

### the Ubuntu docker image doesn’t seem like the best base for a VM image

I’m still not sure what Ubuntu image I should use as a base for my VMs. I’ve been trying both the Ubuntu cloud image ([https://cloud-images.ubuntu.com/focal/current/focal-server-cloudimg-amd64.img](https://cloud-images.ubuntu.com/focal/current/focal-server-cloudimg-amd64.img)) and the Docker Ubuntu:20.04 image

Problems with the Ubuntu cloud image: I haven’t been able to get it to boot yet for some reason I haven’t worked out. It gets stuck on this `A start job is running for /dev/disk/by-label/UEFI` step that Idon’t understand.

Problems with the Docker image: it does boot, but I haven’t managed to convince `cloud-init` to successfully run in it yet. I also need to install a some basic things in it (like `init` and `udev` for some reason), and it’s not really designed to be a system to log into so I feel like it’s missing a lot of things

Maybe today I’ll go back to the cloud image and see what problems there are with getting it to boot.

# 2021-01-22: Building my VMs with Docker

[2021-01-22 by Julia Evans](https://jvns.ca/blog/2021/01/22/day-44--got-some-vms-to-start-in-firecracker/)

Another pretty short post today, still in Firecracker land.

## decided to build my VMs with Docker instead of cloud-init

I’ve been trying to figure out how to build my VMs for a while. Previously my plan was to just run `cloud-init` when the VM started, because I already had `cloud-init.yaml` files to launch VMs that I’d written previously.

It turned out that for some reason I couldn’t get `cloud-init` to start, and also it felt like `cloud-init` was going to be really slow to run – even when it was failing, it was already taking more than 10 seconds. I really wanted the VM to boot in less than 2 seconds.

So I decided to instead build my containers with Docker, convert the Docker filesystem to an ext4 image, and then start that image in Firecracker. Here’s what creating the image looks like in a bash script.

```bash
IMG_ID=$(docker build -q .)
CONTAINER_ID=$(docker run -td $IMG_ID /bin/bash)
MOUNTDIR=mnt
IMAGE=ubuntu.ext4
mount $IMAGE $MOUNTDIR
qemu-img create -f raw $IMAGE 800M
mkfs.ext4 $IMAGE
docker cp $CONTAINER_ID:/ $MOUNTDIR

```

It seems to work fine, and actually building my VMs with Docker feels a lot simpler than doing it with `cloud-init.yaml`, I think they might be easier to develop this way.

### setting up Docker-Compose’s bridges

I kind of want to run my VM management software with `docker-compose`, but it needs to make changes to the host network to set up the bridges / tap interfaces.

I learned that Docker Compose by default creates a new bridge for every `docker-compose` file with a random name. I didn’t think this was going to work for me, because I want to put my VMs on the same bridge as the `gotty` container that needs to SSH into the VMs. So I needed to know the name of the VM.

It turns out the Docker Compose is AMAZING and lets you explicitly set the name of the bridge for a network

So I set up a network in my `docker-compose.yml` file that looks like this, and put my `gotty` container in the `firenet` network.

```bash
version: "3.3"
networks:
  firenet:
    driver: bridge
    ipam:
     driver: default
     config:
       - subnet: 172.101.0.0/16
    driver_opts:
      com.docker.network.bridge.name: firecracker0

```

# 2021-01-23 - Firecracker: start a VM in less than a second

[2021-01-23 by Julia Evans](https://jvns.ca/blog/2021/01/23/firecracker--start-a-vm-in-less-than-a-second/)

Hello! I spent this whole past week figuring out how to use [Firecracker](https://github.com/firecracker-microvm/firecracker/) and I really like it so far.

Initially when I read about Firecracker being released, I thought it was just a tool for cloud providers to use – I knew that AWS Fargate and [https://fly.io](https://fly.io/) used it, but I didn’t think that it was something that I could directly use myself.

But it turns out that Firecracker is relatively straightforward to use (or at least as straightforward as anything else that’s for running VMs), the documentation and examples are pretty clear, you definitely don’t need to be a cloud provider to use it, and as advertised, it starts VMs really fast!

So I wanted to write about using Firecracker from a more DIY “I just want to run some VMs” perspective.

I’ll start out by talking about what I’m using it for, and then I’ll explain a few things I learned about it along the way.

## my goal: a game where every player gets their own virtual machine

I’m working on a sort of game to help people learn command line tools by giving them a problem to solve and a virtual machine to solve it in, a little like a CTF. It still basically exists only on my computer, but I’ve been working on it for a while.

Here’s a screenshot of one of the puzzles I’m working on right now. This one is about setting file extended attributes with `setfacl`.

[![Screenshot of a puzzle.](https://jvns.ca/images/read-me.png)](https://jvns.ca/images/read-me.png)

### why not use containers?

I wanted to use virtual machines and not containers for this project basically because I wanted to mimic a real production machine that the user has root access to – I wanted folks to be able to set sysctls, use `nsenter`, make `iptables` rules, configure networking with `ip`, run `perf`, basically literally anything.

### the problem: starting a virtual machine is slow

I wanted people to be able to click “Start” on a puzzle and instantly launch a virtual machine. Originally I was launching a DigitalOcean VM every time, but they took about a minute to boot, I was getting really impatient waiting for them every time, and I didn’t think it was an acceptable user experience for people to have to wait a minute.

I also tried using qemu, but for reasons I don’t totally understand, starting a VM with qemu was also kind of slow – it seemed to take at least maybe 20 seconds.

### Firecracker can start a VM in less than a second

Firecracker says this about performance in their [specification](https://github.com/firecracker-microvm/firecracker/blob/fea3897ccfab0387ce5cd4fa2dd49d869729d612/SPECIFICATION.md):

> It takes <= 125 ms to go from receiving the Firecracker InstanceStart API call to the start of the Linux guest user-space /sbin/init process.

So far I’ve been using Firecracker to start relatively large VMs – Ubuntu VMs running systemd as an init system – and it takes maybe 2-3 seconds for them to boot. I haven’t been measuring that closely because honestly 5 seconds is fast enough and I don’t mind too much about an extra 200ms either way.

But enough background, let’s talk about how to actually use Firecracker.

## here’s a “hello world” script to start a Firecracker VM

I said at the beginning of this post that Firecracker is pretty straightforward to get started with. Here’s how.

Firecracker’s [getting started](https://github.com/firecracker-microvm/firecracker/blob/fea3897ccfab0387ce5cd4fa2dd49d869729d612/docs/getting-started.md#getting-the-firecracker-binary) instructions are really good (they just work!) but it was separated into a bunch of steps and I wanted to see everything you have to do together in 1 shell script. So I wrote a short shell script you can use to start a Firecracker VM, and some quick instructions for how to use it.

Running a script like this was the first thing I did when trying to wrap my head around Firecracker. There’s basically 3 steps:

**step 1**: Download Firecracker from their [releases page](https://github.com/firecracker-microvm/firecracker/releases) and put it somewhere

**step 2**: Run this script as root (you might have to edit the last line with the path to the `firecracker` binary if it’s not in root’s PATH)

I also put this script in a gist: [firecracker-hello-world.sh](https://gist.github.com/jvns/c8470e75af67deec2e91ff1bd9883e53). The IP addresses here are chosen pretty arbitrarily. Most the script is just writing a JSON file.

```bash
set -eu

# download a kernel and filesystem image
[ -e hello-vmlinux.bin ] || wget https://s3.amazonaws.com/spec.ccfc.min/img/hello/kernel/hello-vmlinux.bin
[ -e hello-rootfs.ext4 ] || wget -O hello-rootfs.ext4 https://github.com/firecracker-microvm/firecracker-demo/raw/fea3897ccfab0387ce5cd4fa2dd49d869729d612/xenial.rootfs.ext4
[ -e hello-id_rsa ] || wget -O hello-id_rsa https://raw.githubusercontent.com/firecracker-microvm/firecracker-demo/ec271b1e5ffc55bd0bf0632d5260e96ed54b5c0c/xenial.rootfs.id_rsa

TAP_DEV="fc-88-tap0"

# set up the kernel boot args
MASK_LONG="255.255.255.252"
MASK_SHORT="/30"
FC_IP="169.254.0.21"
TAP_IP="169.254.0.22"
FC_MAC="02:FC:00:00:00:05"

KERNEL_BOOT_ARGS="ro console=ttyS0 noapic reboot=k panic=1 pci=off nomodules random.trust_cpu=on"
KERNEL_BOOT_ARGS="${KERNEL_BOOT_ARGS} ip=${FC_IP}::${TAP_IP}:${MASK_LONG}::eth0:off"

# set up a tap network interface for the Firecracker VM to user
ip link del "$TAP_DEV" 2> /dev/null || true
ip tuntap add dev "$TAP_DEV" mode tap
sysctl -w net.ipv4.conf.${TAP_DEV}.proxy_arp=1 > /dev/null
sysctl -w net.ipv6.conf.${TAP_DEV}.disable_ipv6=1 > /dev/null
ip addr add "${TAP_IP}${MASK_SHORT}" dev "$TAP_DEV"
ip link set dev "$TAP_DEV" up

# make a configuration file
cat <<EOF > vmconfig.json
{
  "boot-source": {
    "kernel_image_path": "hello-vmlinux.bin",
    "boot_args": "$KERNEL_BOOT_ARGS"
  },
  "drives": [
    {
      "drive_id": "rootfs",
      "path_on_host": "hello-rootfs.ext4",
      "is_root_device": true,
      "is_read_only": false
    }
  ],
  "network-interfaces": [
      {
          "iface_id": "eth0",
          "guest_mac": "$FC_MAC",
          "host_dev_name": "$TAP_DEV"
      }
  ],
  "machine-config": {
    "vcpu_count": 2,
    "mem_size_mib": 1024,
    "ht_enabled": false
  }
}
EOF
# start firecracker
firecracker --no-api --config-file vmconfig.json

```

**step 3**: You have a VM running!

You can also SSH into the VM like this, with the SSH key that the script downloaded:

```bash
ssh -o StrictHostKeyChecking=false  root@169.254.0.21 -i hello-id_rsa

```

You might notice that if you run `ping 8.8.8.8` inside this VM, it doesn’t work: it’s not able to connect to the outside internet. I think I’m actually going to use a setup like this for my puzzles where people don’t need to connect to the internet.

The networking commands and the rootfs image in this script are from the [firecracker-demo](https://github.com/firecracker-microvm/firecracker-demo/) repository which I found really helpful.

### how I put a Firecracker VM on the Docker bridge

I had a couple of problems with this “hello world” setup though:

* I wanted to be able to SSH to them from a Docker container (because I was running my game’s webserver in `docker-compose`)
* I wanted them to be able to connect to the outside internet

I struggled with trying to understand what a Linux bridge was and how it worked for about a day before figuring out how to get this to work. Here’s a slight modification of the previous script [firecracker-hello-world-docker-bridge.sh](https://gist.github.com/jvns/e13e6f498d26b584d8ab66651cdb04e0) which runs a Firecracker VM on the Docker bridge

You can run it as root and SSH to the resulting VM like this (the IP is different because it has to be in the Docker subnet).

```bash
ssh -o StrictHostKeyChecking=false  root@172.17.0.21 -i hello-id_rsa

```

It basically just changes 2 things:

1. There’s an extra `sudo brctl addif docker0 $TAP_DEV` to add the VM’s network interface to the Docker bridge
2. It changes the gateway in the kernel boot args to the Docker bridge network interface’s IP (172.17.0.1)

My guess is that most people probably won’t want to use the Docker bridge, if you just want the VM to be able to connect to the outside internet I think the best way is to create a new bridge.

In my application I’m actually using a bridge called `firecracker0` which is a docker-compose network I made. It feels a little sketchy to be using a bridge managed by Docker in this way but for now it works so I’ll keep doing that unless I find a better way.

### how I built my own Firecracker images

This “hello world” example is all very well and good, but you might say – ok, how do I build my own images?

Basically you have to do 2 things:

1. Make a Linux kernel. I wanted a 5.8 kernel so I used the instructions in the [firecracker docs on creating your own image](https://github.com/firecracker-microvm/firecracker/blob/fea3897ccfab0387ce5cd4fa2dd49d869729d612/docs/rootfs-and-kernel-setup.md) for compiling a Linux kernel and they worked. I was kind of intimidated by this because I’d somehow never compiled a Linux kernel before, but I followed the instructions and it just worked the first time. I thought it would be super slow but it actually took less than 10 minutes to compile from scratch.
2. Make an `ext4` filesystem image with all the files you want in your VM’s filesystem.

Here’s how I put together my filesystem. Initially I tried downloading Ubuntu’s focal cloud image and extracting the root partition with `dd`, but I couldn’t get it work.

Instead, I did what the Firecracker docs suggested and I built a Docker container and copied the contents of the container into a filesystem image.

Here’s what the `Dockerfile` I used looked like approximately: (I haven’t tested this exact Dockerfile but I think it should work). The main things are that you have to install some kind of init system because the default `ubuntu:20.04` image doesn’t come with one because you don’t need one in a container. I also ran `unminimize` to restore some man pages because the container is for interactive use.

```bash
FROM ubuntu:20.04
RUN apt-get update
RUN apt-get install -y init openssh-server
RUN yes | unminimize
# copy over some SSH keys and install other programs I wanted

```

And here’s the basic shell script I’ve been using to create a filesystem image from the Docker container. I ran the whole thing as root, but technically you only have to run `mount` as root.

```bash
IMG_ID=$(docker build -q .)
CONTAINER_ID=$(docker run -td $IMG_ID /bin/bash)

MOUNTDIR=mnt
FS=mycontainer.ext4

mkdir $MOUNTDIR
qemu-img create -f raw $FS 800M
mkfs.ext4 $FS
mount $FS $MOUNTDIR
docker cp $CONTAINER_ID:/ $MOUNTDIR
umount $MOUNTDIR

```

I’m still not quite sure how much I’m going to like this approach of using Docker containers to create VM images – it feels a bit weird to me but it’s been working fine so far.

I think most people who use Firecracker use a more lightweight init system than systemd and it’s definitely not necessary to use systemd but I think I’m going to stick with systemd for now because I want it to feel mostly like a normal production Linux system and a lot of the production servers I’ve used have used systemd.

Okay, that’s all I have to say about creating images. Let’s talk a bit more about configuring Firecracker.

### Firecracker supports either a socket interface or a configuration file

You can start a Firecracker VM 2 ways:

1. create a configuration file and run `firecracker --no-api --config-file vmconfig.json`
2. create an API socket and write instructions to the API socket (like they explain in their [getting started](https://github.com/firecracker-microvm/firecracker/blob/fea3897ccfab0387ce5cd4fa2dd49d869729d612/docs/getting-started.md#getting-the-firecracker-binary) instructions)

I really liked the configuration file approach for doing some initial experimentation because I found it easier to be able to see everything all in one place. But when integrating Firecracker with my actual application in real life, I found it easier to use the API.

### how I wrote a HTTP service that starts Firecracker VMs: use the Go SDK

I wanted to have a little HTTP service that I could call from my Ruby on Rails server to start new VMs and stop them when I was done with them.

Here’s what the interface looks like – you give it a root image and a kernel and it returns an ID and the VM’s IP address. All of the files paths are just local paths on my machine.

```bash
$ http post localhost:8080/create root_image_path=/images/base.ext4 kernel_path=/images/vmlinux-5.8
HTTP/1.1 200 OK
{
    "id": "D248122A-1CCA-475C-856E-E3003A913F32",
    "ip_address": "172.102.0.4"
}

```

and then here’s what deleting a VM looks like (I might make this use the `DELETE` method later to make it more REST-y :) )

```bash
$ http post localhost:8080/delete id=D248122A-1CCA-475C-856E-E3003A913F32
HTTP/1.1 200 OK

```

At first I wasn’t sure how I was going to use the Firecracker socket API to implement this interface, but then I discovered that there’s a [Go SDK](https://github.com/firecracker-microvm/firecracker-go-sdk)! This made it way easier to generate the correct JSON, because there were a bunch of structs and the compiler would tell me if I made a typo in a field name.

I basically wrote all of my code so far by copying and modifying code from [firectl](https://github.com/firecracker-microvm/firectl/), a Go command line tool. The reason I wrote my own tool insted of just using `firectl` directly was that I wanted to have a HTTP API that could launch and stop lots of different VMs.

I found the `firectl` code and the Go SDK pretty easy to understand so I won’t say too much more about it here.

If you’re interested you can see [a gist with my current HTTP service for managing Firecracker VMs](https://gist.github.com/jvns/9b274f24cfa1db7abecd0d32483666a3) which is a huge mess and pretty buggy and not intended for anyone but me to use. It does start VMs successfully though which is an important first step!!!

### DigitalOcean supports nested virtualization

Another question I had was: “ok, where am I going to run these Firecracker VMs in production?”. The funny thing about running a VM in the cloud is that cloud instances are _already_ VMs. Running a VM inside a VM is called “nested virtualization” and not all cloud providers support it – for example AWS doesn’t.

Right now I’m using DigitalOcean and I was delighted to see that DigitalOcean does support nested virtualization even on their smallest droplets – I tried running the “hello world” Firecracker script from above and it just worked!

I think GCP supports nested virtualization too but I haven’t tried it. The official Firecracker documentation suggests using a `metal` instance on AWS, probably because Firecracker is made by AWS.

I don’t know what the performance implications of using nested virtualization are yet but I guess I’ll find out!

### Firecracker only runs on Linux

I should say that Firecracker uses KVM so it only runs on Linux. I don’t know if there’s a way to start VMs in a similarly fast way on a Mac, maybe there is? Or maybe there’s something special about KVM? I don’t understand how KVM works.

### some open questions

A few things I still haven’t figured out:

* Right now I’m not using `jailer`, another part of Firecracker that helps further isolate the Firecracker VM by adding some `seccomp-BPF` rules and other things. Maybe I should be! `firectl` uses `jailer` so it would be pretty easy to copy the code that does that.
* I still don’t totally understand _why_ Firecracker is fast (or alternatively, why qemu is slow). This [LWN article](https://lwn.net/Articles/775736/) says that it’s because Firecracker emulates less devices than qemu does, but I don’t know exactly which devices are the ones that are making qemu slow to start.
* will it be slow to use nested virtualization?
* I don’t know if it’s possible to run graphical applications in Firecracker, it seems like it might not because it’s intended for servers, but maybe it is possible?
* I’m not sure how many Firecracker VMs I can run at a time on my little $5/month DigitalOcean droplet, I need to do some of experiments.

### links

A few people gave me useful links answering some of the above questions.

about why qemu is slower than Firecracker (thanks @tptacek for these):

* [Optimizing QEMU boot time (PDF)](http://oirase.annexia.org/tmp/paper.pdf) is really interesting and extremely clearly written
* [some slides about qemu-lite, a version of qemu that boots faster and uses less memory](http://events17.linuxfoundation.org/sites/events/files/slides/Light%20weight%20virtualization%20with%20QEMU%26KVM_0.pdf)

about how Firecracker works:

* Shuveb Hussain’s great post [How AWS Firecracker works: a deep dive](https://unixism.net/2019/10/how-aws-firecracker-works-a-deep-dive/) explains how Firecracker works and demonstrates some of the concepts with a tiny version of Firecracker called . ([blog post on Sparkler](https://unixism.net/2019/10/sparkler-kvm-based-virtual-machine-manager/), [Sparkler github repo](https://github.com/shuveb/sparkler)). Really cool.
* the Firecracker authors’ paper: [Firecracker: Lightweight Virtualization for Serverless Applications](https://www.usenix.org/conference/nsdi20/presentation/agache) (there’s a video, slides, and a talk)

on building Firecracker images:

* Álvaro Hernández has a [blog post with example code of how he got cloud-init to work with Firecracker](https://ongres.com/blog/automation-to-run-vms-based-on-vanilla-cloud-images-on-firecracker/). I haven’t tried it yet but it looks really helpful
* @jeromegn mentioned in the HN comments that fly.io uses the devmapper snapshotter. I don’t know what that is yet but here’s the [kernel documentation on device-mapper snapshot support](https://www.kernel.org/doc/Documentation/device-mapper/snapshot.txt)
* the “How AWS Firecracker works” post mentions “virtio-fs, which allows efficient sharing of files and directories between hosts and guest. This way, a directory containing the guests’ file system can be on the host, much like how Docker works.” the [kernel docs on virtio-fs](https://www.kernel.org/doc/html/latest/filesystems/virtiofs.html)

software:

* [ignite](https://github.com/weaveworks/ignite) lets you take a container image and run it as a Firecracker VM
