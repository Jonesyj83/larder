"""Checks for addresses the server is about to fetch from on someone's behalf,
so a url typed into a form can't be used to reach the cloud metadata service
or other things on the server's own network."""
import ipaddress
import socket


def _addresses(host, port):
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        return [ipaddress.ip_address("127.0.0.1")]
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []  # doesn't resolve; the request itself will fail
    out = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(info[4][0].split("%")[0]))
        except ValueError:
            pass
    return out


def _never(ip):
    # link-local covers 169.254.169.254 and friends; nothing legitimate lives there
    return ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved


def _local(ip):
    return ip.is_loopback or ip.is_private or not ip.is_global


def check_host(host, port, allow_local=False):
    """Raise ValueError if host is somewhere we shouldn't send requests.
    allow_local lets loopback and LAN addresses through (a local model server)."""
    if not host:
        raise ValueError("Missing host")
    for ip in _addresses(host.strip("[]"), port):
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if _never(ip) or (not allow_local and _local(ip)):
            raise ValueError("That address points inside the network")


def is_local(host, port):
    """True if every address host resolves to is loopback or LAN."""
    found = _addresses(host.strip("[]"), port)
    return bool(found) and all(_local(ip) for ip in found)
