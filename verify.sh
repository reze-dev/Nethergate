#!/usr/bin/env bash
#
# Check that the warpgate stack is actually working.
#
# Every check is a fact about the running stack rather than a guess: the
# tunnel is up, the pods really share a network namespace, the corporate
# resource is reachable through the tunnel, and the host is untouched.
#
# Usage:
#   ./verify.sh [vpn-container] [bastion-container] [browser-container] [url]
#
# The defaults are the production container names. The url is what the
# browser pod should be able to reach through the tunnel. When it is not
# given it is taken from OCP_BASTION_URL in .env, because a resource behind
# a corporate router usually answers to a name rather than to an address.
set -uo pipefail

VPN="${1:-warpgate-vpn}"
BASTION="${2:-warpgate}"
BROWSER="${3:-warpgate-firefox}"
URL="${4:-}"

if [[ -z "$URL" && -r .env ]]; then
    URL="$(grep '^OCP_BASTION_URL=' .env 2>/dev/null | head -1 | cut -d= -f2-)"
fi
URL="${URL:-http://127.0.0.1:443/}"

BOLD=$'\033[1m'
GREEN=$'\033[32m'
RED=$'\033[31m'
OFF=$'\033[0m'

passed=0
failed=0

check() {
    local name="$1" want="$2" got="$3"
    if [[ "$got" == *"$want"* ]]; then
        printf '  %sok%s    %-36s %s\n' "$GREEN" "$OFF" "$name" "$got"
        passed=$((passed + 1))
    else
        printf '  %sFAIL%s  %-36s %s (wanted %s)\n' "$RED" "$OFF" "$name" "$got" "$want"
        failed=$((failed + 1))
    fi
}

note() {
    printf '  %s%s%s\n' "$BOLD" "$1" "$OFF"
}

container_state() {
    podman inspect "$1" --format '{{.State.Status}}' 2>/dev/null || echo "missing"
}

echo
printf '%sChecking the warpgate stack%s\n' "$BOLD" "$OFF"
echo

# --- the containers -------------------------------------------------------
for container in "$VPN" "$BASTION" "$BROWSER"; do
    check "container $container" "running" "$(container_state "$container")"
done

# --- the tunnel -----------------------------------------------------------
tunnel="$(podman exec "$VPN" ip -br addr show tun0 2>/dev/null | head -1)"
check "vpn tunnel" "tun0" "$tunnel"

# --- one shared network namespace ----------------------------------------
namespaces=""
for container in "$VPN" "$BASTION" "$BROWSER"; do
    pid="$(podman inspect "$container" --format '{{.State.Pid}}' 2>/dev/null)"
    if [[ -n "$pid" && -e "/proc/$pid/ns/net" ]]; then
        namespaces="$namespaces $(readlink "/proc/$pid/ns/net")"
    fi
done
read -r -a parts <<< "$namespaces"
if [[ ${#parts[@]} -ge 3 ]] && [[ "${parts[0]}" == "${parts[1]}" && "${parts[1]}" == "${parts[2]}" ]]; then
    check "shared network namespace" "${parts[0]}" "${parts[0]}"
else
    check "shared network namespace" "one namespace" "${namespaces:-none}"
fi

# --- through the tunnel ---------------------------------------------------
reached="$(podman exec "$BROWSER" bash -c \
    "unset http_proxy https_proxy; timeout 30 curl -sk --max-time 25 --noproxy '*' -o /dev/null -w '%{http_code}' '$URL'" \
    2>/dev/null | tail -1)"
check "corporate resource via the tunnel" "200" "${reached:-no answer}"

# --- the host is left alone ----------------------------------------------
tun_count="$(ip -br addr 2>/dev/null | grep -c tun)"
check "host has no tunnel" "0" "$tun_count"

route_count="$(ip route show 2>/dev/null | grep -cE '10\.(8|150|250)\.')"
check "host has no vpn routes" "0" "$route_count"

internet="$(timeout 15 curl -s --max-time 10 -o /dev/null -w '%{http_code}' https://example.com 2>/dev/null)"
check "host still has internet" "200" "${internet:-none}"

# --- the browser ----------------------------------------------------------
firefox="$(podman exec "$BROWSER" bash -c \
    "ps -o user= -C firefox 2>/dev/null | head -1" 2>/dev/null | tr -d ' ')"
check "browser runs as kasm-user" "kasm-user" "${firefox:-not running}"

restarts="$(podman inspect "$BROWSER" --format '{{.RestartCount}}' 2>/dev/null)"
check "browser is not restarting" "0" "${restarts:-?}"

vnc_password="$(podman exec --user 1000 "$BROWSER" bash -c \
    'tr "\0" "\n" < /proc/1/environ | grep "^VNC_PW="' 2>/dev/null | sed 's/^VNC_PW=//' | tr -d '\n')"
if [[ -n "$vnc_password" && "$vnc_password" != "vncpassword" ]]; then
    check "vnc password is not the image default" "set" "set"
else
    check "vnc password is not the image default" "set" "${vnc_password:-missing}"
fi

# --- the bastion ----------------------------------------------------------
bastion_http="$(timeout 15 curl -sk --max-time 10 --noproxy '*' -o /dev/null \
    -w '%{http_code}' https://127.0.0.1:8443/ 2>/dev/null)"
check "web bastion answers" "401" "${bastion_http:-none}"

if [[ -s secrets/admin_password ]]; then
    admin_http="$(timeout 20 curl -sk --max-time 10 --noproxy '*' -o /dev/null \
        -w '%{http_code}' -X POST https://127.0.0.1:8443/@warpgate/api/auth/login \
        -H 'Content-Type: application/json' \
        -d "{\"username\":\"admin\",\"password\":\"$(cat secrets/admin_password)\"}" 2>/dev/null)"
    check "admin can log in" "201" "${admin_http:-none}"
else
    note "admin login was not checked, there is no admin password"
fi

ssh_answer="$(timeout 20 ssh -o StrictHostKeyChecking=no -o BatchMode=yes \
    -o ConnectTimeout=8 -p 2222 test@127.0.0.1 2>&1 | tail -1)"
check "ssh bastion answers" "Permission denied" "$ssh_answer"

vnc_port="$(timeout 10 bash -c 'exec 3<>/dev/tcp/127.0.0.1/6901' 2>/dev/null && echo open || echo closed)"
check "vnc port" "open" "$vnc_port"

# --- and the database is healthy -----------------------------------------
db_errors="$(podman logs "$BASTION" 2>&1 | grep -ci 'unable to open database')"
check "no database errors" "0" "$db_errors"

# --- result ---------------------------------------------------------------
echo
if [[ "$failed" -eq 0 ]]; then
    printf '%s%s checks passed%s\n' "$GREEN" "$BOLD" "$OFF"
else
    printf '%s%s of %s checks failed%s\n' "$RED" "$BOLD" "$failed" "$OFF"
fi
echo
exit $(( failed > 0 ? 1 : 0 ))
