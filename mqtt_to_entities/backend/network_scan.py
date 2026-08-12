"""Discovery of MQTT brokers on the local network.

Deliberately does not shell out to nmap: a plain TCP connect is all a broker
scan needs, and nmap would mean +30MB in the image plus NET_RAW/host_network
privileges the add-on has no other reason to hold.

Instead of only reporting "port open", each responding host gets a real MQTT
CONNECT packet. That distinguishes an actual broker from anything else sitting
on 1883, and the CONNACK return code tells us whether it wants credentials --
which is what the user actually needs to know before filling in the form.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import struct

logger = logging.getLogger("mqtt_to_entities.network_scan")

# The two IANA-registered MQTT ports: plain and TLS.
DEFAULT_PORTS = (1883, 8883)

# A scan of a /24 across 2 ports is 508 connections; this cap keeps that from
# exhausting file descriptors while still finishing in a couple of seconds.
MAX_CONCURRENCY = 256

# Hosts that are up reply well within this; anything slower is not worth the
# wait when sweeping 254 addresses.
CONNECT_TIMEOUT = 1.0
# The broker gets a bit longer to answer CONNECT, since it may check an ACL.
MQTT_PROBE_TIMEOUT = 2.0

# Refuse to sweep anything larger than a /22 (1022 hosts). A /16 would be 65k
# hosts x 2 ports and reads as a network-wide port sweep, which is not what this
# feature is for.
MAX_SCAN_HOSTS = 1024

CONNACK_MESSAGES = {
    0: "acepta conexiones anónimas",
    1: "versión de protocolo no soportada",
    2: "client id rechazado",
    3: "servicio no disponible",
    4: "requiere usuario y contraseña",
    5: "no autorizado (requiere credenciales)",
}


class ScanError(Exception):
    """The requested scan cannot be performed (bad or oversized range)."""


# Home Assistant puts add-on containers on this internal Docker network. An
# address in here tells us nothing about the user's LAN, so it must not be
# offered as the default scan range.
HASSIO_DOCKER_NET = ipaddress.ip_network("172.30.32.0/23")

# Offered when the add-on can only see its own Docker network: the ranges home
# routers actually hand out, most common first.
COMMON_LAN_RANGES = ("192.168.1.0/24", "192.168.0.0/24", "192.168.6.0/24", "10.0.0.0/24")

# ipaddress reports these as private, but they are documentation/benchmark
# blocks -- never a real LAN, so they are not worth scanning.
DOC_AND_BENCH_NETS = (
    "192.0.2.0/24",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "198.18.0.0/15",
)


def _own_ip() -> str | None:
    """The address the container would use for outbound traffic.

    Uses a UDP socket purely to consult the routing table; no packet is sent.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def local_cidr() -> str | None:
    """Best guess at the LAN /24 to scan, or None if it can't be determined.

    Returns None when the only address we have is on Home Assistant's internal
    Docker network: scanning 172.30.32.x would sweep the other add-ons instead
    of the user's network and find nothing useful. Getting the real LAN range
    would require host_network, which lowers the add-on's security rating for
    little gain -- suggesting the usual ranges is a better trade.
    """
    local_ip = _own_ip()
    if local_ip is None:
        return None

    try:
        address = ipaddress.ip_address(local_ip)
    except ValueError:
        return None

    if address in HASSIO_DOCKER_NET or address.is_loopback:
        return None

    try:
        return str(ipaddress.ip_network(f"{local_ip}/24", strict=False))
    except ValueError:
        return None


def cidr_from_address(address: str | None) -> str | None:
    """The /24 around a plain IPv4 address, or None if it isn't a usable one.

    Used with the address the browser is talking to Home Assistant on: the
    add-on itself only sees Docker's internal network, but the request's Host
    header carries the user's real LAN address, which is a far better guess than
    any hardcoded default.
    """
    if not address:
        return None

    # Strip a port and any IPv6 brackets before parsing.
    host = address.strip().rsplit(":", 1)[0] if address.count(":") == 1 else address.strip()
    host = host.strip("[]")

    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        return None  # a hostname, not an address

    if parsed.version != 4 or parsed.is_loopback or parsed in HASSIO_DOCKER_NET:
        return None
    if not parsed.is_private or parsed.is_reserved or parsed.is_link_local:
        # Never sweep a public range. Note ipaddress treats the documentation
        # blocks (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24) as private, so
        # they are excluded explicitly rather than by is_private alone.
        return None
    if any(parsed in ipaddress.ip_network(net) for net in DOC_AND_BENCH_NETS):
        return None

    try:
        return str(ipaddress.ip_network(f"{host}/24", strict=False))
    except ValueError:
        return None


def suggested_ranges(client_address: str | None = None) -> list[str]:
    """Ranges to offer in the UI, best guess first.

    Order: the LAN the browser is on, then anything the add-on could detect
    itself, then the ranges home routers typically use.
    """
    ordered: list[str] = []
    for candidate in (cidr_from_address(client_address), local_cidr(), *COMMON_LAN_RANGES):
        if candidate and candidate not in ordered:
            ordered.append(candidate)
    return ordered


def _hosts_to_scan(cidr: str) -> list[str]:
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError as exc:
        raise ScanError(f"Rango inválido: {exc}") from exc

    if network.version != 4:
        raise ScanError("Solo se admiten rangos IPv4")

    hosts = [str(h) for h in network.hosts()] or [str(network.network_address)]
    if len(hosts) > MAX_SCAN_HOSTS:
        raise ScanError(
            f"El rango tiene {len(hosts)} direcciones; el máximo es {MAX_SCAN_HOSTS} "
            f"(una /22). Usá un rango más chico, por ejemplo una /24."
        )
    return hosts


def _connect_packet() -> bytes:
    """Minimal MQTT 3.1.1 CONNECT with a clean session and no credentials."""
    client_id = b"mqtt_to_entities_scan"
    payload = struct.pack("!H", len(client_id)) + client_id
    # Protocol name "MQTT", level 4, clean-session flag, 30s keepalive.
    variable_header = struct.pack("!H", 4) + b"MQTT" + bytes([4, 0x02]) + struct.pack("!H", 30)
    body = variable_header + payload

    # Remaining Length uses the variable-length scheme; a CONNECT this small is
    # always well under 128 bytes, so one byte is enough.
    assert len(body) < 128
    return bytes([0x10, len(body)]) + body


async def _probe(host: str, port: int, semaphore: asyncio.Semaphore) -> dict | None:
    """Return broker info if host:port speaks MQTT, else None."""
    async with semaphore:
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=CONNECT_TIMEOUT
            )
        except (OSError, asyncio.TimeoutError):
            return None  # closed, filtered, or host down

        try:
            # Port 8883 is TLS: a plaintext CONNECT would just hang or be
            # rejected, so an open port is all we can report without a
            # handshake. That is still useful -- the user knows to tick TLS.
            if port == 8883:
                return {
                    "host": host,
                    "port": port,
                    "tls": True,
                    "confirmed_mqtt": False,
                    "detail": "puerto TLS abierto (no verificado)",
                }

            writer.write(_connect_packet())
            await asyncio.wait_for(writer.drain(), timeout=MQTT_PROBE_TIMEOUT)
            response = await asyncio.wait_for(reader.read(4), timeout=MQTT_PROBE_TIMEOUT)

            # A CONNACK is 0x20 0x02 <flags> <return code>. Anything else means
            # something is listening, but it is not an MQTT broker.
            if len(response) >= 4 and response[0] == 0x20:
                code = response[3]
                return {
                    "host": host,
                    "port": port,
                    "tls": False,
                    "confirmed_mqtt": True,
                    "requires_auth": code in (4, 5),
                    "detail": CONNACK_MESSAGES.get(code, f"CONNACK rc={code}"),
                }
            return {
                "host": host,
                "port": port,
                "tls": False,
                "confirmed_mqtt": False,
                "detail": "puerto abierto, no responde MQTT",
            }
        except (OSError, asyncio.TimeoutError):
            return {
                "host": host,
                "port": port,
                "tls": False,
                "confirmed_mqtt": False,
                "detail": "puerto abierto, sin respuesta",
            }
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except (OSError, asyncio.TimeoutError):
                    pass


async def scan(cidr: str, ports: tuple[int, ...] = DEFAULT_PORTS) -> dict:
    """Sweep a CIDR range for MQTT brokers.

    Raises ScanError for an invalid or oversized range.
    """
    hosts = _hosts_to_scan(cidr)
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    logger.info("Escaneando %s (%d hosts) en los puertos %s", cidr, len(hosts), ports)
    results = await asyncio.gather(
        *(_probe(host, port, semaphore) for host in hosts for port in ports)
    )

    found = [r for r in results if r is not None]
    # Confirmed brokers first, then by address so the list is stable.
    found.sort(
        key=lambda r: (
            not r.get("confirmed_mqtt"),
            ipaddress.ip_address(r["host"]),
            r["port"],
        )
    )
    logger.info("Escaneo de %s terminado: %d resultado(s)", cidr, len(found))
    return {"range": cidr, "hosts_scanned": len(hosts), "results": found}
