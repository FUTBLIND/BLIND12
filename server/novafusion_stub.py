"""
HTTP stub for proxy.novafusion.ea.com (port 80).

The hosts file already redirects proxy.novafusion.ea.com -> 127.0.0.1, but
nothing was ever listening there, so every request was connection-refused.
EA Core logs that as:

    Core::CoreNetworkTransaction::PerformTransProc
    "Couldn't Send Internet Request! Error [12029]"

12029 = ERROR_INTERNET_CANNOT_CONNECT (WinINet), raised by HttpSendRequest.
It appears in C:\\shared.log on every single boot.

Known paths referenced in EACore.dll:
    /redirect/access/activation
    /redirect/access/forgotpassword
    /redirect/access/help-iso
    /redirect/access/registration3
    /redirect/access/store
    /redirect/access/welcome

We do not know the expected response bodies yet - nothing has ever reached
this endpoint to learn from. So: log every request in full, and answer with
a valid, empty 200 so the transaction completes instead of failing to
connect. Refine once the logs show what is actually asked for.
"""
import datetime
import http.server
import socketserver
import ssl

# THIS STUB IS THE ONE THAT PROVED prewarm NECESSARY. It binds six ports from
# six threads at the bottom of this file, and HTTPServer.server_bind calls
# socket.getfqdn, which encodes with the 'idna' codec. On the portable build
# that codec import raced and lost, and the port silently never bound. The
# measurements and the whole explanation are in prewarm.py, imported above.
import threading

# Force the stdlib's lazy imports before any thread starts - see prewarm.py.
# On the portable build the stdlib is a zip, and two threads importing the
# same module for the first time race; the loser gets a LookupError or an
# ImportError far from the real cause. Measured, not theoretical.
import prewarm  # noqa: F401  (imported for its side effect)

# The hosts file sends several EA names here, but they are not all on port 80.
# Redirecting the name is not enough on its own - if nothing is listening on
# the port the client still gets connection-refused, which is the same
# WinINet 12029 we are trying to remove.
#
#   80    paceap.com, www.paceap.com, proxy.novafusion.ea.com,
#         elephant.online.ea.com
#   8080  content.lt.easfc.ea.com      (EASFC content)
#   8099  easw.easports.com            (EA Sports World / Football Club)
#   9421  EACore.dll -> /api?function=ping                  (its own localhost
#   9422  EACore.dll -> flashGetPolicyFile / flashGetRSKey   API - hard-coded
#   9423  EACore.dll -> flashAuthenticate                    127.0.0.1, never
#                                                            had a listener)
# 8099 is easw.easports.com - FUT's OWN web backend (the DLL has that host
# baked in). It is served by fut_rs4_stub.py, which speaks the ut12 REST
# API; a generic catch-all answer there would fail FUT's login.
PLAIN_PORTS = [80, 8080, 9421, 9422, 9423]

# These two are reached over HTTPS, so a plain-HTTP listener is not enough -
# the TLS handshake has to complete before any request is sent.
#   443   profile.ea.com, reports.tools.gos.ea.com, client.akamai.com
# We present the project's existing self-signed cert. The client may still
# reject it as untrusted, but that is a different (and visible) failure from
# "connection refused", and the request line gets logged either way.
TLS_PORTS = [443]
# NOT server.crt/server.key - those are deliberately tiny for the game's
# ancient SSLv3 Blaze stack and modern OpenSSL rejects them ("ee key too
# small"). This is a separate 2048-bit cert naming the three HTTPS hosts.
CERT_FILE = "stub_https.crt"
KEY_FILE = "stub_https.key"


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _log(self, method):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        print(f"\n[{ts}] === NOVAFUSION {method} {self.path} ===", flush=True)
        for k, v in self.headers.items():
            print(f"    {k}: {v}", flush=True)
        if body:
            print(f"    --- body ({len(body)} bytes) ---", flush=True)
            print("    " + body.decode("utf-8", errors="replace"), flush=True)
        return body

    def _respond(self, payload=b"", ctype="text/html; charset=utf-8"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def do_GET(self):
        self._log("GET")
        self._respond(b"<html><body></body></html>")

    def do_POST(self):
        self._log("POST")
        self._respond(b"<html><body></body></html>")

    def do_HEAD(self):
        self._log("HEAD")
        self._respond()

    def log_message(self, fmt, *args):
        pass  # our own _log is more detailed


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(port, tls=False):
    try:
        httpd = ThreadedHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"  port {port}: FAILED to bind ({e})", flush=True)
        return
    if tls:
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=CERT_FILE, keyfile=KEY_FILE)
            # this is a stand-in for dead EA hosts on loopback, so accept
            # whatever the ageing client offers rather than refusing it
            ctx.minimum_version = ssl.TLSVersion.TLSv1
            ctx.set_ciphers("ALL:@SECLEVEL=0")
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        except Exception as e:
            print(f"  port {port}: TLS setup failed ({e})", flush=True)
            return
    print(f"  port {port}: listening{' (TLS)' if tls else ''}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    print("EA HTTP/HTTPS catch-all stub", flush=True)
    jobs = [(p, False) for p in PLAIN_PORTS] + [(p, True) for p in TLS_PORTS]
    for port, tls in jobs[:-1]:
        threading.Thread(target=serve, args=(port, tls), daemon=True).start()
    serve(*jobs[-1])
