#!/usr/bin/env bash
# Entrypoint for the warpgate browser container.
#
# Three things have to happen before the Kasm startup script runs.
#
# The first is the hostname. The Kasm image resolves its own hostname while
# starting up and aborts with "hostname: Name or service not known" when the
# lookup fails. This container joins the network namespace of another one,
# so Podman does not add its hostname to /etc/hosts the way it does for a
# container that owns the namespace. Adding the name here keeps the startup
# working. /etc/hosts is a root-owned tmpfs, so that has to happen while
# still root.
#
# The second is the VNC password. The image bakes in "vncpassword" and
# "vncviewonlypassword" as defaults, so the password has to come from
# somewhere else or the browser is open to anything that can reach the port.
# The secret files are the source of truth when they are there, and the
# environment is only a fallback for when they are not.
#
# The third is dropping privileges. The browser must not run as root, because
# Firefox refuses to start when HOME belongs to another user.
set -e

VNC_SECRET=/run/secrets/vnc_password
VNC_VIEW_ONLY_SECRET=/run/secrets/vnc_viewonly_password

if [[ -r "$VNC_SECRET" ]]; then
    VNC_PW="$(cat "$VNC_SECRET")"
    export VNC_PW
fi

if [[ -r "$VNC_VIEW_ONLY_SECRET" ]]; then
    VNC_VIEW_ONLY_PW="$(cat "$VNC_VIEW_ONLY_SECRET")"
    export VNC_VIEW_ONLY_PW
fi

# Without a password the Kasm startup script fails in a way that says
# nothing about what is wrong, so say it here instead.
if [[ -z "${VNC_PW:-}" ]]; then
    echo "No VNC password was found." >&2
    if [[ -e "$VNC_SECRET" && ! -f "$VNC_SECRET" ]]; then
        echo "$VNC_SECRET is a directory, which happens when the stack is started" >&2
        echo "before ./configure.py has made it." >&2
    else
        echo "Expected it at $VNC_SECRET." >&2
    fi
    echo "Run ./configure.py and start the stack again." >&2
    exit 1
fi

if [[ "$(id -u)" == "0" ]]; then
    name="$(hostname)"
    if ! grep -qE "[[:space:]]${name}(\$|[[:space:]])" /etc/hosts 2>/dev/null; then
        printf '127.0.0.1\t%s\n' "$name" >> /etc/hosts
    fi

    exec setpriv --reuid=1000 --regid=1000 --init-groups \
        /dockerstartup/kasm_default_profile.sh \
        /dockerstartup/vnc_startup.sh \
        /dockerstartup/kasm_startup.sh \
        ${1:---wait}
fi

exec /dockerstartup/kasm_default_profile.sh \
    /dockerstartup/vnc_startup.sh \
    /dockerstartup/kasm_startup.sh \
    ${1:---wait}
