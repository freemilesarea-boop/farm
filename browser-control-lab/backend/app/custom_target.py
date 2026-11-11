"""Policy for user-supplied ("custom") test URLs.

The lab only ever drives browsers at targets the operator owns or is authorised to test:

* ``localhost`` / ``127.0.0.1`` / ``::1`` are always allowed.
* Any other host must be listed in ``LAB_CUSTOM_ALLOWED_HOSTS`` (exact host names, no
  wildcards). Extra hosts that may serve sub-resources only (e.g. a CDN for your own
  site) go in ``LAB_CUSTOM_RESOURCE_HOSTS``.
* Third-party media / social platforms are refused even if listed: pointing many
  automated players at them would inflate their views / watch time, which this lab
  must not do (see README "Safety").
* Only http/https; no credentials in URLs; IP literals other than loopback are refused
  unless allow-listed.
* SSRF: allow-listed names are resolved by the backend and must resolve to public
  addresses (unless ``LAB_CUSTOM_ALLOW_PRIVATE_NETWORK=1``); the browser is then *pinned*
  to exactly those addresses with ``--host-resolver-rules`` so DNS cannot be re-bound
  later. Every request (including each redirect hop) is re-checked in the browser's
  route guard, and the final page URL is verified after navigation.
* Ownership: before the first test of a non-local host, the session fetches
  ``http(s)://<host>/.well-known/blc-lab-verify.txt`` and it must contain this lab's
  token (like the verification file used by hosted load-testing services).
"""
from __future__ import annotations

import ipaddress
import secrets
import socket
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
VERIFY_PATH = "/.well-known/blc-lab-verify.txt"
DEAD_PROXY_HOST = "blc-lab-blocked.invalid"  # resolves to NOTFOUND under the resolver rules
MAX_URL_LENGTH = 2048

# Platforms where automated repeated playback would manipulate third-party metrics.
# Matched against the host and every parent domain (so m.youtube.com is covered).
BLOCKED_PLATFORM_DOMAINS = frozenset({
    # video / streaming
    "youtube.com", "youtu.be", "youtube-nocookie.com", "googlevideo.com", "ytimg.com",
    "twitch.tv", "ttvnw.net", "jtvnw.net", "kick.com", "vimeo.com", "vimeocdn.com",
    "dailymotion.com", "dmcdn.net", "rumble.com", "bilibili.com", "bilibili.tv", "hdslb.com",
    "netflix.com", "nflxvideo.net", "afreecatv.com", "sooplive.co.kr", "chzzk.naver.com",
    "tv.naver.com", "naver.com", "pstatic.net", "tv.kakao.com", "kakao.com", "daum.net",
    "wavve.com", "tving.com", "coupangplay.com", "iqiyi.com", "youku.com", "douyin.com",
    # social
    "tiktok.com", "tiktokcdn.com", "tiktokv.com", "instagram.com", "cdninstagram.com",
    "facebook.com", "fb.com", "fbcdn.net", "fb.watch", "threads.net", "x.com", "twitter.com",
    "twimg.com", "reddit.com", "redd.it", "linkedin.com", "pinterest.com", "snapchat.com",
    "weibo.com", "vk.com", "ok.ru",
    # audio
    "spotify.com", "soundcloud.com", "music.apple.com", "melon.com", "genie.co.kr",
})


class CustomURLError(ValueError):
    """Raised with a user-facing message when a custom URL is not allowed."""


def _norm_host(host: str) -> str:
    host = host.strip().strip("[]").rstrip(".").lower()
    try:
        return host.encode("idna").decode("ascii") if host and not _is_ip(host) else host
    except UnicodeError:
        raise CustomURLError(f"Invalid host name: {host!r}") from None


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def blocked_platform(host: str) -> str | None:
    parts = host.split(".")
    for i in range(len(parts) - 1):
        cand = ".".join(parts[i:])
        if cand in BLOCKED_PLATFORM_DOMAINS:
            return cand
    return None


def is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return addr.is_global and not addr.is_multicast


def parse_host_list(raw: str) -> tuple[list[str], list[str]]:
    """Parse a comma/space separated host list -> (valid hosts, rejection messages)."""
    hosts, problems = [], []
    for item in raw.replace(",", " ").split():
        item = item.strip()
        if not item:
            continue
        if "://" in item or "/" in item or "*" in item or "@" in item:
            problems.append(f"{item}: host names only (no scheme, path, wildcard or credentials)")
            continue
        try:
            h = _norm_host(item.rsplit(":", 1)[0] if item.count(":") == 1 else item)
        except CustomURLError as exc:
            problems.append(str(exc))
            continue
        plat = blocked_platform(h)
        if plat:
            problems.append(f"{h}: third-party media/social platform ({plat}) cannot be a test target")
            continue
        if h in LOCAL_HOSTS:
            continue  # always allowed anyway
        if h not in hosts:
            hosts.append(h)
    return hosts, problems


def parse_overrides(raw: str) -> dict[str, str]:
    """``host=ip`` pairs (like /etc/hosts) for staging/LAN targets and tests."""
    out: dict[str, str] = {}
    for item in raw.replace(",", " ").split():
        if "=" not in item:
            continue
        h, _, ip = item.partition("=")
        h = _norm_host(h)
        if _is_ip(ip.strip()):
            out[h] = ip.strip()
    return out


@dataclass(frozen=True)
class CustomPolicy:
    allowed_hosts: tuple[str, ...] = ()
    resource_hosts: tuple[str, ...] = ()
    allow_private_network: bool = False
    require_verification: bool = True
    host_overrides: dict[str, str] = field(default_factory=dict)
    config_problems: tuple[str, ...] = ()

    @classmethod
    def from_strings(cls, allowed: str, resources: str, allow_private: bool, require_verification: bool,
                     overrides: str) -> "CustomPolicy":
        a, p1 = parse_host_list(allowed)
        r, p2 = parse_host_list(resources)
        return cls(tuple(a), tuple(h for h in r if h not in a), allow_private, require_verification,
                   parse_overrides(overrides), tuple(p1 + p2))

    # ------------------------------------------------------------------ resolution
    def resolve(self, host: str) -> list[str]:
        """Resolve a non-local host and enforce the public-address rule."""
        if host in self.host_overrides:
            ips = [self.host_overrides[host]]
        elif _is_ip(host):
            ips = [host]
        else:
            try:
                infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            except socket.gaierror as exc:
                raise CustomURLError(f"Cannot resolve {host}: {exc.strerror or exc}") from None
            ips = sorted({i[4][0] for i in infos})
        if not ips:
            raise CustomURLError(f"Cannot resolve {host}")
        if not self.allow_private_network:
            bad = [ip for ip in ips if not is_public_ip(ip)]
            if bad:
                raise CustomURLError(
                    f"{host} resolves to a non-public address ({', '.join(bad)}). Refused to prevent SSRF; "
                    "set LAB_CUSTOM_ALLOW_PRIVATE_NETWORK=1 only for your own LAN/staging servers.")
        return ips

    # ------------------------------------------------------------------ validation
    def validate(self, url: str, *, resolve: bool = True) -> "ValidatedTarget":
        if not isinstance(url, str) or not url.strip():
            raise CustomURLError("URL is empty")
        url = url.strip()
        if len(url) > MAX_URL_LENGTH:
            raise CustomURLError(f"URL is longer than {MAX_URL_LENGTH} characters")
        if any(c in url for c in "\r\n\t\\ "):
            raise CustomURLError("URL contains whitespace or backslashes")
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise CustomURLError(f"Malformed URL: {exc}") from None
        if parts.scheme not in ("http", "https"):
            raise CustomURLError(f"Only http and https URLs are allowed (got '{parts.scheme or 'none'}:')")
        if parts.username or parts.password or "@" in parts.netloc:
            raise CustomURLError("Credentials in URLs are not allowed")
        if not parts.hostname:
            raise CustomURLError("URL has no host")
        host = _norm_host(parts.hostname)
        plat = blocked_platform(host)
        if plat:
            raise CustomURLError(
                f"{host} is a third-party media/social platform ({plat}). Automated repeated playback there "
                "would manipulate its views/watch time, so it cannot be tested with this lab.")
        if host in LOCAL_HOSTS:
            local = True
            ips: list[str] = []
        else:
            local = False
            if host not in self.allowed_hosts:
                raise CustomURLError(
                    f"{host} is not in the allowlist. Add it to LAB_CUSTOM_ALLOWED_HOSTS in lab.env "
                    "(only hosts you own or are authorised to test) and restart the backend.")
            if _is_ip(host) and not self.allow_private_network and not is_public_ip(host):
                raise CustomURLError(f"{host} is a non-public address")
            ips = self.resolve(host) if resolve else []
        netloc = f"[{host}]" if ":" in host else host
        if port:
            netloc += f":{port}"
        normalized = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
        return ValidatedTarget(url=normalized, host=host, scheme=parts.scheme, port=port, local=local, ips=ips)

    # ------------------------------------------------------------------ browser-side checks
    def host_allowed(self, url: str, *, navigation: bool) -> bool:
        """Route-guard check for every request a custom session makes (redirect hops included)."""
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        if parts.scheme in ("data", "blob", "about"):
            return True
        if parts.scheme not in ("http", "https", "ws", "wss") or parts.username or parts.password:
            return False
        host = _norm_host(parts.hostname or "")
        if not host or blocked_platform(host):
            return False
        if host in LOCAL_HOSTS or host in self.allowed_hosts:
            return True
        return (not navigation) and host in self.resource_hosts

    def resolver_rules(self) -> str:
        """--host-resolver-rules: pin allow-listed hosts to validated IPs; everything else NOTFOUND."""
        rules = []
        for h in (*self.allowed_hosts, *self.resource_hosts):
            try:
                ip = self.resolve(h)[0]
            except CustomURLError:
                continue  # unresolvable / refused -> stays NOTFOUND
            rules.append(f"MAP {h} {'[' + ip + ']' if ':' in ip else ip}")
        rules += ["MAP * ~NOTFOUND", "EXCLUDE localhost", "EXCLUDE 127.0.0.1"]
        return ", ".join(rules)

    def proxy_args(self) -> list[str]:
        """Network-level allowlist that does not depend on Playwright request interception.

        Playwright's route handler is *not* invoked for redirect hops, and host-resolver
        rules do not apply to IP literals (e.g. a redirect to 169.254.169.254). So every
        connection is sent to an unresolvable proxy (resolver rules map it to NOTFOUND ->
        the request fails), except localhost and the allow-listed hosts, which bypass the
        proxy and connect directly to their pinned addresses.
        """
        bypass = ["localhost", "127.0.0.1", "[::1]", *self.allowed_hosts, *self.resource_hosts]
        return [f"--proxy-server=http://{DEAD_PROXY_HOST}:1", f"--proxy-bypass-list={';'.join(bypass)}"]

    def to_dict(self) -> dict:
        return {
            "allowed_hosts": list(self.allowed_hosts),
            "resource_hosts": list(self.resource_hosts),
            "allow_private_network": self.allow_private_network,
            "require_verification": self.require_verification,
            "config_problems": list(self.config_problems),
            "blocked_platform_examples": ["youtube.com", "twitch.tv", "tiktok.com", "instagram.com",
                                          "afreecatv.com", "chzzk.naver.com"],
        }


@dataclass(frozen=True)
class ValidatedTarget:
    url: str
    host: str
    scheme: str
    port: int | None
    local: bool
    ips: list[str]

    @property
    def origin(self) -> str:
        netloc = f"[{self.host}]" if ":" in self.host else self.host
        if self.port:
            netloc += f":{self.port}"
        return f"{self.scheme}://{netloc}"

    @property
    def verify_url(self) -> str:
        return self.origin + VERIFY_PATH


def load_or_create_token(path: Path) -> str:
    try:
        tok = path.read_text().strip()
        if len(tok) >= 32:
            return tok
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tok = "blc-lab-" + secrets.token_hex(16)
    path.write_text(tok + "\n")
    return tok
