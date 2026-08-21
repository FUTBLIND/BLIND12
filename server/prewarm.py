# -*- coding: utf-8 -*-
r"""Force the stdlib's lazy imports NOW, on one thread, before the servers start.

WHY THIS EXISTS - measured, not theoretical.

The portable build ships an embeddable Python whose entire standard library
lives inside `python37.zip`. That is what keeps the runtime to one file and
6 MB, and it is fine for ordinary use. It is NOT fine for a module that is
imported for the first time by two threads at once: the loser of that race does
not block and retry, it FAILS, and the error names something that looks
unrelated to threading.

Measured on the portable build, 2026-08-21. `novafusion_stub.py` binds six
ports from six threads; `http.server.HTTPServer.server_bind` calls
`socket.getfqdn`, which encodes with the `idna` codec; and the codec is
imported on first use:

    8 threads calling getfqdn cold  ->  3 to 5 failed, on EVERY run of 3
    LookupError: unknown encoding: idna
    -> the port never binds, and the stub looks like it started fine

With the codec imported once beforehand: 5 runs of 5, zero failures.

IT CANNOT HAPPEN ON A DEVELOPMENT MACHINE. An installed Python has a loose
stdlib on disk, where the same race is harmless. So this bug is invisible here
and certain there, which is exactly the class of fault the portable build has
to defend against by construction rather than by testing.

`idna` was the one that bit. It is not the only lazy import a stub can reach
from a thread, so the whole set is warmed here rather than the single culprit,
and every stub imports this module first. Importing it twice is free.

Nothing here raises: a stdlib module that cannot be found is a broken runtime
and the stub's own failure will say so far more clearly than an ImportError
from a helper. `report()` says what actually warmed, for the build's checks.
"""
import codecs

# Modules the stdlib imports lazily, on paths the stubs really take.
#
#   encodings.idna  socket.getfqdn -> hostname encoding. THE MEASURED ONE.
#   stringprep      imported by encodings.idna
#   unicodedata     imported by stringprep, and it is a .pyd, not pure Python
#   mimetypes       http.server.guess_type, which also reads the registry
#   encodings.*     the codecs socket and http.server resolve by name
_MODULES = (
    "encodings.idna",
    "stringprep",
    "unicodedata",
    "mimetypes",
    "encodings.ascii",
    "encodings.latin_1",
    "encodings.utf_8",
    "encodings.idna",
)

# Codec NAMES, looked up through the registry so the alias cache is populated
# too - a lookup by name is what actually fails in the race, not the import.
_CODECS = ("idna", "ascii", "latin-1", "utf-8")

_WARMED = []
_FAILED = []


def _warm():
    import importlib
    for name in _MODULES:
        if name in _WARMED:
            continue
        try:
            importlib.import_module(name)
            _WARMED.append(name)
        except Exception as e:                       # noqa: BLE001
            _FAILED.append("%s (%s)" % (name, e))
    for name in _CODECS:
        try:
            codecs.lookup(name)
        except Exception as e:                       # noqa: BLE001
            _FAILED.append("codec %s (%s)" % (name, e))


_warm()


def report():
    """(warmed, failed) - for the build's verification, not for the stubs."""
    return list(_WARMED), list(_FAILED)


if __name__ == "__main__":
    ok, bad = report()
    print("prewarm: %d module(s) warmed" % len(ok))
    for m in ok:
        print("   %s" % m)
    if bad:
        print("prewarm: %d FAILED" % len(bad))
        for m in bad:
            print("   %s" % m)
