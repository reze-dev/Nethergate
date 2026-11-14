#!/bin/bash
# Generate a throwaway CA and certificates, then run OpenVPN in the
# foreground. The PKI lives on a volume so the client container can use it.
set -euo pipefail

pki=/etc/openvpn/pki
mkdir -p "$pki"
chmod 0777 "$pki"

if [[ ! -f "$pki/ca.crt" ]]; then
    openssl genrsa -out "$pki/ca.key" 2048 2>/dev/null
    # The CA deliberately carries no extended key usage. OpenVPN checks the
    # EKU of every certificate in the chain it is given, and a CA that
    # declares an EKU fails the check with "VERIFY KU ERROR".
    openssl req -x509 -new -nodes -key "$pki/ca.key" -sha256 -days 2 \
        -addext "basicConstraints=critical,CA:TRUE" \
        -subj "/CN=warpgate-test-ca" -out "$pki/ca.crt" 2>/dev/null

    # The server certificate needs the serverAuth extended key usage and the
    # client certificate needs clientAuth, otherwise OpenVPN rejects the peer
    # with a key usage error.
    openssl genrsa -out "$pki/server.key" 2048 2>/dev/null
    openssl req -new -key "$pki/server.key" -subj "/CN=server" \
        -out "$pki/server.csr" 2>/dev/null
    printf "extendedKeyUsage=serverAuth\n" > "$pki/server.ext"
    openssl x509 -req -in "$pki/server.csr" -CA "$pki/ca.crt" -CAkey "$pki/ca.key" \
        -CAcreateserial -out "$pki/server.crt" -days 2 -sha256 \
        -extfile "$pki/server.ext" 2>/dev/null
    # The chain file must contain the leaf first and then the CA, so that
    # OpenVPN sends both to the client. Otherwise the client only sees the
    # leaf and cannot check the key usage of the chain.
    cat "$pki/server.crt" "$pki/ca.crt" > "$pki/server-chain.crt"

    openssl genrsa -out "$pki/client.key" 2048 2>/dev/null
    openssl req -new -key "$pki/client.key" -subj "/CN=client" \
        -out "$pki/client.csr" 2>/dev/null
    printf "extendedKeyUsage=clientAuth\n" > "$pki/client.ext"
    openssl x509 -req -in "$pki/client.csr" -CA "$pki/ca.crt" -CAkey "$pki/ca.key" \
        -CAcreateserial -out "$pki/client.crt" -days 2 -sha256 \
        -extfile "$pki/client.ext" 2>/dev/null

    cp "$pki/ca.crt" "$pki/client-ca.crt"
    # Same for the client: leaf first, then the CA.
    cat "$pki/client.crt" "$pki/ca.crt" > "$pki/client-chain.crt"

    # OpenVPN's own generator produces the static key format it expects.
    openvpn --genkey --secret "$pki/ta.key" 2>/dev/null
fi

# A stand-in for the corporate bastion. It serves a marker over HTTP on the
# far side of the tunnel, so the test can prove that traffic really flows
# through the tunnel rather than merely being configured.
mkdir -p /var/www
printf 'CORPORATE-RESOURCE-REACHED\n' > /var/www/index.html
python3 -m http.server 443 --directory /var/www >/var/www/http.log 2>&1 &
http_pid=$!
trap 'kill "$http_pid" 2>/dev/null || true' EXIT

exec openvpn --config /etc/openvpn/server.conf
