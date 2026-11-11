"""Backend URL policy for the custom scenario (no browser needed)."""
import pytest

from app.custom_target import CustomPolicy, CustomURLError, blocked_platform


def pol(allowed="my-site.example", resources="cdn.my-site.example", private=False, overrides=None):
    return CustomPolicy.from_strings(
        allowed, resources, private, True,
        overrides if overrides is not None else "my-site.example=93.184.216.34,cdn.my-site.example=93.184.216.35")


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/lab/video", "http://localhost/", "http://[::1]:3000/x",
                                 "HTTP://LOCALHOST:8000/Path?q=1"])
def test_localhost_allowed(url):
    t = pol().validate(url)
    assert t.local and t.ips == []


def test_allowlisted_domain_allowed_and_pinned():
    p = pol()
    t = p.validate("https://My-Site.example/watch?id=1#frag")
    assert t.host == "my-site.example" and not t.local and t.ips == ["93.184.216.34"]
    assert t.url == "https://my-site.example/watch?id=1#frag"      # host normalised, rest kept as typed
    assert t.input_url == "https://My-Site.example/watch?id=1#frag"  # original preserved for display
    assert t.media_kind == "page"
    assert t.verify_url == "https://my-site.example/.well-known/blc-lab-verify.txt"
    rules = p.resolver_rules()
    assert "MAP my-site.example 93.184.216.34" in rules and "MAP cdn.my-site.example 93.184.216.35" in rules
    assert rules.index("MAP my-site.example") < rules.index("MAP * ~NOTFOUND")


@pytest.mark.parametrize("url", ["https://not-listed.example/", "http://sub.my-site.example/",
                                 "http://my-site.example.evil.com/", "http://cdn.my-site.example/"])
def test_not_allowlisted_blocked(url):
    with pytest.raises(CustomURLError, match="not in the allowlist"):
        pol().validate(url)


@pytest.mark.parametrize("url", ["https://www.youtube.com/watch?v=x", "https://youtu.be/x", "https://m.twitch.tv/a",
                                 "https://www.tiktok.com/@a", "https://chzzk.naver.com/live/x",
                                 "https://play.sooplive.co.kr/x"])
def test_third_party_platforms_always_blocked(url):
    p = pol(allowed="youtube.com www.youtube.com youtu.be twitch.tv tiktok.com chzzk.naver.com sooplive.co.kr")
    assert p.allowed_hosts == ()                                    # refused from the allowlist itself
    assert len(p.config_problems) == 7
    with pytest.raises(CustomURLError, match="third-party media/social platform"):
        p.validate(url)


@pytest.mark.parametrize("url, msg", [
    ("", "empty"), ("   ", "empty"), ("file:///etc/passwd", "Only http and https"),
    ("ftp://localhost/x", "Only http and https"), ("javascript:alert(1)", "Only http and https"),
    ("data:text/html,hi", "Only http and https"), ("chrome://settings", "Only http and https"),
    ("//localhost/x", "Only http and https"), ("http://", "no host"), ("http:///path", "no host"),
    ("http://user:pw@localhost/", "Credentials"), ("http://localhost@my-site.example/", "Credentials"),
    ("http://local host/", "whitespace"), ("http://localhost\\@evil.com/", "whitespace or backslashes"),
    ("http://localhost:99999/", "Malformed"), ("http://localhost/" + "a" * 2100, "longer than"),
])
def test_invalid_urls_blocked(url, msg):
    with pytest.raises(CustomURLError, match=msg):
        pol().validate(url)


def test_ssrf_private_resolution_refused():
    # allow-listed name that resolves to loopback / LAN / metadata service -> refused by default
    for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.169.254", "::1"):
        p = pol(overrides=f"my-site.example={ip}")
        with pytest.raises(CustomURLError, match="non-public address"):
            p.validate("http://my-site.example/")
    # ...unless the operator explicitly opts in for their own LAN/staging servers
    assert pol(private=True, overrides="my-site.example=10.0.0.5").validate("http://my-site.example/").ips == ["10.0.0.5"]


def test_ip_literals():
    with pytest.raises(CustomURLError, match="not in the allowlist"):
        pol().validate("http://10.0.0.1/")
    with pytest.raises(CustomURLError, match="not in the allowlist"):
        pol().validate("http://169.254.169.254/latest/meta-data/")
    p = pol(allowed="10.0.0.9", overrides="")
    with pytest.raises(CustomURLError, match="non-public"):
        p.validate("http://10.0.0.9/")


def test_route_guard_rules_for_redirect_hops():
    p = pol()
    assert p.host_allowed("http://localhost:8000/x", navigation=True)
    assert p.host_allowed("https://my-site.example/next", navigation=True)
    assert p.host_allowed("https://cdn.my-site.example/v.webm", navigation=False)   # sub-resource OK
    assert not p.host_allowed("https://cdn.my-site.example/", navigation=True)      # but not as a page
    assert not p.host_allowed("https://evil.example/", navigation=True)              # redirect target
    assert not p.host_allowed("https://evil.example/x.js", navigation=False)
    assert not p.host_allowed("https://www.youtube.com/embed/x", navigation=False)
    assert not p.host_allowed("file:///etc/passwd", navigation=True)
    assert not p.host_allowed("http://u:p@my-site.example/", navigation=True)
    assert p.host_allowed("data:image/png;base64,AA==", navigation=False)


def test_allowlist_parsing():
    p = CustomPolicy.from_strings("A.example, https://b.example, *.c.example, localhost, a.example", "", False, True, "")
    assert p.allowed_hosts == ("a.example",)
    assert any("b.example" in x for x in p.config_problems) and any("*.c.example" in x for x in p.config_problems)


def test_blocked_platform_matching():
    assert blocked_platform("www.youtube.com") == "youtube.com"
    assert blocked_platform("rr3---sn.googlevideo.com") == "googlevideo.com"
    assert blocked_platform("notyoutube.com") is None
    assert blocked_platform("youtube.com.my-site.example") is None


def test_network_level_allowlist_args():
    p = pol()
    args = p.proxy_args()
    assert args[0] == "--proxy-server=http://blc-lab-blocked.invalid:1"
    bypass = args[1].split("=", 1)[1].split(";")
    assert bypass == ["localhost", "127.0.0.1", "[::1]", "my-site.example", "cdn.my-site.example"]
    # the dead proxy host itself must resolve to NOTFOUND (it is not mapped)
    assert "blc-lab-blocked.invalid" not in p.resolver_rules().split("MAP * ~NOTFOUND")[0]


@pytest.mark.parametrize("url, kind", [
    ("http://localhost:8000/media/clip.mp4", "video_file"), ("http://localhost/a/B.WEBM?x=1", "video_file"),
    ("http://localhost/v.m4v#t=3", "video_file"), ("http://localhost/v.ogv", "video_file"),
    ("http://localhost/watch?file=x.mp4", "page"), ("http://localhost/mp4/", "page"), ("http://localhost/", "page"),
])
def test_media_kind_detection(url, kind):
    assert pol().validate(url).media_kind == kind


def test_input_url_kept_verbatim():
    raw = "HTTP://LOCALHOST:8000/Lab/Video?Q=Ab%20C#t=1.5"
    t = pol().validate("  " + raw + "  ")
    assert t.input_url == raw
    assert t.url == "http://localhost:8000/Lab/Video?Q=Ab%20C#t=1.5"
