#!/usr/bin/env bash
# Entrypoint for the warpgate browser container.
#
# The Kasm image resolves its own hostname while starting up and aborts with
# "hostname: Name or service not known" when the lookup fails. This container
# joins the network namespace of another one, so Podman does not add its
# hostname to /etc/hosts the way it does for a container that owns the
# namespace. Adding the name here keeps the startup working.
#
# /etc/hosts is a root-owned tmpfs, so the fix has to happen while still
# root. The browser itself must not run as root, because Firefox refuses to
# start when HOME belongs to another user.
set -e

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
