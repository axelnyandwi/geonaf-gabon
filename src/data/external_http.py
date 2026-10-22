"""Bounded HTTPS client for non-Copernicus open-data hosts (WorldCover, Overpass).

Reuses GEONAF's verified TLS session (``secure_session``) and retry policy
(``run_with_retry``). Every request is checked against an explicit host
allow-list, never follows redirects, is throttled per host, has a timeout and
a response-size ceiling, and is counted (requests, bytes) so the network
consumption of a demonstration can be reported. No credentials are sent.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from urllib.parse import urlsplit

from requests.utils import get_environ_proxies as requests_proxies

from src.data.network import NetworkError, retry_after_seconds, run_with_retry, secure_session
from src.network_config import NetworkSettings

USER_AGENT = "GEONAF/0.4 (prototype de recherche géospatiale ; requêtes bornées et mises en cache)"


@dataclass(frozen=True)
class HostPolicy:
    service: str               # Short label used in errors and logs.
    min_interval_s: float      # Minimum delay between two requests to this host.
    max_response_bytes: int    # Ceiling for one response body.


EXTERNAL_HOSTS = {
    # ESA WorldCover public COG bucket (CC BY 4.0). Range reads only.
    "esa-worldcover.s3.eu-central-1.amazonaws.com": HostPolicy("worldcover", 0.1, 4 * 1024 * 1024),
    # Main Overpass instance: usage policy asks for few, cached, bounded queries.
    "overpass-api.de": HostPolicy("overpass", 10.0, 64 * 1024 * 1024),
}


class ResponseTooLarge(ValueError):
    """Domain refusal (not retried): the body exceeds the authorised ceiling."""


@dataclass(frozen=True)
class ExternalResponse:
    status_code: int
    headers: dict
    content: bytes


class ExternalHttpClient:
    """Allow-listed, throttled and metered HTTPS access; one instance per session/page."""

    def __init__(self, settings: NetworkSettings | None = None, *, session=None, hosts=None,
                 clock=time.monotonic, sleep=time.sleep):
        self.settings = settings or NetworkSettings.from_environment()
        self.session = session if session is not None else secure_session(self.settings)
        self.hosts = dict(EXTERNAL_HOSTS if hosts is None else hosts)
        self._clock, self._sleep = clock, sleep
        self._last_request: dict[str, float] = {}
        self.usage: dict[str, dict[str, int]] = {}

    def policy(self, url: str) -> tuple[str, HostPolicy]:
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            valid = (parsed.scheme == "https" and host in self.hosts and parsed.port in (None, 443)
                     and not parsed.username and not parsed.password and not parsed.fragment)
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("Hôte externe non autorisé ou URL non HTTPS.")
        return host, self.hosts[host]

    def _throttle(self, host: str, policy: HostPolicy) -> None:
        last = self._last_request.get(host)
        if last is not None:
            wait = policy.min_interval_s - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = self._clock()

    def request(self, method: str, url: str, *, headers=None, data=None, max_bytes: int | None = None,
                timeout: tuple[float, float] | None = None, expected=(200,)) -> ExternalResponse:
        host, policy = self.policy(url)
        if method not in ("GET", "HEAD", "POST"):
            raise ValueError("Méthode HTTP non autorisée.")
        ceiling = policy.max_response_bytes if max_bytes is None else min(int(max_bytes), policy.max_response_bytes)
        timeout = timeout or (self.settings.connect_timeout, self.settings.read_timeout)
        merged = {"User-Agent": USER_AGENT, **(headers or {})}

        def action():
            self._throttle(host, policy)
            usage = self.usage.setdefault(host, {"requests": 0, "bytes": 0})
            usage["requests"] += 1
            with self.session.request(method, url, headers=merged, data=data, timeout=timeout, stream=True,
                                      allow_redirects=False, verify=True, proxies=requests_proxies(url)) as response:
                if response.status_code not in expected:
                    raise NetworkError(service=policy.service, category="http", status_code=response.status_code,
                                       retry_after=retry_after_seconds(response.headers.get("Retry-After")))
                declared = response.headers.get("Content-Length")
                if declared is not None and declared.isdigit() and int(declared) > ceiling and method != "HEAD":
                    raise ResponseTooLarge("Réponse annoncée supérieure au plafond autorisé.")
                chunks, size = [], 0
                if method != "HEAD":
                    for chunk in response.iter_content(64 * 1024):
                        size += len(chunk)
                        usage["bytes"] += len(chunk)
                        if size > ceiling:
                            raise ResponseTooLarge("Réponse supérieure au plafond autorisé.")
                        chunks.append(chunk)
                return ExternalResponse(response.status_code, dict(response.headers), b"".join(chunks))

        return run_with_retry(action, self.settings, service=policy.service)

    def usage_summary(self) -> dict[str, dict[str, int]]:
        return {host: dict(values) for host, values in self.usage.items()}
