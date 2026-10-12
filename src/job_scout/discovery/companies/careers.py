"""Bounded static publisher pages with public-IP pinning and robots checks."""

import json
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from job_scout.discovery.detect import DiscoveryError, detect
from job_scout.discovery.http import PublicAPITransport
from job_scout.sources.base import BoardHTTP, SourceError

USER_AGENT = "JobScout"


def safe_url(value: str) -> str:
    detect(value)  # Reject credentials, IP literals and internal names.
    if urlsplit(value).scheme != "https":
        raise DiscoveryError("Company pages require HTTPS")
    # Do not rewrite ATS posting paths here; strip query/fragment on ordinary pages.
    parsed = urlsplit(value)
    return f"https://{parsed.hostname}{parsed.path or '/'}"


def origin(value: str) -> str:
    return f"https://{urlsplit(safe_url(value)).hostname}"


class SiteTransport(PublicAPITransport):
    async def handle_async_request(self, request):
        safe_url(str(request.url))
        # Each observed host is checked and pinned; redirects are never followed.
        scoped = PublicAPITransport(
            transport=self.transport, resolver=self.resolver, allowed_hosts={request.url.host}
        )
        return await scoped.handle_async_request(request)


class Page(HTMLParser):
    """Parse evidence only; scripts are never executed and body text is not retained."""

    def __init__(self, text: str, url: str):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.text: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.organizations: list[tuple[str, str]] = []
        self.restricted = False
        self._json: list[str] | None = None
        self._anchor: tuple[str, list[str]] | None = None
        self._hidden: list[str] = []
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = {key: value or "" for key, value in attrs}
        if tag == "meta" and attrs.get("name", "").lower() in {"robots", "jobscout"}:
            self.restricted |= any(
                term in attrs.get("content", "").lower() for term in ("noindex", "nofollow", "none")
            )
        if tag in {"script", "style", "template"}:
            if tag == "script" and not self._hidden and attrs.get("type") == "application/ld+json":
                self._json = []
            self._hidden.append(tag)
        if tag == "a" and not self._hidden and attrs.get("href") and len(self.links) < 200:
            try:
                self._anchor = (urljoin(self.url, attrs["href"]), [])
            except ValueError:
                self._anchor = None

    def handle_data(self, data):
        if not self._hidden:
            self.text.append(data)
        if self._json is not None:
            self._json.append(data)
        elif self._anchor is not None and not self._hidden:
            self._anchor[1].append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._json is not None:
            try:
                self._organizations(json.loads("".join(self._json)))
            except (ValueError, RecursionError):
                pass
            self._json = None
        if self._hidden and self._hidden[-1] == tag:
            self._hidden.pop()
        if tag == "a" and self._anchor is not None:
            self.links.append((self._anchor[0], " ".join(self._anchor[1])))
            self._anchor = None

    def _organizations(self, value, depth=0):
        if depth > 5:
            return
        if isinstance(value, list):
            for item in value[:100]:
                self._organizations(item, depth + 1)
        elif isinstance(value, dict):
            types = value.get("@type", [])
            types = [types] if isinstance(types, str) else types
            if isinstance(types, list) and "Organization" in types:
                name, url = value.get("name"), value.get("url")
                if isinstance(name, str) and isinstance(url, str) and 0 < len(name.strip()) <= 200:
                    try:
                        if origin(url) == origin(self.url):
                            self.organizations.append((name.strip(), origin(url)))
                    except DiscoveryError:
                        pass
            self._organizations(value.get("@graph"), depth + 1)


class CareersHTTP:
    def __init__(self, http: BoardHTTP):
        self.http = http
        self.robots: dict[str, RobotFileParser] = {}

    async def page(self, url: str) -> Page:
        url = safe_url(url)
        site = origin(url)
        if site not in self.robots:
            response = await self.http.get_response(
                site + "/robots.txt", {}, allowed_statuses=(404,)
            )
            robot = RobotFileParser()
            robot.parse([] if response.status_code == 404 else response.text.splitlines())
            self.robots[site] = robot
        robot = self.robots[site]
        if not robot.can_fetch(USER_AGENT, url):
            raise SourceError("Publisher robots policy disallows this page")
        delay = robot.crawl_delay(USER_AGENT)
        rate = robot.request_rate(USER_AGENT)
        interval = max(delay or 0, rate.seconds / rate.requests if rate and rate.requests else 0)
        if interval > 60:
            raise SourceError("Publisher crawl delay exceeds this run's budget")
        # Conservative shared pacing; the first page also waits after robots fetch.
        if interval:
            await self.http.sleep(interval)
            self.http.interval = max(self.http.interval, interval)
        response = await self.http.get_response(url, {})
        if response.headers.get("content-type", "").split(";", 1)[0] not in {
            "text/html",
            "application/xhtml+xml",
        }:
            raise SourceError("Publisher page is not HTML")
        page = Page(response.text, url)
        if page.restricted or any(
            term in response.headers.get("x-robots-tag", "").lower()
            for term in ("noindex", "nofollow", "none")
        ):
            raise SourceError("Publisher page opts out of indexing or link following")
        return page
