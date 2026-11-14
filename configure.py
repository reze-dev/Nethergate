#!/usr/bin/env python3
"""Set the warpgate stack up from a single configuration file.

The stack carries no corporate details inline any more. Everything lives in
one file, .env, and this script turns that file into a working stack:

    .env                    the settings
    secrets/vpn_password    the corporate VPN password
    secrets/vnc_password    the browser's VNC password
    secrets/admin_password  the warpgate admin password
    data/                   warpgate's own state, generated on first run

It is safe to run more than once. Existing secrets and warpgate's state are
left alone unless --reset is given.

Python rather than shell because podman-compose is a Python program, so the
interpreter is already on any machine that can run the stack, and because
editing structured files is what this does.

Usage:
    ./configure.py              set up or update the configuration
    ./configure.py --check      validate the configuration, change nothing
    ./configure.py --print      print the resolved configuration
    ./configure.py --reset      discard warpgate's state and regenerate it
    ./configure.py --help
"""

from __future__ import annotations

import argparse
import getpass
import os
import pwd
import re
import secrets as pysecrets
import shutil
import string
import subprocess
import sys
import tempfile
from pathlib import Path

PROJ_DIR = Path(__file__).resolve().parent
ENV_FILE = PROJ_DIR / ".env"
EXAMPLE_FILE = PROJ_DIR / ".env.example"
SECRETS_DIR = PROJ_DIR / "secrets"
DATA_DIR = PROJ_DIR / "data"

VPN_SECRET = SECRETS_DIR / "vpn_password"
VNC_SECRET = SECRETS_DIR / "vnc_password"
VNC_VIEW_ONLY_SECRET = SECRETS_DIR / "vnc_viewonly_password"
ADMIN_SECRET = SECRETS_DIR / "admin_password"

WARPGATE_IMAGE = os.environ.get("WARPGATE_IMAGE", "ghcr.io/warp-tech/warpgate:latest")

# The warpgate image runs as its own "warpgate" user. The script reads the
# uid out of the image rather than assuming it, and falls back to this one.
WARPGATE_UID = int(os.environ.get("WARPGATE_UID", "1000"))


class Setting:
    """One entry of the configuration."""

    def __init__(self, key, default, need, description, section="", prompt="never"):
        self.key = key
        self.default = default
        # "no", "yes" or "openconnect", which means only when the VPN client
        # is openconnect.
        self.need = need
        self.description = description
        self.section = section
        # "never", "always", "openconnect" or "openvpn". Whether the script
        # asks for this one when it has no value yet.
        self.prompt = prompt


SCHEMA = [
    Setting("VPN_CLIENT", "openconnect", "no",
            "VPN client to use: openconnect for a corporate gateway, openvpn for an OpenVPN server",
            "VPN", "always"),
    Setting("VPN_USERNAME", "", "openconnect",
            "Corporate VPN username", "VPN", "openconnect"),
    Setting("VPN_AUTHGROUP", "", "openconnect",
            "Corporate VPN authentication group", "VPN", "openconnect"),
    Setting("VPN_GATEWAY", "", "openconnect",
            "Corporate VPN gateway URL, for example https://vpn.example.com/", "VPN",
            "openconnect"),
    Setting("VPN_CONFIG", "", "no",
            "Path to an OpenVPN client config, only used when VPN_CLIENT=openvpn", "VPN",
            "openvpn"),
    Setting("OCP_BASTION_IP", "", "no",
            "Address of the corporate resource reached through the tunnel",
            "Corporate resource behind the tunnel", "always"),
    Setting("OCP_BASTION_PORT", "443", "no",
            "Port of the corporate resource reached through the tunnel",
            "Corporate resource behind the tunnel"),
    Setting("WARPGATE_EXTERNAL_HOST", "127.0.0.1", "no",
            "Bare hostname warpgate is reached on, no scheme and no port",
            "Warpgate bastion", "always"),
    Setting("WARPGATE_EXTERNAL_PORT", "", "no",
            "Port warpgate's web bastion is reached on, defaults to PUBLISH_HTTPS_PORT",
            "Warpgate bastion"),
    Setting("WARPGATE_HTTP_PORT", "8888", "no",
            "Port warpgate's web bastion listens on inside the pod", "Warpgate bastion"),
    Setting("WARPGATE_SSH_PORT", "2222", "no",
            "Port warpgate's SSH bastion listens on inside the pod", "Warpgate bastion"),
    Setting("PUBLISH_SSH_PORT", "2222", "no",
            "Host port published for the SSH bastion", "Ports published on the host"),
    Setting("PUBLISH_HTTP_PORT", "8888", "no",
            "Host port published for the web bastion", "Ports published on the host"),
    Setting("PUBLISH_HTTPS_PORT", "8443", "no",
            "Host port published for the web bastion over TLS",
            "Ports published on the host"),
    Setting("PUBLISH_VNC_PORT", "6901", "no",
            "Host port published for the browser's VNC", "Ports published on the host"),
    Setting("BROWSER_HOSTNAME", "warpgate-firefox", "no",
            "Hostname of the browser container", "Browser"),
]

SECTION_TITLES = {
    "VPN": "VPN",
    "Corporate resource behind the tunnel": "Corporate resource behind the tunnel",
    "Warpgate bastion": "Warpgate bastion",
    "Ports published on the host": "Ports published on the host",
    "Browser": "Browser",
}


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
def say(message=""):
    print(message)


def warn(message):
    print(message, file=sys.stderr)


def die(message, code=1):
    warn(f"configure.py: {message}")
    sys.exit(code)


# ---------------------------------------------------------------------------
# Reading and writing the settings file
# ---------------------------------------------------------------------------
def parse_env(text):
    """Reads KEY=VALUE lines. Anything else is ignored, so a malformed file
    cannot run code."""
    values = {}
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_env(path):
    if not path.is_file():
        return {}
    return parse_env(path.read_text(encoding="utf-8"))


def write_atomic(path, text, mode):
    handle, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def write_env_file(values, extras):
    # The external port is only written down when it is different from the
    # published one. Leaving it empty keeps it following the published port
    # when that changes later.
    if values.get("WARPGATE_EXTERNAL_PORT") == values.get("PUBLISH_HTTPS_PORT"):
        values = dict(values)
        values["WARPGATE_EXTERNAL_PORT"] = ""

    out = [
        "# Warpgate stack settings.",
        "#",
        "# This file is the single source of truth for the stack. It is read by",
        "# podman-compose directly, so the values here are the values the containers",
        "# get. Run ./configure.py to change them, or edit this file and run",
        "# podman-compose --env-file .env up -d.",
        "#",
        "# The credentials are not here. They live in secrets/, one file per secret.",
        "",
    ]

    current = None
    for setting in SCHEMA:
        if setting.section != current:
            current = setting.section
            out.append(f"# --- {SECTION_TITLES[current]} "
                       + "-" * max(0, 64 - len(current)) + "---")
        out.append(f"# {setting.description}")
        out.append(f"{setting.key}={values.get(setting.key, '')}")
        out.append("")

    for key in sorted(extras):
        out.append(f"{key}={extras[key]}")

    write_atomic(ENV_FILE, "\n".join(out).rstrip("\n") + "\n", 0o600)


def write_example_file():
    out = [
        "# Warpgate stack settings, as a template.",
        "#",
        "# Copy this to .env, fill in the values, and run ./configure.py --check.",
        "# ./configure.py writes a .env with the same keys and the same layout.",
        "",
    ]
    for setting in SCHEMA:
        out.append(f"# {setting.description}")
        out.append(f"{setting.key}={setting.default}")
        out.append("")
    write_atomic(EXAMPLE_FILE, "\n".join(out).rstrip("\n") + "\n", 0o644)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
PLACEHOLDER = re.compile(r"^<.*>$")
IPV4 = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$")
HOSTNAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.-]*$")


def is_set(value):
    return bool(value) and not PLACEHOLDER.match(value)


def validate(values):
    errors = []
    advisories = []

    for setting in SCHEMA:
        value = values.get(setting.key, "")
        needed = setting.need == "yes" or (
            setting.need == "openconnect" and values.get("VPN_CLIENT") == "openconnect")

        if needed and not is_set(value):
            errors.append(f"{setting.key} is not set")
        if PLACEHOLDER.match(value):
            errors.append(f"{setting.key} still holds the placeholder '{value}'")

    gateway = values.get("VPN_GATEWAY", "")
    if is_set(gateway) and not gateway.startswith(("http://", "https://")):
        errors.append(f"VPN_GATEWAY has to be an http:// or https:// URL, not '{gateway}'")

    client = values.get("VPN_CLIENT", "")
    if is_set(client) and client not in ("openconnect", "openvpn"):
        errors.append(f"VPN_CLIENT has to be openconnect or openvpn, not '{client}'")

    if client == "openvpn" and not is_set(values.get("VPN_CONFIG", "")):
        errors.append("VPN_CONFIG has to be set when VPN_CLIENT=openvpn")

    for key in ("OCP_BASTION_PORT", "WARPGATE_EXTERNAL_PORT", "WARPGATE_HTTP_PORT",
                "WARPGATE_SSH_PORT", "PUBLISH_SSH_PORT", "PUBLISH_HTTP_PORT",
                "PUBLISH_HTTPS_PORT", "PUBLISH_VNC_PORT"):
        value = values.get(key, "")
        if not value:
            continue
        if not value.isdigit() or not 1 <= int(value) <= 65535:
            errors.append(f"{key} has to be a port number between 1 and 65535, not '{value}'")

    bastion_ip = values.get("OCP_BASTION_IP", "")
    if is_set(bastion_ip) and not IPV4.match(bastion_ip):
        errors.append(f"OCP_BASTION_IP has to be an IPv4 address, not '{bastion_ip}'")

    # warpgate wants a bare hostname here. A scheme or a port makes it
    # ignore the value and warn about it on every start.
    host = values.get("WARPGATE_EXTERNAL_HOST", "")
    if is_set(host) and ("://" in host or "/" in host or ":" in host):
        errors.append(
            "WARPGATE_EXTERNAL_HOST has to be a bare hostname without a scheme or a "
            f"port, not '{host}'")

    browser = values.get("BROWSER_HOSTNAME", "")
    if is_set(browser) and not HOSTNAME.match(browser):
        errors.append(f"BROWSER_HOSTNAME is not a valid hostname, not '{browser}'")

    # Rootless Podman cannot bind a port below this one without a sysctl
    # change, so a published port under it stops the stack from starting.
    lowest = 1024
    sysctl = Path("/proc/sys/net/ipv4/ip_unprivileged_port_start")
    if sysctl.is_file():
        try:
            lowest = int(sysctl.read_text().strip())
        except ValueError:
            pass
    for key in ("PUBLISH_SSH_PORT", "PUBLISH_HTTP_PORT", "PUBLISH_HTTPS_PORT",
                "PUBLISH_VNC_PORT"):
        value = values.get(key, "")
        if value.isdigit() and int(value) < lowest:
            advisories.append(
                f"{key} is {value}, but rootless Podman cannot bind a port below "
                f"{lowest}, so the stack will not start. Either use a port of "
                f"{lowest} or above, or allow the low ports with:\n"
                f"    echo 'net.ipv4.ip_unprivileged_port_start={value}' | "
                f"sudo tee /etc/sysctl.d/99-rootless-ports.conf\n"
                f"    sudo sysctl --system")

    return errors, advisories


# ---------------------------------------------------------------------------
# Running podman
# ---------------------------------------------------------------------------
def run(argv, **kwargs):
    return subprocess.run(argv, check=False, **kwargs)


def can_unshare():
    return run(["podman", "unshare", "true"],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def host_uid_for(container_uid):
    """The host uid that container_uid maps to under rootless podman."""
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return None
    subuid = Path("/etc/subuid")
    if not subuid.is_file():
        return None
    for line in subuid.read_text().splitlines():
        name, _, rest = line.partition(":")
        if name != user:
            continue
        parts = rest.split(":")
        if len(parts) >= 2 and parts[0].isdigit():
            return int(parts[0]) + container_uid - 1
    return None


def ensure_data_dir():
    """The data directory belongs to the warpgate user inside the container,
    which is a subuid on the host. podman unshare is the only way to create
    it with that owner."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not can_unshare():
        return
    target = host_uid_for(WARPGATE_UID)
    if target is None:
        return
    run(["podman", "unshare", "chown", f"{WARPGATE_UID}:{WARPGATE_UID}", str(DATA_DIR)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
def random_password(length=20):
    alphabet = string.ascii_letters + string.digits
    return "".join(pysecrets.choice(alphabet) for _ in range(length))


def write_secret(path, value):
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


def ask(prompt, confirm=False):
    while True:
        try:
            value = getpass.getpass(prompt)
        except (EOFError, OSError) as error:
            warn(f"  could not read a value: {error}")
            return ""
        if not confirm:
            return value
        try:
            again = getpass.getpass("  again: ")
        except (EOFError, OSError):
            warn("  could not read the value again.")
            return ""
        if value == again:
            return value
        warn("  The two entries do not match.")


def ensure_secret(path, description, policy):
    """policy is "ask", "ask-required" or "generate".

    "ask-required" is for the corporate password. It can only be typed in,
    because a made up one would not authenticate, and it would be confusing
    to print one that looks like a real credential.

    The file has to exist even when it is empty. It is a bind mount source,
    and Podman makes a directory out of a missing one."""
    if not path.exists():
        write_secret(path, "")

    if path.stat().st_size > 0:
        say(f"  {description} is already set, leaving it alone.")
        return

    if policy == "generate":
        value = random_password()
        write_secret(path, value)
        say(f"  {description} was not set, so one was generated: {value}")
        return

    if args.yes:
        if policy == "ask-required":
            warn(f"  {description} is not set ({path}). Put it there before "
                 f"starting the stack.")
            return
        value = random_password()
        write_secret(path, value)
        say(f"  {description} was not set, so one was generated: {value}")
        return

    say(f"  {description} is not set yet.")
    value = ask(f"  {description}: ", confirm=True)
    if not value:
        warn("  left empty.")
        return
    write_secret(path, value)
    say(f"  written to {path}")


# ---------------------------------------------------------------------------
# warpgate's own state
# ---------------------------------------------------------------------------
def image_user_uid():
    """The uid the warpgate image runs as, read from the image itself.

    Under rootless podman that user is a subuid on the host, so the files it
    creates in data/ belong to that uid and not to you. The script asks the
    image who it is rather than assuming."""
    global WARPGATE_UID

    name = "warpgate-uid-probe"
    run(["podman", "rm", "-f", name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        result = run(["podman", "create", "--name", name, WARPGATE_IMAGE, "run"],
                     capture_output=True, text=True)
        if result.returncode != 0:
            return WARPGATE_UID
        result = run(["podman", "image", "inspect", WARPGATE_IMAGE,
                      "--format", "{{.Config.User}}"],
                     capture_output=True, text=True)
        wanted = result.stdout.strip() if result.returncode == 0 else ""
        if not wanted:
            return WARPGATE_UID
        if wanted.isdigit():
            WARPGATE_UID = int(wanted)
            return WARPGATE_UID

        with tempfile.NamedTemporaryFile() as tmp:
            result = run(["podman", "cp", f"{name}:/etc/passwd", tmp.name],
                         capture_output=True, text=True)
            if result.returncode != 0:
                return WARPGATE_UID
            content = Path(tmp.name).read_text(encoding="utf-8", errors="replace")
    finally:
        run(["podman", "rm", "-f", name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for line in content.splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[0] == wanted:
            try:
                WARPGATE_UID = int(parts[2])
            except ValueError:
                pass
            break
    return WARPGATE_UID


def ensure_data_dir():
    """The data directory belongs to the user warpgate runs as inside the
    container, which is a subuid on the host. podman unshare is the only way
    to create it with that owner."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not can_unshare():
        return None
    target = host_uid_for(WARPGATE_UID)
    if target is None:
        return None
    run(["podman", "unshare", "chown", f"{WARPGATE_UID}:{WARPGATE_UID}", str(DATA_DIR)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return target


def warpgate_container(command, admin_password=None, stdin=None):
    """Runs a warpgate command against data/.

    No --userns and no --user on purpose. The image's own user has to be the
    one that writes the files, and that user maps to the same host uid the
    pod's warpgate container uses."""
    argv = ["podman", "run", "--rm", "-v", f"{DATA_DIR}:/data:z"]
    if admin_password is not None:
        argv += ["-e", f"WARPGATE_ADMIN_PASSWORD={admin_password}"]
    argv += ["--entrypoint", "/usr/local/bin/warpgate", WARPGATE_IMAGE]
    argv += command
    return run(argv, stdin=stdin)


def init_warpgate_state(admin_password, values):
    """Generates warpgate's config, database, certificates and admin user.

    This is the same thing warpgate's own interactive setup does, with the
    answers the script already collected. --if-config-exists bootstrap makes
    it leave an existing database alone."""
    ensure_data_dir()

    if not can_unshare():
        warn("  podman unshare is not available, so warpgate's state cannot be "
             "prepared here.")
        return False

    say("  preparing warpgate's state, this takes a moment...")
    result = warpgate_container([
        "unattended-setup",
        "--data-path", "/data",
        "--http-port", values.get("WARPGATE_HTTP_PORT", "8888"),
        "--ssh-port", values.get("WARPGATE_SSH_PORT", "2222"),
        "--if-config-exists", "bootstrap",
    ], admin_password=admin_password)
    return result.returncode == 0


def patch_config_file(path, host, http_port, ssh_port):
    """Rewrites data/warpgate.yaml in place, setting the external address.

    The document is edited line by line so that everything else, including
    the comments and the order, is left as it was."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")

    top = None
    for i, line in enumerate(lines):
        if line.strip() and not line.lstrip().startswith("#") and indent_of(line) == 0:
            if re.match(r"^external_host:", line):
                top = i
                break
    if top is None:
        lines.insert(0, f"external_host: {host}")
        lines.insert(1, "")
    else:
        lines[top] = f"external_host: {host}"

    lines = set_in_section(lines, "http", "external_host", host)
    lines = set_in_section(lines, "http", "external_port", http_port)
    lines = set_in_section(lines, "ssh", "external_host", host)
    lines = set_in_section(lines, "ssh", "external_port", ssh_port)

    path.write_text("\n".join(lines), encoding="utf-8")


def patch_warpgate_config(values):
    """Points warpgate at the address the bastion is really reached on.

    data/warpgate.yaml belongs to the container user, so it is read and
    rewritten from inside the user namespace, which keeps the owner and the
    mode of the file as they are."""
    path = DATA_DIR / "warpgate.yaml"
    if not path.is_file():
        return False
    if not can_unshare():
        warn("  podman unshare is not available, leaving data/warpgate.yaml alone.")
        return False

    result = run([
        "podman", "unshare", "python3", str(Path(__file__).resolve()),
        "--patch-config",
        str(path),
        values.get("WARPGATE_EXTERNAL_HOST", ""),
        values.get("WARPGATE_EXTERNAL_PORT", ""),
        values.get("WARPGATE_SSH_PORT", ""),
    ])
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global args

    parser = argparse.ArgumentParser(
        description="Set the warpgate stack up from a single configuration file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
The settings live in .env next to this script. secrets/ holds the
credentials, and data/ holds warpgate's own state.
""")
    parser.add_argument("--check", action="store_true",
                        help="validate the configuration and change nothing")
    parser.add_argument("--print", action="store_true",
                        help="print the resolved configuration and exit")
    parser.add_argument("--reset", action="store_true",
                        help="discard warpgate's state and regenerate it")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="never prompt, fail if something is missing")
    parser.add_argument("--no-secrets", action="store_true",
                        help="leave secrets/ alone")
    parser.add_argument("--patch-config", nargs=4, help=argparse.SUPPRESS,
                        metavar=("PATH", "HOST", "HTTP_PORT", "SSH_PORT"))
    args = parser.parse_args()

    if args.patch_config:
        path, host, http_port, ssh_port = args.patch_config
        patch_config_file(path, host, http_port, ssh_port)
        return 0

    values = load_env(ENV_FILE)
    known = {s.key for s in SCHEMA}
    extras = {k: v for k, v in values.items() if k not in known}

    if args.print:
        for setting in SCHEMA:
            print(f"{setting.key:<24} {values.get(setting.key, '')}")
        for key in sorted(extras):
            print(f"{key:<24} {extras[key]}")
        return 0

    if not ENV_FILE.is_file():
        say(f"No {ENV_FILE.name} yet, starting from the example.")

    # Fill in what is missing. Only the things that are actually asked for
    # get a question, the rest keep their defaults.
    for setting in SCHEMA:
        if is_set(values.get(setting.key, "")):
            continue
        if args.yes or args.check:
            if setting.default:
                values.setdefault(setting.key, setting.default)
            continue

        if setting.prompt == "openconnect" and values.get("VPN_CLIENT") != "openconnect":
            continue
        if setting.prompt == "openvpn" and values.get("VPN_CLIENT") != "openvpn":
            continue
        if setting.prompt == "never":
            if setting.default:
                values.setdefault(setting.key, setting.default)
            continue

        if setting.default:
            prompt = f"  {setting.key} ({setting.description}) [{setting.default}]: "
        else:
            prompt = f"  {setting.key} ({setting.description}): "
        try:
            answer = input(prompt).strip()
        except EOFError:
            answer = ""
        if not sys.stdin.isatty():
            # Nothing echoes the answer when the input is piped, so put a
            # newline in to keep the prompts readable.
            print()
        values[setting.key] = answer or setting.default

    errors, advisories = validate(values)

    # The web bastion's external port follows the published HTTPS port, so
    # that warpgate hands out the address people actually reach it on. It is
    # only kept apart when it was set to something else on purpose.
    external_port = values.get("WARPGATE_EXTERNAL_PORT", "")
    if not is_set(external_port):
        if is_set(values.get("PUBLISH_HTTPS_PORT", "")):
            values["WARPGATE_EXTERNAL_PORT"] = values["PUBLISH_HTTPS_PORT"]
    elif is_set(values.get("PUBLISH_HTTPS_PORT", "")) \
            and external_port != values["PUBLISH_HTTPS_PORT"]:
        advisories.append(
            f"the web bastion is published on {values['PUBLISH_HTTPS_PORT']} but "
            f"warpgate will advertise {external_port}. Set WARPGATE_EXTERNAL_PORT "
            f"to the same port unless something sits in between, such as a reverse "
            f"proxy.")

    if errors:
        # Write what is known anyway, so there is a file to fill in.
        write_env_file(values, extras)
        write_example_file()
        say()
        say("The configuration is not complete:")
        for error in errors:
            say(f"  - {error}")
        say()
        say(f"Fill the empty values in {ENV_FILE.name}, then run ./configure.py "
            f"again to check it.")
        return 1

    if advisories:
        say()
        say("Worth knowing:")
        for advisory in advisories:
            say(f"  - {advisory}")

    if args.check:
        say()
        say("The configuration is complete.")
        say()
        for setting in SCHEMA:
            print(f"  {setting.key:<24} {values.get(setting.key, '')}")
        return 0

    # ---- write everything ----------------------------------------------
    say(f"Writing {ENV_FILE.name}")
    write_env_file(values, extras)
    write_example_file()

    if not args.no_secrets:
        say("Secrets")
        SECRETS_DIR.mkdir(parents=True, exist_ok=True)
        SECRETS_DIR.chmod(0o700)
        # The corporate password can only be typed in, never generated. The
        # admin password is asked for too, because it is typed into a
        # browser. The VNC passwords are for a local client, so a generated
        # one is fine.
        ensure_secret(VPN_SECRET, "The corporate VPN password", "ask-required")
        ensure_secret(VNC_SECRET, "The browser's VNC password", "generate")
        ensure_secret(VNC_VIEW_ONLY_SECRET, "The browser's view-only VNC password",
                      "generate")
        ensure_secret(ADMIN_SECRET, "The warpgate admin password", "ask")

    # ---- warpgate's own state ------------------------------------------
    say("Warpgate")
    uid = image_user_uid()
    say(f"  the image runs as uid {uid}")
    if args.reset:
        say("  discarding the old state")
        # The state belongs to the warpgate user inside the container, so it
        # can only be removed from inside the user namespace.
        if can_unshare():
            run(["podman", "unshare", "rm", "-rf", str(DATA_DIR)])
        else:
            shutil.rmtree(DATA_DIR, ignore_errors=True)
    ensure_data_dir()

    admin_password = ""
    if ADMIN_SECRET.is_file():
        admin_password = ADMIN_SECRET.read_text(encoding="utf-8")

    if not (DATA_DIR / "warpgate.yaml").is_file():
        if not admin_password:
            warn("  no admin password available, warpgate's state was not generated.")
            warn("  start the stack once and run ./configure.py again.")
        elif not init_warpgate_state(admin_password, values):
            warn("  warpgate's state could not be generated here.")
    else:
        say("  warpgate's state is already there, leaving it alone.")

    if (DATA_DIR / "warpgate.yaml").is_file():
        if patch_warpgate_config(values):
            say(f"  data/warpgate.yaml now points at "
                f"{values.get('WARPGATE_EXTERNAL_HOST')}:"
                f"{values.get('WARPGATE_EXTERNAL_PORT')}")
        else:
            warn("  data/warpgate.yaml could not be updated.")

    # ---- summary --------------------------------------------------------
    say()
    say("Done. The stack is configured.")
    say()
    say(f"  bastion web    https://{values.get('WARPGATE_EXTERNAL_HOST')}:"
        f"{values.get('WARPGATE_EXTERNAL_PORT')}")
    say(f"  bastion ssh    ssh -p {values.get('PUBLISH_SSH_PORT')} <user>@"
        f"{values.get('WARPGATE_EXTERNAL_HOST')}")
    say(f"  browser vnc    127.0.0.1:{values.get('PUBLISH_VNC_PORT')}")
    say(f"  admin password in {ADMIN_SECRET}")

    if VPN_SECRET.is_file() and VPN_SECRET.stat().st_size == 0:
        say()
        say("  The corporate VPN password is not set. The VPN container will not")
        say("  come up without it, and neither will anything that depends on it.")
        say(f"  Put it in {VPN_SECRET} and start the stack again.")

    say()
    say("Start it with:")
    say("  podman-compose up -d")
    return 0


if __name__ == "__main__":
    sys.exit(main())
