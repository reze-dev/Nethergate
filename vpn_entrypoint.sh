#!/usr/bin/bash
# Establish the corporate VPN and keep it up.
#
# The tunnel lives entirely inside this container's network namespace. The
# warpgate and firefox containers join that namespace, so their traffic
# goes through the corporate VPN while the host keeps its own internet
# connection. Nothing here changes the host's routing.
set -u

: "${VPN_USERNAME:=}"
: "${VPN_AUTHGROUP:=<corporate-vpn-group>}"
: "${VPN_GATEWAY:=<corporate-vpn-endpoint>}"
: "${OCP_BASTION_IP:=}"
: "${VPN_CLIENT:=openconnect}"
: "${VPN_CONFIG:=}"

PASSWORD_FILE=/run/secrets/vpn_password

vpn_pid=""
socat_pid=""

stop_vpn() {
  if [[ -n "$socat_pid" ]]; then
    kill -TERM "$socat_pid" 2>/dev/null || true
  fi
  if [[ -n "$vpn_pid" ]]; then
    kill -TERM "$vpn_pid" 2>/dev/null || true
    wait "$vpn_pid" 2>/dev/null || true
  fi
  exit 0
}

trap stop_vpn TERM INT

if [[ -n "$OCP_BASTION_IP" ]]; then
  echo "Forwarding TCP 443 -> ${OCP_BASTION_IP}:443..."
  socat TCP-LISTEN:443,bind=0.0.0.0,fork,reuseaddr "TCP:${OCP_BASTION_IP}:443" &
  socat_pid=$!
fi

# The openvpn test path is used to validate the container plumbing
# without touching the corporate gateway.
if [[ "$VPN_CLIENT" == "openvpn" ]]; then
  if [[ -z "$VPN_CONFIG" ]]; then
    echo "VPN_CONFIG is required when VPN_CLIENT=openvpn" >&2
    exit 1
  fi
  echo "Starting OpenVPN (test mode)..."
  while true; do
    openvpn --config "$VPN_CONFIG"
    echo "OpenVPN exited with status $?; retrying in 10 seconds."
    sleep 10
  done
fi

if [[ ! -r "$PASSWORD_FILE" || ! -s "$PASSWORD_FILE" ]]; then
  echo "VPN password secret is missing or unreadable" >&2
  exit 1
fi

while true; do
  echo "Starting OpenConnect..."

  openconnect \
    --protocol=anyconnect \
    --authgroup="$VPN_AUTHGROUP" \
    --form-entry="main:username=${VPN_USERNAME}" \
    --passwd-on-stdin \
    --non-inter \
    --force-dpd=30 \
    --reconnect-timeout=300 \
    --script=/etc/vpnc/vpnc-script \
    "$VPN_GATEWAY" <"$PASSWORD_FILE" &

  vpn_pid=$!
  wait "$vpn_pid"
  rc=$?
  vpn_pid=""

  echo "OpenConnect exited with status ${rc}; retrying in 10 seconds."
  sleep 10
done
