FROM registry.fedoraproject.org/fedora:latest

# openvpn is the stand-in used by the test harness. The production path uses
# openconnect against the corporate gateway.
RUN dnf install -y \
    openconnect \
    openvpn \
    vpnc-script \
    iproute \
    iputils \
    openssh-clients \
    socat \
    procps-ng \
    curl \
    && dnf clean all

COPY vpn_entrypoint.sh /usr/local/bin/vpn_entrypoint.sh

RUN chmod 0755 /usr/local/bin/vpn_entrypoint.sh

ENTRYPOINT ["/usr/local/bin/vpn_entrypoint.sh"]
