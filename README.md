# Warpgate bastion and container browser

A local bastion for the corporate VPN, plus a containerised browser that runs
inside that VPN. The point of the layout is that **the VPN lives only inside the
pods**: the containers reach the corporate network through the tunnel, while the
host keeps its own internet connection. You can browse the internet and the
corporate intranet at the same time.

## How it works

```
host ──────────────────────────────► internet
  │
  │  (no tun device, no VPN routes on the host)
  │
  └─► podman network "warpgate_default"
        │
        └─► warpgate-vpn  ──► tun0 ──► corporate VPN
              │  ▲
              │  └── network namespace shared by the two pods below
              │
              ├─► warpgate  (SSH + HTTP bastion)
              └─► firefox  (Kasm image, browser over VNC)
```

`warpgate` and `warpgate-firefox` use `network_mode: "service:vpn"`, so they
join the **VPN container's network namespace**. The tunnel interface lives in
that namespace and nowhere else, which is what keeps the host's routing table
untouched. Everything the two pods send goes out through the tunnel.

The VPN container also runs `socat`, forwarding TCP 443 on that namespace to
the corporate bastion address, so the pods can reach the OpenShift cluster
through the VPN on the usual HTTPS port.

## The files

| File | Purpose |
| --- | --- |
| `justfile` | Every task, wrapped up. `just` lists them. |
| `configure.py` | Writes the configuration and prepares warpgate's state. |
| `verify.sh` | Checks that the running stack really works. |
| `podman-compose.yml` | The real stack, using the corporate gateway. |
| `podman-compose.test.yml` | The same stack against a local OpenVPN server, for testing without corporate credentials. |
| `Dockerfile` | Builds `localhost/warpgate-vpn:latest` (openconnect, openvpn, socat). |
| `Dockerfile.firefox` | Builds `localhost/warpgate-firefox:latest` from the Kasm Firefox image. |
| `firefox-entrypoint.sh` | Entrypoint for the browser container, see "Fixes" below. |
| `vpn_entrypoint.sh` | Keeps the VPN up and runs the bastion forward. |
| `test/` | The local OpenVPN server used by the test harness. |
| `.env` | The settings. The only place a setting is written. |
| `secrets/` | The credentials, one file per secret. |
| `data/` | Warpgate's own state: database, TLS certificate, config. |

`.env`, `secrets/` and `data/` are in `.gitignore`. None of them belong in
version control.

## Credits and licences

This project is glue. It does not contain any of the interesting parts, and
it would not be worth much without these:

| What | Project | Licence |
| --- | --- | --- |
| The bastion | [Warpgate](https://github.com/warp-tech/warpgate) | Apache-2.0 |
| The browser container | [Kasm Workspaces images](https://github.com/kasmtech/workspaces-images) | MIT, for Kasm's own files |
| The VNC server in it | [KasmVNC](https://github.com/kasmtech/KasmVNC) | GPL-2.0 |
| The browser | [Mozilla Firefox](https://www.mozilla.org/firefox/) | MPL-2.0 |
| The VPN client | [openconnect](https://www.infradead.org/openconnect/) | LGPL-2.1-or-later |
| The tunnel script | [vpnc-script](https://github.com/NetworkManager/vpnc-script) | GPL-2.0-or-later |
| The forwarder | [socat](http://www.dest-unreach.org/socat/) | GPL-2.0-only |
| The test harness client | [OpenVPN](https://openvpn.net/) | GPL-2.0-only |
| The compose implementation | [podman-compose](https://github.com/containers/podman-compose) | GPL-2.0 |
| The task runner | [just](https://github.com/casey/just) | CC0-1.0 |

The base images are [Fedora](https://fedoraproject.org/) and
[Ubuntu](https://ubuntu.com/), which carry their own collection of licences.
The Kasm image is published under a licence that covers Kasm's own files in
that repository, and explicitly not the dependencies inside it.

The code in this repository is MIT licensed, see [LICENSE](LICENSE).

### Why MIT works here

Most of what runs inside the containers is GPL or LGPL licensed, and none of
it is modified. A copyleft licence does not spread to the code that merely
runs, links against or orchestrates it, so the glue around it can be
licensed however you like.

The one place where something is modified is `vnc_startup.sh` in the browser
image, which gets a single line changed so the browser keeps its route when
the container ends up with a different interface name. That file stays
GPL-2.0, it cannot be relicensed, and the terms of the GPL are met by
publishing the source of the change. `Dockerfile.firefox` is the complete
source of that modification, so this repository is where the source lives.

The rest of the packages come from the distribution's own repositories, and
their sources are published there. Nothing GPL is copied into this
repository's files.

## Requirements

- Podman and podman-compose
- Rootless Podman with a usable `newuidmap`/`newgidmap`, and your user
  listed in `/etc/subuid` and `/etc/subgid`
- Python 3, which is already there because podman-compose is a Python program
- `just`, for the task file
- SELinux enforcing is fine, the containers are labelled `spc_t`

## Setting it up

One command does the whole thing:

```bash
cd ~/Projects/warpgate
./configure.py
```

It asks for the handful of things it cannot guess, then writes:

| What | Where |
| --- | --- |
| the settings | `.env` |
| the corporate VPN password | `secrets/vpn_password` |
| the browser's VNC password | `secrets/vnc_password` |
| the browser's view-only VNC password | `secrets/vnc_viewonly_password` |
| the warpgate admin password | `secrets/admin_password` |
| warpgate's own state | `data/` |

Then start the stack:

```bash
podman-compose up -d
```

`podman-compose` reads `.env` on its own, so there is nothing else to pass.

### What it asks for

The questions are only the ones that cannot be worked out:

```
  VPN_CLIENT (VPN client to use: openconnect for a corporate gateway, ...) [openconnect]:
  VPN_USERNAME (Corporate VPN username):
  VPN_AUTHGROUP (Corporate VPN authentication group):
  VPN_GATEWAY (Corporate VPN gateway URL, for example https://vpn.example.com/):
  OCP_BASTION_IP (Address of the corporate resource reached through the tunnel):
  WARPGATE_EXTERNAL_HOST (Bare hostname warpgate is reached on, ...) [127.0.0.1]:
  The corporate VPN password:
  The warpgate admin password:
```

The corporate password is only ever typed in, never generated, because a made
up one would not authenticate and it would be confusing to print something
that looks like a real credential. The two VNC passwords are generated and
printed, since they are only for a local client.

Everything else keeps a sensible default, and anything that is already in
`.env` is left alone. So running it again only asks about what is missing.

### The other modes

| Command | What it does |
| --- | --- |
| `./configure.py` | set up or update, asking about what is missing |
| `./configure.py --check` | validate and change nothing |
| `./configure.py --print` | print the settings that are in effect |
| `./configure.py --reset` | throw away warpgate's state and generate it again |
| `./configure.py --yes` | never prompt, generate what it can |

### The settings

`.env` is the single source of truth. `podman-compose.yml` reads it, so a
value only has to be changed in one place. `./configure.py --print` shows what
is currently in effect, and `./configure.py --check` tells you whether the
configuration is complete without changing anything.

The settings fall into groups:

| Group | What it covers |
| --- | --- |
| `VPN_*` | the corporate gateway: client, username, auth group, gateway URL |
| `OCP_BASTION_*` | the corporate resource reached through the tunnel |
| `WARPGATE_*` | how warpgate advertises itself, and its listen ports |
| `PUBLISH_*` | the host ports the stack is published on |
| `BROWSER_*` | the browser container's hostname |

`.env.example` has the same keys with the defaults, and the comments say what
each one is for.

Two of them are worth a note:

- `WARPGATE_EXTERNAL_HOST` has to be a **bare hostname**, no scheme and no
  port. warpgate ignores anything else and warns about it on every start. The
  port goes in `WARPGATE_EXTERNAL_PORT`.
- `WARPGATE_EXTERNAL_PORT` is left empty on purpose, which makes it follow
  `PUBLISH_HTTPS_PORT`. Set it only when something sits in between, such as a
  reverse proxy.
- `OCP_BASTION_URL` is the name the corporate resource is reached by. A
  resource behind a corporate router usually answers to a name rather than to
  an address, so this is the name its certificate is for. `just verify` uses
  it to check that the resource really is reachable through the tunnel.

### Ports

| Address | What |
| --- | --- |
| `127.0.0.1:8443` | Warpgate web bastion, over TLS |
| `127.0.0.1:8888` | Warpgate web bastion, the same thing on another port |
| `127.0.0.1:2222` | Warpgate SSH bastion |
| `127.0.0.1:6901` | The browser's VNC endpoint |

Rootless Podman cannot bind a port below 1024, so the web bastion is published
on 8443 rather than 443. If you want it on 443:

```bash
echo 'net.ipv4.ip_unprivileged_port_start=443' | sudo tee /etc/sysctl.d/99-rootless-ports.conf
sudo sysctl --system
```

Then set `PUBLISH_HTTPS_PORT=443` in `.env` and run `./configure.py`.

### The credentials

The credentials are not in `.env`, they are in `secrets/`, one file per
secret, all mode 0600 inside a mode 0700 directory. Nothing is passed as an
environment variable, so the corporate password never shows up in
`podman inspect`, in `ps`, or in the logs.

```bash
install -m 0600 /dev/null secrets/vpn_password
printf '%s' 'THE-CORPORATE-PASSWORD' > secrets/vpn_password
```

Only the VPN container mounts `secrets/vpn_password`, read-only. The other
containers cannot see it at all.

### The admin password and the database

The admin password lives in two places: the secret file, and warpgate's
database. If the two drift apart the login quietly fails with a 401 and
nothing else, which is a confusing way to find out.

So the script keeps them together. It records a fingerprint of the password
in `secrets/admin_password.applied` when it applies it, and when it finds a
difference it puts it right:

```
Warpgate
  the image runs as uid 1000
  applying the admin password to the database...
  the admin password was applied to the database.
```

warpgate's `recover-access` insists on a terminal, so the script drives it
through a pty. It adds a new credential rather than replacing the old one,
which means applying it twice is harmless.

### The data directory

`data/` belongs to the user warpgate runs as inside the container, which
under rootless podman is a subuid on the host, and not to you. The script
asks the image which uid that is rather than assuming, and creates the
directory with that owner from inside the user namespace, which is the only
place where that uid is visible.

```
Warpgate
  the image runs as uid 1000
```

That is also why the same directory cannot simply be deleted with `rm -rf`
when it is owned by that uid. The script does it through `podman unshare`.


## Running it

Everything is wrapped up in a `justfile`. Run `just` on its own to see the
whole list.

It needs `just`, `podman`, `podman-compose` and Python 3. The last one is
already there, because `podman-compose` is a Python program.

| Command | What it does |
| --- | --- |
| `just configure` | ask for what is not known yet and write the configuration |
| `just check` | validate the configuration and change nothing |
| `just settings` | print the settings that are in effect |
| `just build` | build every image |
| `just up` / `just down` | start and stop the stack |
| `just restart` | stop and start it again |
| `just status` | what is running |
| `just logs` | follow the logs, or `just logs-vpn`, `just logs-bastion`, `just logs-browser` |
| `just verify` | check that the whole stack really works |
| `just admin` / `just vnc` | print a password |
| `just secrets` | where the credentials are and how they are protected |
| `just reset` | throw away warpgate's state and generate it again |
| `just clean` | stop everything and remove the containers, volumes and images |
| `just test-up` / `just test-down` / `just test-verify` | the same thing against a local OpenVPN server |
| `just debug-browser` | run the browser's startup in the foreground and keep the trace |

`just verify` is the interesting one. It checks facts about the running
stack rather than guessing:

```
  ok    vpn tunnel                           tun0  UNKNOWN  10.0.0.5/32
  ok    shared network namespace             net:[4026532859]
  ok    corporate resource via the tunnel    200
  ok    host has no tunnel                   0
  ok    host has no vpn routes               0
  ok    host still has internet              200
  ok    browser runs as kasm-user            kasm-user
  ok    admin can log in                     201
  ok    no database errors                   0

  17 checks passed
```

It exits non-zero when something is wrong, so it works as a smoke test.

The test harness runs the same stack against a local OpenVPN server that
stands in for the corporate gateway. It needs no corporate password and never
touches the corporate gateway.

```bash
just build        # once, for the test server image
just test-up
just test-verify
just test-down
```

The test server generates its own throwaway CA and certificates on first
start and serves a marker page on the far side of the tunnel, so the test
proves that traffic really flows through the tunnel rather than merely being
configured.

The test stack reads the same `.env` and the same secrets as the real one, so
it exercises the same code paths.

## Checking that the isolation holds

```bash
# The host has no tunnel interface.
ip -br addr | grep tun             # no output

# The host has no routes into the corporate network.
ip route | grep -E '10\.0\.0\.'   # no output

# The host still reaches the internet.
curl -s -o /dev/null -w '%{http_code}\n' https://example.com   # 200

# The pods do reach the corporate network.
podman exec warpgate-firefox curl -s --noproxy '*' http://127.0.0.1:443/
```

## Fixes that were needed to make this work

These were all found by running the stack and reading the container logs.
They are already applied, and are worth knowing about if the images are ever
rebuilt from scratch.

**SELinux blocked the tunnel device.** The host's `/dev/net/tun` is owned by
root, so a rootless container cannot open it unless SELinux allows it. The
containers are labelled `spc_t`, the "super privileged container" type that a
VPN container needs. Without it the VPN fails with:

```
mknod: /dev/net/tun: File exists
Failed to open tun device: Permission denied
```

**`userns: keep-id` is required.** Without it the container's root is an
unmapped subuid and the `TUNSETIFF` ioctl is rejected with `EPERM`.

**The browser container must not run as root.** The original
`Dockerfile.firefox` had `USER root` so that the `sed` patch could be
applied. That makes Firefox refuse to start, because `HOME` belongs to
`kasm-user`:

```
Running Firefox as root in a regular user's session is not supported.
```

The patch is now applied in a separate build stage, and the final image keeps
the original uid 1000.

**The browser container needs a TTY.** `kasmvncpasswd` calls `isatty` and
`tcgetattr`, so it fails without one:

```
getpassword error: Inappropriate ioctl for device
```

**The browser container needs its hostname resolvable.** The Kasm startup
script aborts if `hostname` cannot be resolved. A container that joins
another container's network namespace does not get its own hostname added to
`/etc/hosts` by Podman, so `firefox-entrypoint.sh` adds it before dropping
privileges. `/etc/hosts` is a root-owned tmpfs, so that has to happen while
still root.

**The Kasm image ships default VNC passwords.** The image has
`VNC_PW=vncpassword` and `VNC_VIEW_ONLY_PW=vncviewonlypassword` baked into
its environment, so a browser that is started without saying otherwise is
open to anything that can reach the port. `firefox-entrypoint.sh` reads both
from `secrets/` instead, and the compose file blanks the inherited values so
the image default cannot slip through.

**The published ports belong to the `vpn` service.** `warpgate` and
`firefox` share the VPN container's namespace, so any `ports:` for them would
not be published. The port list belongs on `vpn`.

**Rootless Podman cannot bind port 443.** Binding a privileged port needs
`net.ipv4.ip_unprivileged_port_start=443` in a sysctl file. The default here
is 8443 so the stack starts without any system changes.

**warpgate's state belongs to the container user.** The `warpgate` image runs
as uid 1000, which under rootless podman is a subuid on the host, so
`data/` is owned by that uid and not by you. `configure.py` creates it from
inside the user namespace and edits warpgate's configuration the same way,
which keeps the owner and the mode of the files as they are.

**The shared directories need a shared SELinux label.** `:Z` gives a container
its own private label, and Podman reassigns it to whichever container mounts
the directory last. With `data/` mounted by both the warpgate container and
`configure.py`, the second one relabelled it and the running container lost
access to the database:

```
ERROR warpgate_common_http::errors: Request failed with an internal error
  error=database error: Connection Error: (code: 14) unable to open database file
```

`:z` gives a shared label instead, which is what a directory that more than
one container mounts needs.

## Troubleshooting

**The VPN container logs `Failed to open tun device`.** The SELinux label or
the user namespace is wrong. Check that the service has both
`security_opt: label=type:spc_t` and `userns: keep-id`.

**The browser container restarts in a loop.** Check the last lines of its log:

```bash
podman logs --tail 20 warpgate-firefox
```

- `Running Firefox as root in a regular user's session is not supported`
  means the image is running as root. Rebuild `Dockerfile.firefox`.
- `hostname: Name or service not known` means the hostname is not resolvable.
  Rebuild the image so `firefox-entrypoint.sh` is in place.
- `getpassword error: Inappropriate ioctl for device` means the container has
  no TTY.

**The tunnel comes up but nothing is reachable.** Check that `warpgate` and
`firefox` really share the namespace:

```bash
for c in warpgate-vpn warpgate warpgate-firefox; do
    pid=$(podman inspect "$c" --format '{{.State.Pid}}')
    echo "$c $(readlink /proc/$pid/ns/net)"
done
```

All three must print the same `net:[...]`. If they differ, the pods were
started before the VPN container and are in a stale namespace. Recreate them
with `podman-compose up -d --force-recreate`.

**`./configure.py` says the configuration is not complete.** Run it without
`--yes` and answer the prompts, or fill the empty values in `.env` and run
`./configure.py --check`.

**The web bastion logs in but the session keeps dropping.** `external_host`
has to be a bare hostname and `http.external_port` has to be the port people
actually reach the bastion on. `./configure.py` sets both, and keeps them in
step with `PUBLISH_HTTPS_PORT`.

**The login returns 401 and the password is right.** The password in
`secrets/admin_password` and the one in warpgate's database have drifted
apart. `./configure.py` notices and puts it right. To do it by hand:

```bash
podman exec -it warpgate recover-access admin
```

**The warpgate container logs `unable to open database file`.** The SELinux
label on `data/` was reassigned. The mounts have to use `:z` and not `:Z`,
because more than one container mounts that directory. Restarting the stack
puts the label back.
