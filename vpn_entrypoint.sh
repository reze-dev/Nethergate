#!/usr/bin/bash
# Establish the VPN and keep it up.
#
# The tunnel lives entirely inside this container's network namespace. The
# warpgate and firefox containers join that namespace, so their traffic
# goes through the VPN while the host keeps its own internet connection.
# Nothing here changes the host's routing.
#
# Everything is configured through the environment, so this image carries no
# details of any particular gateway.
set -u

: "${VPN_CLIENT:=openconnect}"
: "${VPN_USERNAME:=}"
: "${VPN_AUTHGROUP:=}"
: "${VPN_GATEWAY:=}"
: "${VPN_CONFIG:=}"
: "${OCP_BASTION_IP:=}"
: "${OCP_BASTION_PORT:=443}"

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

# The forward makes the resource behind the VPN reachable on the usual port
# inside the pod, so the browser can use https://127.0.0.1/ without knowing
# anything about the network behind it.
if [[ -n "$OCP_BASTION_IP" ]]; then
  echo "Forwarding TCP ${OCP_BASTION_PORT} -> ${OCP_BASTION_IP}:${OCP_BASTION_PORT}..."
  socat "TCP-LISTEN:${OCP_BASTION_PORT},bind=0.0.0.0,fork,reuseaddr" \
        "TCP:${OCP_BASTION_IP}:${OCP_BASTION_PORT}" &
  socat_pid=$!
fi

# The openvpn path is used to validate the container plumbing without
# touching a corporate gateway.
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

if [[ "$VPN_CLIENT" != "openconnect" ]]; then
  echo "VPN_CLIENT has to be openconnect or openvpn, not '${VPN_CLIENT}'" >&2
  exit 1
fi

for setting in VPN_USERNAME VPN_AUTHGROUP VPN_GATEWAY; do
  if [[ -z "${!setting}" ]]; then
    echo "$setting is not set" >&2
    exit 1
  fi
done

if [[ ! -r "$PASSWORD_FILE" || ! -s "$PASSWORD_FILE" ]]; then
  echo "VPN password secret is missing or unreadable." >&2
  echo "Put the corporate password in secrets/vpn_password and start the stack again." >&2
  echo "Without it the VPN cannot come up, and nothing that depends on it can start." >&2
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
