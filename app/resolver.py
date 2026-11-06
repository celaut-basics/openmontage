"""Give the guest a DNS resolver, because under nodo nothing else does.

Run once at start, as root, before the entrypoint drops privileges (the approach of
celaut-basics/yt-transcript, service/resolver.py). nodo serves no DNS and writes no
/etc/resolv.conf into the guest; the network `*` that the service declares opens UDP and
TCP 53, so a public resolver is reachable. The rule:

- `OPENMONTAGE_DNS_SERVERS` set (space or comma separated): write those servers.
- Not set, and `/__config__` exists (a nodo guest): write DEFAULT_DNS_SERVERS.
- Not set, and no `/__config__` (Docker, a test): change nothing.
"""

import ipaddress
import os
import re
import sys

RESOLV_CONF = "/etc/resolv.conf"
NODE_CONFIG = "/__config__"
DEFAULT_DNS_SERVERS = ("1.1.1.1", "9.9.9.9", "8.8.8.8")


def servers_of(env):
    text = env.get("OPENMONTAGE_DNS_SERVERS", "")
    servers = tuple(s for s in re.split(r"[\s,]+", text) if s)
    for server in servers:
        ipaddress.ip_address(server)  # ValueError for a name or garbage
    return servers


def main(env=None, path=RESOLV_CONF, node_config=NODE_CONFIG):
    env = dict(os.environ if env is None else env)
    try:
        servers = servers_of(env) or (DEFAULT_DNS_SERVERS if os.path.isfile(node_config) else ())
    except ValueError as error:
        print("resolver: OPENMONTAGE_DNS_SERVERS: %s" % error, file=sys.stderr)
        return 1
    if not servers:
        return 0
    text = "# Written by openmontage at start (app/resolver.py).\n"
    text += "".join("nameserver %s\n" % s for s in servers[:3])
    text += "options timeout:2 attempts:2\n"
    if os.path.islink(path):
        os.unlink(path)
    with open(path, "w", encoding="ascii") as handle:
        handle.write(text)
    os.chmod(path, 0o644)
    print("resolver: %s" % " ".join(servers[:3]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
