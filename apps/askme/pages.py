"""Reading pages of trusted official websites into the library, safely.

Only https pages on an active trusted site (or its subdomains) are read, and
redirects must stay on one. The host must resolve to public addresses only,
so a page link can never make the server reach its own network. Pages are
read as HTML; PDFs are not read yet.
"""

import hashlib
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from django.conf import settings
from django.core.exceptions import ValidationError

SKIPPED = {"script", "style", "noscript", "nav", "footer", "header", "form", "svg", "aside"}
BLOCKS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th", "blockquote", "dd", "dt"}


class _Text(HTMLParser):
    """The readable words of a page: its title and paragraph-like blocks."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.blocks: list[str] = []
        self._skipping = 0
        self._in_title = False
        self._current: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in SKIPPED:
            self._skipping += 1
        elif tag == "title":
            self._in_title = True
        elif tag in BLOCKS or tag == "br":
            self._flush()

    def handle_endtag(self, tag):
        if tag in SKIPPED and self._skipping:
            self._skipping -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in BLOCKS:
            self._flush()

    def handle_data(self, data):
        if self._skipping:
            return
        if self._in_title:
            self.title += data
        else:
            self._current.append(data)

    def _flush(self):
        text = re.sub(r"\s+", " ", "".join(self._current)).strip()
        if len(text) >= 3:
            self.blocks.append(text)
        self._current = []

    def result(self) -> tuple[str, str]:
        self._flush()
        title = re.sub(r"\s+", " ", self.title).strip()
        return title[:300], "\n\n".join(self.blocks)


def page_text(html: str) -> tuple[str, str]:
    parser = _Text()
    parser.feed(html)
    parser.close()
    return parser.result()


def _public_host(host: str) -> None:
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, 443)}
    except OSError as error:
        raise ValidationError({"url": "That site could not be found."}) from error
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if not ip.is_global:
            raise ValidationError({"url": "That address is not a public website."})


def trusted_site(url: str, sites):
    """The active trusted site a page belongs to; raises when there is none."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValidationError({"url": "Use the page's full https:// link."})
    if parts.username or parts.password or (parts.port not in (None, 443)):
        raise ValidationError({"url": "Use the page's plain https:// link."})
    for site in sites:
        if site.active and site.covers(parts.hostname):
            return site
    raise ValidationError({"url": "That website is not on the trusted list."})


class _StayOnTrustedSites(urllib.request.HTTPRedirectHandler):
    def __init__(self, sites):
        super().__init__()
        self.sites = sites

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        trusted_site(newurl, self.sites)
        _public_host(urllib.parse.urlsplit(newurl).hostname)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def read(url: str, sites) -> dict:
    """Reads a trusted page: {"title", "text", "hash"}; raises ValidationError."""
    trusted_site(url, sites)
    _public_host(urllib.parse.urlsplit(url).hostname)
    opener = urllib.request.build_opener(_StayOnTrustedSites(sites))
    request = urllib.request.Request(  # noqa: S310 - https, trusted host, checked above
        url, headers={"User-Agent": "TGCG-AskMe/1.0 (+https://app.tgcg.online)"}
    )
    limit = settings.ASKME_PAGE_MAX_BYTES
    try:
        with opener.open(request, timeout=20) as response:
            kind = response.headers.get_content_type()
            if kind == "application/pdf":
                raise ValidationError({"url": "PDF files cannot be read yet; use a web page."})
            if kind not in ("text/html", "application/xhtml+xml", "text/plain"):
                raise ValidationError({"url": "That link is not a web page."})
            raw = response.read(limit + 1)
            charset = response.headers.get_content_charset() or "utf-8"
    except ValidationError:
        raise
    except urllib.error.HTTPError as error:
        raise ValidationError({"url": f"The site answered HTTP {error.code}."}) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ValidationError({"url": "The site could not be reached; try again later."}) from error
    if len(raw) > limit:
        raise ValidationError({"url": "That page is too large to read."})
    html = raw.decode(charset, errors="replace")
    title, text = ("", html.strip()) if kind == "text/plain" else page_text(html)
    if not text:
        raise ValidationError({"url": "No readable words were found on that page."})
    return {
        "title": title,
        "text": text[: settings.ASKME_PAGE_MAX_CHARS],
        "hash": hashlib.sha256(text.encode()).hexdigest(),
    }
