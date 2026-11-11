import pytest

from app.security import URLNotAllowed, assert_local_url, is_local_url, redact


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/lab/basic", "http://localhost/x", "http://[::1]:8000/", "about:blank",
    "data:text/html,hi",
])
def test_local_urls_allowed(url):
    assert is_local_url(url)


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=1", "http://example.com", "http://127.0.0.1.evil.com/",
    "http://localhost@evil.com/", "ftp://localhost/", "file:///etc/passwd", "http://10.0.0.5/",
])
def test_external_urls_blocked(url):
    assert not is_local_url(url)
    with pytest.raises(URLNotAllowed):
        assert_local_url(url)


@pytest.mark.parametrize("raw, secret", [
    ("login password=hunter2 ok", "hunter2"),
    ('{"token": "abc.def.ghi"}', "abc.def.ghi"),
    ("Authorization: Bearer eyJhbGciOi.xyz", "eyJhbGciOi.xyz"),
    ("GET /cb?access_token=s3cr3t&x=1", "s3cr3t"),
    ("http://admin:pa55@localhost/", "pa55"),
    ("api_key=AKIA123", "AKIA123"),
])
def test_redaction(raw, secret):
    out = redact(raw)
    assert secret not in out
    assert "[REDACTED]" in out
