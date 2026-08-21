"""
Self-test SSLv3 client, using the same FIFA 12 bundled OpenSSL 0.9.8l DLLs,
to verify server_sslv3.py actually completes a handshake before pointing
the real game client at it.

Run with: py -3.7-32 client_sslv3_test.py
"""
import ctypes
import os
import socket
import sys

import gamepath
# GAME PATH: resolved, not hardcoded. gamepath.py checks FUT12_GAME_DIR,
# gamepath.txt, the uninstall registry and the standard install locations,
# so this runs on a machine where FIFA 12 is not where it is here.
DLL_DIR = gamepath.core_dir()
TARGET = ("127.0.0.1", 42127)

os.environ["PATH"] = DLL_DIR + os.pathsep + os.environ["PATH"]
libeay = ctypes.CDLL(os.path.join(DLL_DIR, "libeay32.dll"))
ssleay = ctypes.CDLL(os.path.join(DLL_DIR, "ssleay32.dll"))

ssleay.SSL_library_init.restype = ctypes.c_int
ssleay.SSL_library_init.argtypes = []
ssleay.SSL_load_error_strings.restype = None
ssleay.SSL_load_error_strings.argtypes = []
ssleay.SSLv3_client_method.restype = ctypes.c_void_p
ssleay.SSLv3_client_method.argtypes = []
ssleay.SSL_CTX_new.restype = ctypes.c_void_p
ssleay.SSL_CTX_new.argtypes = [ctypes.c_void_p]
ssleay.SSL_CTX_set_cipher_list.restype = ctypes.c_int
ssleay.SSL_CTX_set_cipher_list.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
ssleay.SSL_new.restype = ctypes.c_void_p
ssleay.SSL_new.argtypes = [ctypes.c_void_p]
ssleay.SSL_set_fd.restype = ctypes.c_int
ssleay.SSL_set_fd.argtypes = [ctypes.c_void_p, ctypes.c_int]
ssleay.SSL_connect.restype = ctypes.c_int
ssleay.SSL_connect.argtypes = [ctypes.c_void_p]
ssleay.SSL_get_error.restype = ctypes.c_int
ssleay.SSL_get_error.argtypes = [ctypes.c_void_p, ctypes.c_int]
ssleay.SSL_write.restype = ctypes.c_int
ssleay.SSL_write.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
ssleay.SSL_read.restype = ctypes.c_int
ssleay.SSL_read.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
ssleay.SSL_get_peer_certificate.restype = ctypes.c_void_p
ssleay.SSL_get_peer_certificate.argtypes = [ctypes.c_void_p]
ssleay.SSL_get_current_cipher.restype = ctypes.c_void_p
ssleay.SSL_get_current_cipher.argtypes = [ctypes.c_void_p]
ssleay.SSL_CIPHER_get_name.restype = ctypes.c_char_p
ssleay.SSL_CIPHER_get_name.argtypes = [ctypes.c_void_p]
ssleay.SSL_free.restype = None
ssleay.SSL_free.argtypes = [ctypes.c_void_p]

libeay.ERR_get_error.restype = ctypes.c_ulong
libeay.ERR_get_error.argtypes = []
libeay.ERR_error_string.restype = ctypes.c_char_p
libeay.ERR_error_string.argtypes = [ctypes.c_ulong, ctypes.c_char_p]


def dump_openssl_errors():
    while True:
        code = libeay.ERR_get_error()
        if code == 0:
            break
        buf = ctypes.create_string_buffer(256)
        libeay.ERR_error_string(code, buf)
        print(f"    OpenSSL error 0x{code:x}: {buf.value.decode(errors='replace')}")


def main():
    ssleay.SSL_library_init()
    ssleay.SSL_load_error_strings()

    ctx = ssleay.SSL_CTX_new(ssleay.SSLv3_client_method())
    ssleay.SSL_CTX_set_cipher_list(ctx, b"DES-CBC3-SHA:RC4-SHA:RC4-MD5:AES128-SHA:AES256-SHA")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect(TARGET)

    ssl_conn = ssleay.SSL_new(ctx)
    ssleay.SSL_set_fd(ssl_conn, sock.fileno())

    print(f"Connecting to {TARGET[0]}:{TARGET[1]} over SSLv3...")
    ret = ssleay.SSL_connect(ssl_conn)
    if ret != 1:
        err = ssleay.SSL_get_error(ssl_conn, ret)
        print(f"Handshake FAILED (SSL_connect={ret}, SSL_get_error={err})")
        dump_openssl_errors()
        sys.exit(1)

    current_cipher = ssleay.SSL_get_current_cipher(ssl_conn)
    cipher = ssleay.SSL_CIPHER_get_name(current_cipher) if current_cipher else None
    print(f"Handshake SUCCEEDED! Cipher: {cipher.decode() if cipher else '?'}")

    msg = b"hello from sslv3 test client"
    ssleay.SSL_write(ssl_conn, msg, len(msg))
    print(f"Sent: {msg}")

    ssleay.SSL_free(ssl_conn)
    sock.close()


if __name__ == "__main__":
    main()
