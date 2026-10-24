"""Minimal WebDAV / CalDAV protocol layer on top of httpx.

Only what calsync needs: PROPFIND, REPORT (calendar-query, calendar-multiget,
sync-collection), MKCALENDAR, conditional PUT and DELETE. XML is parsed with
the standard library (Python's bundled expat rejects entity-expansion bombs).
"""

import asyncio
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from calsync.errors import (
    AuthError,
    CalsyncError,
    ConflictError,
    CursorExpired,
    ForbiddenError,
    NotFoundError,
)
from calsync.http import Sleep, request_with_retry

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
CS = "http://calendarserver.org/ns/"

MULTIGET_BATCH = 100
MAX_REDIRECTS = 5
REDIRECT_STATUS = frozenset({301, 302, 307, 308})


def q(ns: str, name: str) -> str:
    return f"{{{ns}}}{name}"


@dataclass(frozen=True, slots=True)
class DavResponse:
    href: str  # absolute URL, resolved against the URL of the response that listed it
    status: int
    props: dict[str, ET.Element]

    def text(self, name: str) -> str | None:
        element = self.props.get(name)
        if element is None or element.text is None:
            return None
        return element.text.strip() or None

    def href_prop(self, name: str) -> str | None:
        """An href-valued property, resolved to an absolute URL like ``href``."""
        element = self.props.get(name)
        if element is None:
            return None
        child = element.find(q(DAV, "href"))
        return None if child is None or not child.text else urljoin(self.href, child.text.strip())


def _status_code(text: str | None) -> int | None:
    if not text:
        return None
    parts = text.split()
    return int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None


def parse_multistatus(body: bytes, base_url: str) -> tuple[list[DavResponse], str | None]:
    """Parse a 207 body. Hrefs become absolute URLs: iCloud serves calendars from a
    different host (pNN-caldav.icloud.com) than the one discovery starts on."""
    root = ET.fromstring(body)
    responses: list[DavResponse] = []
    for node in root.findall(q(DAV, "response")):
        href = urljoin(base_url, (node.findtext(q(DAV, "href")) or "").strip())
        props: dict[str, ET.Element] = {}
        for propstat in node.findall(q(DAV, "propstat")):
            if _status_code(propstat.findtext(q(DAV, "status"))) != 200:
                continue
            prop = propstat.find(q(DAV, "prop"))
            if prop is not None:
                for child in prop:
                    props[child.tag] = child
        status = _status_code(node.findtext(q(DAV, "status"))) or 200
        responses.append(DavResponse(href, status, props))
    token = root.findtext(q(DAV, "sync-token"))
    return responses, token.strip() if token else None


def _precondition(body: bytes) -> str | None:
    """The first precondition element of a DAV:error body (RFC 4918 16), e.g.
    ``{urn:ietf:params:xml:ns:caldav}valid-calendar-data``; None if there is none."""
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, ValueError):
        return None
    if root.tag != q(DAV, "error"):
        return None
    first = next(iter(root), None)
    return None if first is None else first.tag


def _xml(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def propfind_body(props: Sequence[str]) -> bytes:
    root = ET.Element(q(DAV, "propfind"))
    prop = ET.SubElement(root, q(DAV, "prop"))
    for name in props:
        ET.SubElement(prop, name)
    return _xml(root)


def sync_collection_body(token: str | None) -> bytes:
    root = ET.Element(q(DAV, "sync-collection"))
    ET.SubElement(root, q(DAV, "sync-token")).text = token or None
    ET.SubElement(root, q(DAV, "sync-level")).text = "1"
    prop = ET.SubElement(root, q(DAV, "prop"))
    ET.SubElement(prop, q(DAV, "getetag"))
    return _xml(root)


def calendar_query_etags_body() -> bytes:
    root = ET.Element(q(CALDAV, "calendar-query"))
    prop = ET.SubElement(root, q(DAV, "prop"))
    ET.SubElement(prop, q(DAV, "getetag"))
    filt = ET.SubElement(root, q(CALDAV, "filter"))
    vcal = ET.SubElement(filt, q(CALDAV, "comp-filter"), {"name": "VCALENDAR"})
    ET.SubElement(vcal, q(CALDAV, "comp-filter"), {"name": "VEVENT"})
    return _xml(root)


def multiget_body(hrefs: Sequence[str]) -> bytes:
    root = ET.Element(q(CALDAV, "calendar-multiget"))
    prop = ET.SubElement(root, q(DAV, "prop"))
    ET.SubElement(prop, q(DAV, "getetag"))
    ET.SubElement(prop, q(CALDAV, "calendar-data"))
    for href in hrefs:
        ET.SubElement(root, q(DAV, "href")).text = href
    return _xml(root)


def mkcalendar_body(display_name: str) -> bytes:
    root = ET.Element(q(CALDAV, "mkcalendar"))
    prop = ET.SubElement(ET.SubElement(root, q(DAV, "set")), q(DAV, "prop"))
    ET.SubElement(prop, q(DAV, "displayname")).text = display_name
    components = ET.SubElement(prop, q(CALDAV, "supported-calendar-component-set"))
    ET.SubElement(components, q(CALDAV, "comp"), {"name": "VEVENT"})
    return _xml(root)


_XML_HEADERS = {"Content-Type": "application/xml; charset=utf-8"}


class DavClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        *,
        sleep: Sleep = asyncio.sleep,
        on_throttle: Callable[[], None] | None = None,
    ) -> None:
        self._client = client
        self._base = base_url
        self._sleep = sleep
        self._on_throttle = on_throttle

    def url(self, href: str) -> str:
        return urljoin(self._base, href)

    async def _send(
        self,
        method: str,
        href: str,
        *,
        kind: str,
        ok: tuple[int, ...],
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        url = self.url(href)
        # Redirects are followed by hand: httpx drops credentials on a cross-host
        # redirect and may rewrite the method, both wrong for WebDAV.
        for _ in range(MAX_REDIRECTS + 1):
            response = await request_with_retry(
                self._client,
                method,
                url,
                kind=kind,
                content=content,
                headers=headers,
                sleep=self._sleep,
                on_throttle=self._on_throttle,
            )
            location = response.headers.get("location")
            if response.status_code not in REDIRECT_STATUS or not location:
                break
            target = urljoin(url, location)
            if urlsplit(url).scheme == "https" and urlsplit(target).scheme != "https":
                raise CalsyncError(f"{method} {href}: refusing redirect to {target}")
            url = target
        else:
            raise CalsyncError(f"{method} {href}: too many redirects")
        status = response.status_code
        if status in ok:
            return response
        sync_token_invalid = b"valid-sync-token" in response.content
        if status in (403, 409) and sync_token_invalid:
            raise CursorExpired(href)
        if status == 401:
            raise AuthError(f"{method} {href}: HTTP 401")
        if status == 403:
            # Credentials worked; the server refused this request (privileges, a
            # precondition such as valid-calendar-data or no-uid-conflict, ...).
            raise ForbiddenError(f"{method} {href}: {_precondition(response.content) or 'HTTP 403'}")
        if status == 404:
            raise NotFoundError(href)
        if status == 412:
            raise ConflictError(href)
        raise CalsyncError(f"{method} {href}: HTTP {status}")

    async def propfind(self, href: str, props: Sequence[str], *, depth: int) -> list[DavResponse]:
        response = await self._send(
            "PROPFIND",
            href,
            kind="propfind",
            ok=(207,),
            content=propfind_body(props),
            headers={**_XML_HEADERS, "Depth": str(depth)},
        )
        return self._parse(method="PROPFIND", href=href, response=response)[0]

    async def report(
        self, href: str, body: bytes, *, kind: str, depth: int | None = None
    ) -> tuple[list[DavResponse], str | None]:
        if depth is None:
            # RFC 6578 section 3.2: sync-collection is only defined with Depth: 0.
            depth = 0 if kind == "sync-collection" else 1
        response = await self._send(
            "REPORT",
            href,
            kind=kind,
            ok=(207,),
            content=body,
            headers={**_XML_HEADERS, "Depth": str(depth)},
        )
        return self._parse(method="REPORT", href=href, response=response)

    @staticmethod
    def _parse(*, method: str, href: str, response: httpx.Response) -> tuple[list[DavResponse], str | None]:
        try:
            return parse_multistatus(response.content, str(response.url))
        except ET.ParseError as exc:
            raise CalsyncError(f"{method} {href}: invalid multistatus XML: {exc}") from exc

    async def put(
        self, href: str, data: bytes, *, if_match: str | None = None, create_only: bool = False
    ) -> str | None:
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if create_only:
            headers["If-None-Match"] = "*"
        else:
            headers["If-Match"] = if_match if if_match is not None else "*"  # "*": must still exist
        response = await self._send(
            "PUT", href, kind="put", ok=(200, 201, 204), content=data, headers=headers
        )
        return response.headers.get("etag")

    async def delete(self, href: str, *, if_match: str | None = None) -> None:
        headers = {"If-Match": if_match or "*"}
        await self._send("DELETE", href, kind="delete", ok=(200, 204), headers=headers)

    async def mkcalendar(self, href: str, display_name: str) -> None:
        await self._send(
            "MKCALENDAR",
            href,
            kind="mkcalendar",
            ok=(201,),
            content=mkcalendar_body(display_name),
            headers=_XML_HEADERS,
        )
