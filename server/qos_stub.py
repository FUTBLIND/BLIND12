"""
Stub for the EA DirtySDK QoS coordinator our preAuth response points at
(QOSS/BWPS = 127.0.0.1:17502). Real request captured from the live client:

  GET /qos/qos?vers=1&qtyp=1&prpt=3659 HTTP/1.1
  Host: 127.0.0.1:17502
  Content-Length: 0
  User-Agent: ProtoHttp 1.3/DS 8.8.6.0
  Accept: */*

`prpt` is the UDP port the client wants a probe packet sent back to for
round-trip timing.

Response format was recovered from the client's own parser (FUN_00c953f0 in
fifa.exe): DirtySDK TagField text, i.e. space-separated `key=value` pairs with
dotted compound names. The parser looks for a `qos`, `firewall` or `firetype`
section and then reads sub-keys relative to it.

It rejects the response (returns -1) unless:
    qos.qosport   != 0
    qos.requestid != 0
    and, when in probe mode, qos.probesize != 0 and qos.numprobes >= 2
An empty 200 OK therefore always failed, which is why the client retried.
"""
import re
import socket
import threading

# Force the stdlib's lazy imports before any thread starts - see prewarm.py.
# On the portable build the stdlib is a zip, and two threads importing the
# same module for the first time race; the loser gets a LookupError or an
# ImportError far from the real cause. Measured, not theoretical.
import prewarm  # noqa: F401  (imported for its side effect)

LISTEN_PORT = 17502
PRPT_RE = re.compile(rb"prpt=(\d+)")

LOOPBACK_IP_INT = 0x7F000001  # 127.0.0.1 - loopback only, nothing routable
CLIENT_PROBE_PORT = 3659

# /qos/qos - probe parameters. numprobes must be >= 2 and probesize non-zero.
# The client only ever fetches /qos/qos - the firewall and firetype endpoints
# below are never requested - so the address-discovery fields have to be in
# THIS body or the client never learns its external address. Its parser reads
# the suffixes  .qosport .probesize .numprobes .firetype .reqsecret .requestid
# .ports.ports .ips.ips  (strings lifted straight out of fifa.exe).
#
# Without them the client reported EXIP=0/PORT=0 and NQOS NATT=4 (UNKNOWN) in
# UpdateNetworkInfo, which surfaces as "firewall is restrictive" and makes
# Ultimate Team refuse to start. firetype=1 is open/unrestricted.
QOS_BODY = (
    f"qos.qosport={LISTEN_PORT} "
    f"qos.probesize=1200 "
    f"qos.numprobes=4 "
    f"qos.requestid=1 "
    f"qos.reqsecret=1 "
    f"qos.numinterfaces=1 "
    f"qos.ips.ips={LOOPBACK_IP_INT} "
    f"qos.ports.ports={CLIENT_PROBE_PORT} "
    f"qos.firetype=1"
)

# /qos/firewall - the address the coordinator "observed". Loopback by design:
# there are no remote peers here and a real IP would be pointless and exposing.
FIREWALL_BODY = (
    f"firewall.numinterfaces=1 "
    f"firewall.ips.ips={LOOPBACK_IP_INT} "
    f"firewall.ports.ports={CLIENT_PROBE_PORT} "
    f"firewall.requestid=1 "
    f"firewall.reqsecret=1"
)

# /qos/firetype - NAT/firewall classification. 1 = open/unrestricted.
FIRETYPE_BODY = "firetype.firetype=1"


def body_for_path(path: bytes) -> str:
    if b"/qos/firewall" in path:
        return FIREWALL_BODY
    if b"/qos/firetype" in path:
        return FIRETYPE_BODY
    return QOS_BODY


PROBE_SIZE = 1200
NUM_PROBES = 4


def send_udp_probe(port):
    """Send the probe burst the client is actually waiting for.

    `prpt` in the request is the port the client wants probed, and the body we
    return advertises probesize=1200 / numprobes=4. We were sending ONE 8-byte
    packet, so the client never got the burst it had been told to expect, gave
    up on address discovery, and reported EXIP 0.0.0.0:0 with NQOS NATT=4
    (UNKNOWN) in UpdateNetworkInfo. That is what the UI shows as "firewall is
    restrictive", and Ultimate Team refuses to run on an unknown NAT.

    Send exactly what we advertised: NUM_PROBES packets of PROBE_SIZE bytes,
    each carrying its index so the client can order/count them.
    """
    try:
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Probes should appear to come from the QoS port the client was told
        # about. The listener thread already holds it, so ask to share it and
        # fall back to an ephemeral source port rather than failing outright.
        try:
            udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            udp.bind(("127.0.0.1", LISTEN_PORT))
        except OSError:
            pass
        for i in range(NUM_PROBES):
            pkt = i.to_bytes(4, "big") + bytes(PROBE_SIZE - 4)
            udp.sendto(pkt, ("127.0.0.1", port))
        udp.close()
        print(f"[UDP-PROBE] Sent {NUM_PROBES} x {PROBE_SIZE}B probes to 127.0.0.1:{port}")
    except OSError as e:
        print(f"[UDP-PROBE] Failed to send to port {port}: {e}")


def udp_listen():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", LISTEN_PORT))
    print(f"[UDP] Listening on 127.0.0.1:{LISTEN_PORT}")
    while True:
        data, addr = sock.recvfrom(4096)
        print(f"[UDP] Received {len(data)} bytes from {addr}: {data[:100]!r}")
        sock.sendto(data, addr)
        print(f"[UDP] Echoed {len(data)} bytes back to {addr}")


def tcp_listen():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", LISTEN_PORT))
    sock.listen(5)
    print(f"[TCP] Listening on 127.0.0.1:{LISTEN_PORT}")
    while True:
        conn, addr = sock.accept()
        print(f"[TCP] Connection from {addr}")
        t = threading.Thread(target=tcp_handle, args=(conn, addr), daemon=True)
        t.start()


def tcp_handle(conn, addr):
    try:
        data = conn.recv(4096)
        if not data:
            return
        print(f"[TCP] Received {len(data)} bytes from {addr}: {data!r}")

        m = PRPT_RE.search(data)
        if m:
            prpt = int(m.group(1))
            send_udp_probe(prpt)
        else:
            print("[TCP] No prpt= found in request")

        body = body_for_path(data.split(b"\r\n", 1)[0]).encode("ascii")
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        conn.sendall(response)
        print(f"[TCP] Sent 200 OK, body: {body.decode('ascii')}")
    except (ConnectionResetError, ConnectionAbortedError) as e:
        print(f"[TCP] {addr} error: {e}")
    finally:
        conn.close()


def main():
    threading.Thread(target=udp_listen, daemon=True).start()
    threading.Thread(target=tcp_listen, daemon=True).start()
    print("QoS stub running on UDP+TCP 17502. Ctrl+C to stop.")
    threading.Event().wait()


if __name__ == "__main__":
    main()
