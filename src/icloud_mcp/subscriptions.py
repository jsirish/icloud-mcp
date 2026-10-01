"""Read-only access to subscribed (webcal / ICS feed) calendars.

iCloud lists a subscribed calendar as a CalDAV collection of type ``subscribed``
(not ``calendar``), so ``principal.calendars()`` never returns it, and the
collection itself serves no events over CalDAV. What iCloud does return, in the
``cs:source`` property, is the original feed URL. This module reads those URLs
from the account's own collection listing and fetches the feeds directly.

Security notes:

* Feed URLs routinely embed secret tokens. They are used internally and never
  returned or logged; tool output carries only the feed's host name.
* The account credentials are never sent to a feed host (plain, unauthenticated
  requests only).
* Feed URLs come from the account, not from the caller, but are still treated as
  untrusted: http/https only, no embedded credentials, redirects re-validated,
  private/loopback/link-local addresses refused unless
  ``ICLOUD_SUBSCRIPTIONS_ALLOW_PRIVATE`` is set, response size capped.
* Event text comes from third parties. Callers must treat it as data, never as
  instructions.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import icalendar
import requests
from dateutil.rrule import rrulestr
from defusedxml import ElementTree as SafeET

from .auth import require_auth
from .calendar import _get_caldav_client, _parse_range_bound, _value_to_iso
from .config import config
from .mail_utils import clean_rich_text
from .urls import ensure_icloud_url

logger = logging.getLogger("icloud_mcp.subscriptions")

MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
MAX_OCCURRENCES_PER_EVENT = 1000
MAX_EVENTS = 1000
FETCH_TIMEOUT = (5, 20)
FETCH_WORKERS = 8
DESCRIPTION_MAX_CHARS = 500
TEXT_MAX_CHARS = 200

_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/">'
    "<d:prop><d:displayname/><d:resourcetype/><cs:source/></d:prop></d:propfind>"
)


class FeedError(Exception):
    """A feed could not be fetched or parsed. The message never contains the URL."""


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _discover(email: str, password: str) -> list[dict[str, str]]:
    """Subscribed collections of the account: ``id``, ``name`` and the private ``source``."""
    client = _get_caldav_client(email, password)
    home = client.principal().calendar_home_set
    home_url = str(home.url)
    response = client.request(
        home_url, "PROPFIND", _PROPFIND_BODY, {"Depth": "1", "Content-Type": "application/xml"}
    )
    raw = getattr(response, "raw", None)
    if raw is None:
        raise FeedError("unexpected PROPFIND response from iCloud")
    root = SafeET.fromstring(raw if isinstance(raw, bytes) else raw.encode("utf-8"))

    found = []
    for resp in root.iter():
        if _local(resp.tag) != "response":
            continue
        href = name = source = ""
        subscribed = False
        for el in resp.iter():
            tag = _local(el.tag)
            if tag == "href" and not href and el.text:
                href = el.text.strip()
            elif tag == "displayname" and el.text:
                name = el.text
            elif tag == "subscribed":
                subscribed = True
            elif tag == "source":
                for inner in el.iter():
                    if _local(inner.tag) == "href" and inner.text:
                        source = inner.text.strip()
        if not (subscribed and href and source):
            continue
        found.append({"id": urljoin(home_url, href), "name": name or "Unnamed Subscription", "source": source})
    return found


def _excluded(name: str) -> bool:
    """True when config filters the subscription out (INCLUDE set and not listed, or EXCLUDE listed)."""
    key = name.strip().lower()
    if config.SUBSCRIPTIONS_INCLUDE and key not in config.SUBSCRIPTIONS_INCLUDE:
        return True
    return key in config.SUBSCRIPTIONS_EXCLUDE


def _feed_host(source: str) -> str:
    return urlparse(source).hostname or ""


def list_subscriptions() -> list[dict[str, Any]]:
    """Subscribed calendars with their IDs. The feed URL is never included, only its host."""
    email, password = require_auth()
    result = []
    for sub in _discover(email, password):
        if _excluded(sub["name"]):
            continue
        result.append({"id": sub["id"], "name": sub["name"], "feed_host": _feed_host(sub["source"])})
    return sorted(result, key=lambda s: s["name"].lower())


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def _normalize_feed_url(source: str) -> str:
    parsed = urlparse(source.strip())
    scheme = parsed.scheme.lower()
    if scheme == "webcal":
        scheme = "http"
    elif scheme == "webcals":
        scheme = "https"
    if scheme not in ("http", "https") or not parsed.hostname:
        raise FeedError("feed URL is not http(s)")
    if parsed.username or parsed.password:
        raise FeedError("feed URL contains credentials")
    return parsed._replace(scheme=scheme).geturl()


def _check_destination(url: str) -> None:
    """Refuse feed hosts that resolve to non-public addresses (unless allowed by config)."""
    if config.SUBSCRIPTIONS_ALLOW_PRIVATE:
        return
    host = urlparse(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise FeedError("feed host did not resolve") from e
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise FeedError("feed host resolves to a non-public address")


def _fetch_feed(source: str) -> bytes:
    url = _normalize_feed_url(source)
    for _ in range(MAX_REDIRECTS + 1):
        _check_destination(url)
        try:
            with requests.get(url, stream=True, timeout=FETCH_TIMEOUT, allow_redirects=False) as resp:
                if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    if not location:
                        raise FeedError(f"HTTP {resp.status_code} without Location")
                    url = _normalize_feed_url(urljoin(url, location))
                    continue
                if resp.status_code != 200:
                    raise FeedError(f"HTTP {resp.status_code}")
                body = bytearray()
                for chunk in resp.iter_content(chunk_size=65536):
                    body.extend(chunk)
                    if len(body) > MAX_FEED_BYTES:
                        raise FeedError("feed larger than the size limit")
                return bytes(body)
        except requests.RequestException as e:
            # Exception text can embed the URL (and its token); report the class only.
            raise FeedError(type(e).__name__) from None
    raise FeedError("too many redirects")


# ---------------------------------------------------------------------------
# Parsing and recurrence expansion
# ---------------------------------------------------------------------------


def _to_aware(value: date | datetime, tzname: str) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=ZoneInfo(tzname))
    return datetime.combine(value, datetime.min.time(), tzinfo=ZoneInfo(tzname))


def _localize(value: date | datetime, tzname: str) -> date | datetime:
    """Floating datetimes get the default timezone so every timed value carries an offset."""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=ZoneInfo(tzname))
    return value


def _prop_values(component: Any, name: str) -> list[date | datetime]:
    """All values of a possibly repeated, possibly multi-valued date property (e.g. EXDATE)."""
    raw = component.get(name)
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    values = []
    for item in items:
        for entry in getattr(item, "dts", []):
            values.append(entry.dt)
    return values


def _occurrence_key(value: date | datetime, tzname: str) -> Any:
    if isinstance(value, datetime):
        return _to_aware(value, tzname).astimezone(UTC)
    return value


def _event_end(component: Any, start: date | datetime) -> date | datetime:
    if component.get("DTEND") is not None:
        return component.decoded("DTEND")
    if component.get("DURATION") is not None:
        return start + component.decoded("DURATION")
    return start + timedelta(days=1) if not isinstance(start, datetime) else start


def _expand(
    component: Any, range_start: datetime, range_end: datetime, tzname: str
) -> list[tuple[date | datetime, date | datetime]]:
    """(start, end) of each occurrence of ``component`` overlapping the range."""
    start = component.decoded("DTSTART")
    end = _event_end(component, start)
    all_day = not isinstance(start, datetime)
    length = _to_aware(end, tzname) - _to_aware(start, tzname)
    rrule = component.get("RRULE")

    def overlaps(s: date | datetime) -> bool:
        s_aware = _to_aware(s, tzname)
        return s_aware < range_end and s_aware + length > range_start

    if rrule is None:
        return [(start, end)] if overlaps(start) else []

    anchor = _to_aware(start, tzname)
    rule = rrulestr(rrule.to_ical().decode(), dtstart=anchor)
    excluded = {_occurrence_key(v, tzname) for v in _prop_values(component, "EXDATE")}
    out = []
    for occurrence in rule.between(range_start - length, range_end, inc=True):
        if len(out) >= MAX_OCCURRENCES_PER_EVENT:
            break
        value: date | datetime = occurrence.date() if all_day else occurrence
        if _occurrence_key(value, tzname) in excluded or not overlaps(value):
            continue
        out.append((value, (value + (end - start)) if all_day else occurrence + length))
    return out


def _trim(value: Any, limit: int) -> str:
    text = clean_rich_text(value).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_feed(
    data: bytes, calendar_name: str, calendar_id: str, range_start: datetime, range_end: datetime
) -> list[dict[str, Any]]:
    """Events of one ICS feed overlapping ``[range_start, range_end)``."""
    try:
        cal = icalendar.Calendar.from_ical(data)
    except Exception as e:
        raise FeedError("feed is not valid iCalendar") from e

    tzname = config.DEFAULT_TIMEZONE
    masters, overrides = [], {}
    for component in cal.walk("VEVENT"):
        if component.get("DTSTART") is None or component.get("STATUS") == "CANCELLED":
            continue
        recurrence_id = component.get("RECURRENCE-ID")
        if recurrence_id is not None:
            key = (str(component.get("UID")), _occurrence_key(recurrence_id.dt, tzname))
            overrides[key] = component
        else:
            masters.append(component)

    events = []
    for component in masters:
        try:
            occurrences = _expand(component, range_start, range_end, tzname)
        except Exception as e:
            logger.debug("Skipping unparsable event in %s: %s", calendar_name, type(e).__name__)
            continue
        uid = str(component.get("UID") or "")
        for start, end in occurrences:
            source = component
            override = overrides.get((uid, _occurrence_key(start, tzname)))
            if override is not None:
                source = override
                start = override.decoded("DTSTART")
                end = _event_end(override, start)
                if not (_to_aware(start, tzname) < range_end and _to_aware(end, tzname) > range_start):
                    continue
            events.append(
                {
                    "id": f"{calendar_id}#{uid}",
                    "summary": _trim(source.get("SUMMARY"), TEXT_MAX_CHARS),
                    "description": _trim(source.get("DESCRIPTION"), DESCRIPTION_MAX_CHARS),
                    "location": _trim(source.get("LOCATION"), TEXT_MAX_CHARS),
                    "start": _value_to_iso(_localize(start, tzname)),
                    "end": _value_to_iso(_localize(end, tzname)),
                    "all_day": not isinstance(start, datetime),
                    "recurring": component.get("RRULE") is not None,
                    "calendar": calendar_name,
                    "subscribed": True,
                }
            )
    return events


# ---------------------------------------------------------------------------
# Operation
# ---------------------------------------------------------------------------


def _sort_key(event: dict[str, Any]) -> str:
    return event.get("start") or ""


def list_subscribed_events(
    calendar_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    email, password = require_auth()
    now = datetime.now(UTC)
    start = _parse_range_bound(start_date, now, end_of_day=False)
    end = _parse_range_bound(end_date, now + timedelta(days=30), end_of_day=True)
    if end <= start:
        raise ValueError("end_date must be after start_date")

    subscriptions = [s for s in _discover(email, password) if not _excluded(s["name"])]
    if calendar_id:
        wanted = ensure_icloud_url(calendar_id, "calendar")
        subscriptions = [s for s in subscriptions if s["id"].rstrip("/") == wanted.rstrip("/")]
        if not subscriptions:
            raise ValueError("calendar_id is not a subscribed calendar; see calendar_list_subscriptions")

    def load(sub: dict[str, str]) -> tuple[dict[str, str], list[dict[str, Any]] | None, str | None]:
        try:
            data = _fetch_feed(sub["source"])
            return sub, parse_feed(data, sub["name"], sub["id"], start, end), None
        except FeedError as e:
            return sub, None, str(e)

    events: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        for sub, feed_events, error in pool.map(load, subscriptions):
            if error is not None:
                errors.append({"calendar": sub["name"], "error": error})
            else:
                events.extend(feed_events or [])
    events.sort(key=_sort_key)
    truncated = len(events) > MAX_EVENTS
    return {
        "events": events[:MAX_EVENTS],
        "truncated": truncated,
        "feeds_checked": len(subscriptions),
        "feed_errors": errors,
    }
