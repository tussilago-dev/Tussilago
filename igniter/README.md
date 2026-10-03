# Igniter

Igniter is the orchestrator for managing and deploying microVMs for Tussilago.

## Dependencies

- [extra/squashfs-tools](https://archlinux.org/packages/extra/x86_64/squashfs-tools/)

```bash
# Enable ip forwarding
echo 1 | sudo tee /proc/sys/net/ipv4/ip_forward
sudo iptables -P FORWARD ACCEPT
```
