from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

from app.revocation import RevocationCache, _fetch_and_parse


def test_revocation_cache_basic():
    cache = RevocationCache()
    assert cache.is_revoked(123) is False
    cache.update({123, 456})
    assert cache.is_revoked(123) is True
    assert cache.is_revoked(456) is True
    assert cache.is_revoked(789) is False
    assert cache.size() == 2


def test_revocation_cache_replaces_on_update():
    cache = RevocationCache()
    cache.update({1, 2, 3})
    cache.update({4})  # full replacement, not merge
    assert cache.is_revoked(1) is False
    assert cache.is_revoked(4) is True


async def test_fetch_and_parse_handles_der_crl(monkeypatch, root_ca):
    """Build a CRL with our test root_ca, serve it via a stub httpx, parse it."""

    revoked_serials = [11111, 22222]
    builder = x509.CertificateRevocationListBuilder()
    builder = builder.issuer_name(root_ca.cert.subject)
    builder = builder.last_update(datetime.now(timezone.utc) - timedelta(minutes=1))
    builder = builder.next_update(datetime.now(timezone.utc) + timedelta(hours=24))
    for s in revoked_serials:
        rcert = (
            x509.RevokedCertificateBuilder()
            .serial_number(s)
            .revocation_date(datetime.now(timezone.utc) - timedelta(minutes=10))
            .build()
        )
        builder = builder.add_revoked_certificate(rcert)
    crl = builder.sign(private_key=root_ca.key, algorithm=hashes.SHA256())
    der = crl.public_bytes(serialization.Encoding.DER)

    class _StubResp:
        def __init__(self, body):
            self.content = body

        def raise_for_status(self):
            return

    class _StubClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            return _StubResp(der)

    monkeypatch.setattr("app.revocation.httpx.AsyncClient", _StubClient)

    serials = await _fetch_and_parse("http://stub/crl", timeout_seconds=5)
    assert serials == set(revoked_serials)
