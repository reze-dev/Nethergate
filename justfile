# Warpgate bastion and container browser.
#
# Run `just` on its own to see everything that can be done.

set shell := ["bash", "-uc"]

compose := "podman-compose"
test_compose := "podman-compose -f podman-compose.test.yml"

vpn := "warpgate-vpn"
bastion := "warpgate"
browser := "warpgate-firefox"

# Podman format strings, kept in variables because just would otherwise try
# to read the braces as its own interpolation.
ps_format := "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
name_format := "{{.Name}}"

# --- setting up ------------------------------------------------------------

# Ask for everything that is not known yet and write the configuration
configure:
    @./configure.py

# Validate the configuration and change nothing
check:
    @./configure.py --check

# Show the settings that are in effect
settings:
    @./configure.py --print

# Throw away warpgate's state and generate it again
reset:
    @./configure.py --reset

# Build every image the stack needs
build:
    @podman build -t localhost/warpgate-vpn:latest .
    @podman build -t localhost/warpgate-firefox:latest -f Dockerfile.firefox .
    @podman build -t localhost/ovpn-test-server:latest -f test/Dockerfile.ovpn-server test/
    @echo "images are built"

# --- running ---------------------------------------------------------------

# Start the stack
up:
    @{{compose}} up -d
    @echo "the bastion is on https://127.0.0.1:8443 and the browser on 127.0.0.1:6901"

# Stop the stack
down:
    @{{compose}} down

# Stop and start the stack again
restart: down up

# Follow the logs
logs:
    @{{compose}} logs -f

# Follow the logs of the vpn container
logs-vpn:
    @{{compose}} logs -f {{vpn}}

# Follow the logs of the bastion container
logs-bastion:
    @{{compose}} logs -f {{bastion}}

# Follow the logs of the browser container
logs-browser:
    @{{compose}} logs -f {{browser}}

# Show what is running
status:
    @podman ps --format '{{ps_format}}' | grep -E 'NAMES|warpgate' || true
    @echo
    @{{compose}} ps 2>/dev/null || true

# Check that the whole stack works
verify:
    @./verify.sh {{vpn}} {{bastion}} {{browser}}

# --- the test harness ------------------------------------------------------

# Start the stack against a local OpenVPN server
test-up:
    @{{test_compose}} up -d
    @echo "the test stack is up"

# Stop the test stack and throw away its PKI
test-down:
    @{{test_compose}} down -v

# Check that the test stack works
test-verify:
    @./verify.sh warpgate-vpn-test warpgate-warpgate-test warpgate-firefox-test http://127.0.0.1:443/


# Run the browser's startup in the foreground and keep the trace
debug-browser:
    @{{test_compose}} --profile debug up -d ff-trace
    @echo "the trace is in: podman exec warpgate-ff-trace tail -f /tmp/trace.log"

# --- the credentials -------------------------------------------------------

# Show the warpgate admin password
admin:
    @cat secrets/admin_password

# Show the browser's vnc password
vnc:
    @cat secrets/vnc_password

# Show where everything is and how it is protected
secrets:
    @ls -la secrets/

# --- housekeeping ----------------------------------------------------------

# Stop everything and remove the containers, the volumes and the images
clean:
    @{{compose}} down -v || true
    @{{test_compose}} down -v || true
    @podman rm -f {{vpn}} {{bastion}} {{browser}} warpgate-vpn-test warpgate-warpgate-test warpgate-firefox-test warpgate-ovpn-server 2>/dev/null || true
    @podman volume ls --format '{{name_format}}' | grep -i pki | xargs -r podman volume rm || true
    @podman rmi -f localhost/warpgate-vpn:latest localhost/warpgate-firefox:latest localhost/ovpn-test-server:latest 2>/dev/null || true
    @echo "everything is cleaned up"

# Remove warpgate's state but keep the images and the secrets
clean-data:
    @{{compose}} down || true
    @podman unshare rm -rf data
    @echo "warpgate's state is gone, run 'just configure' to make a new one"
