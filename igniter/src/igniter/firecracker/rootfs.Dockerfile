FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    ca-certificates \
    e2fsprogs \
    iproute2 \
    iputils-ping \
    systemd \
    systemd-sysv \
    tzdata \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Use UTC and C.UTF-8 in the guest.
RUN printf 'LANG=C.UTF-8\n' > /etc/locale.conf \
    && printf 'Etc/UTC\n' > /etc/timezone \
    && ln -snf /usr/share/zoneinfo/Etc/UTC /etc/localtime

# Each VM generates its own machine ID on first boot.
RUN install -d /var/lib/dbus \
    && ln -sf /etc/machine-id /var/lib/dbus/machine-id \
    && truncate -s 0 /etc/machine-id
