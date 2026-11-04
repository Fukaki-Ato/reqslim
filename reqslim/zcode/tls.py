"""Local TLS for ZCode's signed provider requests.

ZCode 3.17 enables client request signing for plan accounts.  Its signer
refuses to run unless the provider ``baseUrl`` is HTTPS, and the signing
handshake itself is a normal HTTP request to the same origin.  A loopback
HTTP proxy therefore cannot work for those accounts: every model request
fails before it is sent, with ``ClientRequestSigningV4Error``.

This module creates a private CA and a ``127.0.0.1`` server certificate in
the reqslim config directory.  The installer points ZCode's
``ZCODE_AGENT_CA_CERT`` at a bundle containing the private CA plus the host's
normal roots, so only ZCode trusts the local listener.
"""

from __future__ import annotations

import os
import shutil
import ssl
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TlsFiles:
    """Paths of the generated TLS material."""

    ca: Path
    bundle: Path
    cert: Path
    key: Path


def _openssl() -> str | None:
    found = shutil.which("openssl")
    if found:
        return found
    candidates = [
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git" / "usr" / "bin" / "openssl.exe",
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git" / "mingw64" / "bin" / "openssl.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "usr" / "bin" / "openssl.exe",
        Path("C:/Program Files/Git/usr/bin/openssl.exe"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _run(openssl: str, args: list[str], cwd: Path) -> None:
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        [openssl, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        creationflags=flags,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"openssl failed: {' '.join(args)}: {detail[:500]}")


def _system_ca_pem() -> str:
    """Return the host's default roots as PEM, or an empty string.

    ``caCertFile`` replaces Node's default trust store, so the bundle must
    keep the normal roots as well as the private CA.  Python's default
    context reads the Windows certificate store on Windows and the OpenSSL
    default paths elsewhere.
    """
    try:
        context = ssl.create_default_context()
        ders = context.get_ca_certs(binary_form=True)
    except (OSError, ssl.SSLError):
        return ""
    return "".join(ssl.DER_cert_to_PEM_cert(der) for der in ders)


def _write_bundle(ca: Path, bundle: Path) -> None:
    local = ca.read_text(encoding="ascii")
    text = _system_ca_pem().rstrip() + "\n" + local.rstrip() + "\n"
    tmp = bundle.with_name(bundle.name + ".tmp")
    tmp.write_text(text, encoding="ascii")
    os.replace(tmp, bundle)


def _valid_tls(files: TlsFiles) -> bool:
    try:
        server_context = make_context(files.cert, files.key)
        client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_context.load_verify_locations(cafile=str(files.ca))
        server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
        client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
        server = server_context.wrap_bio(server_in, server_out, server_side=True)
        client = client_context.wrap_bio(client_in, client_out, server_hostname="127.0.0.1")
        done = [False, False]
        for _ in range(12):
            for index, connection in enumerate((client, server)):
                if not done[index]:
                    try:
                        connection.do_handshake()
                        done[index] = True
                    except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                        pass
            if client_out.pending:
                server_in.write(client_out.read())
            if server_out.pending:
                client_in.write(server_out.read())
            if all(done):
                return True
    except (OSError, ValueError):
        return False
    return False


def ensure_tls(config_dir: Path, *, force: bool = False) -> TlsFiles:
    """Create (or reuse) the private CA and server certificate.

    The private CA key is kept next to the certificate so the files can be
    regenerated without changing what ZCode trusts.  A missing or forced
    generation replaces the whole directory; callers must then restart the
    proxy and ZCode.
    """
    tls_dir = Path(config_dir) / "tls"
    files = TlsFiles(
        ca=tls_dir / "ca.crt",
        bundle=tls_dir / "ca-bundle.pem",
        cert=tls_dir / "server.crt",
        key=tls_dir / "server.key",
    )
    if not force and _valid_tls(files):
        _write_bundle(files.ca, files.bundle)
        return files

    openssl = _openssl()
    if not openssl:
        raise RuntimeError(
            "openssl was not found; it is required to create the local TLS "
            "certificate. Install Git for Windows/OpenSSL, or re-run the "
            "installer with --no-tls (ZCode plan accounts will not sign)."
        )

    tls_dir.mkdir(parents=True, exist_ok=True)
    ca_key = tls_dir / "ca.key"
    ca_cnf = tls_dir / "ca.cnf"
    server_cnf = tls_dir / "server.cnf"
    csr = tls_dir / "server.csr"
    ext = tls_dir / "server.ext"
    serial = tls_dir / "ca.srl"
    for path in (
        files.ca, ca_key, files.cert, files.key, files.bundle,
        ca_cnf, server_cnf, csr, ext, serial,
    ):
        try:
            path.unlink()
        except OSError:
            pass

    # Config files instead of ``-addext``: LibreSSL on macOS and older
    # OpenSSL releases do not all support that flag, but they do support
    # ``x509_extensions`` and ``-extensions``.
    ca_cnf.write_text(
        "[req]\n"
        "distinguished_name=dn\n"
        "x509_extensions=v3_ca\n"
        "prompt=no\n"
        "[dn]\n"
        "CN=reqslim local CA\n"
        "[v3_ca]\n"
        "basicConstraints=critical,CA:TRUE\n"
        "keyUsage=critical,keyCertSign,cRLSign\n"
        "subjectKeyIdentifier=hash\n",
        encoding="ascii",
    )
    _run(
        openssl,
        [
            "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes",
            "-days", "3650", "-keyout", ca_key.name, "-out", files.ca.name,
            "-config", ca_cnf.name,
        ],
        tls_dir,
    )
    server_cnf.write_text(
        "[req]\n"
        "distinguished_name=dn\n"
        "prompt=no\n"
        "[dn]\n"
        "CN=127.0.0.1\n",
        encoding="ascii",
    )
    _run(
        openssl,
        [
            "req", "-new", "-newkey", "rsa:2048", "-sha256", "-nodes",
            "-keyout", files.key.name, "-out", csr.name,
            "-config", server_cnf.name,
        ],
        tls_dir,
    )
    ext.write_text(
        "[v3_server]\n"
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1\n",
        encoding="ascii",
    )
    _run(
        openssl,
        [
            "x509", "-req", "-sha256", "-days", "3650",
            "-in", csr.name, "-CA", files.ca.name, "-CAkey", ca_key.name,
            "-CAcreateserial", "-out", files.cert.name,
            "-extfile", ext.name, "-extensions", "v3_server",
        ],
        tls_dir,
    )
    _write_bundle(files.ca, files.bundle)
    try:
        ca_cnf.unlink()
        server_cnf.unlink()
        csr.unlink()
        ext.unlink()
        serial.unlink()
    except OSError:
        pass
    for private in (ca_key, files.key):
        try:
            os.chmod(private, 0o600)
        except OSError:
            pass
    if not _valid_tls(files):
        raise RuntimeError("the generated TLS certificate or key is invalid")
    return files


def make_context(cert: str | Path, key: str | Path) -> ssl.SSLContext:
    """Server context for the proxy's local HTTPS listeners."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(cert), str(key))
    return context
