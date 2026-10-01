import socket
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from icloud_mcp import subscriptions
from icloud_mcp.config import config
from icloud_mcp.subscriptions import FeedError, parse_feed

SECRET = "tok_SECRET123"
FEED_URL = f"https://feeds.example.org/cal.ics?token={SECRET}"

RANGE_START = datetime(2026, 3, 1, tzinfo=UTC)
RANGE_END = datetime(2026, 3, 31, 23, 59, 59, tzinfo=UTC)


def ics(*events: str) -> bytes:
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n" + "".join(events) + "END:VCALENDAR\r\n").encode()


def vevent(*lines: str) -> str:
    return "BEGIN:VEVENT\r\nDTSTAMP:20260101T000000Z\r\n" + "".join(f"{x}\r\n" for x in lines) + "END:VEVENT\r\n"


def run(data: bytes, start=RANGE_START, end=RANGE_END):
    return parse_feed(data, "Team", "https://p1-caldav.icloud.com/1/calendars/ABC/", start, end)


# ---------------------------------------------------------------------------
# parse_feed
# ---------------------------------------------------------------------------


def test_single_timed_event_in_range():
    events = run(ics(vevent("UID:a", "DTSTART:20260310T180000Z", "DTEND:20260310T190000Z", "SUMMARY:Game", "LOCATION:Field 2")))
    assert len(events) == 1
    e = events[0]
    assert e["summary"] == "Game" and e["location"] == "Field 2"
    assert e["start"] == "2026-03-10T18:00:00+00:00"
    assert e["all_day"] is False and e["recurring"] is False and e["subscribed"] is True
    assert e["calendar"] == "Team"
    assert e["id"].endswith("#a")


def test_events_outside_range_are_dropped():
    data = ics(
        vevent("UID:a", "DTSTART:20260201T180000Z", "DTEND:20260201T190000Z", "SUMMARY:Before"),
        vevent("UID:b", "DTSTART:20260410T180000Z", "DTEND:20260410T190000Z", "SUMMARY:After"),
    )
    assert run(data) == []


def test_all_day_event_uses_dates():
    events = run(ics(vevent("UID:a", "DTSTART;VALUE=DATE:20260315", "DTEND;VALUE=DATE:20260316", "SUMMARY:Holiday")))
    assert events[0]["all_day"] is True
    assert events[0]["start"] == "2026-03-15" and events[0]["end"] == "2026-03-16"


def test_all_day_event_without_dtend_lasts_one_day():
    events = run(ics(vevent("UID:a", "DTSTART;VALUE=DATE:20260315", "SUMMARY:Holiday")))
    assert events[0]["end"] == "2026-03-16"


def test_weekly_recurrence_expands_and_honours_exdate():
    data = ics(
        vevent(
            "UID:r",
            "DTSTART:20260302T180000Z",
            "DTEND:20260302T190000Z",
            "RRULE:FREQ=WEEKLY;BYDAY=MO",
            "EXDATE:20260316T180000Z",
            "SUMMARY:Practice",
        )
    )
    starts = [e["start"][:10] for e in run(data)]
    assert starts == ["2026-03-02", "2026-03-09", "2026-03-23", "2026-03-30"]
    assert all(e["recurring"] for e in run(data))


def test_recurrence_override_replaces_occurrence():
    data = ics(
        vevent("UID:r", "DTSTART:20260302T180000Z", "DTEND:20260302T190000Z", "RRULE:FREQ=WEEKLY;COUNT=3", "SUMMARY:Practice"),
        vevent(
            "UID:r",
            "RECURRENCE-ID:20260309T180000Z",
            "DTSTART:20260309T200000Z",
            "DTEND:20260309T210000Z",
            "SUMMARY:Practice (moved)",
        ),
    )
    events = run(data)
    assert [(e["summary"], e["start"][:16]) for e in events] == [
        ("Practice", "2026-03-02T18:00"),
        ("Practice (moved)", "2026-03-09T20:00"),
        ("Practice", "2026-03-16T18:00"),
    ]


def test_cancelled_event_is_skipped():
    assert run(ics(vevent("UID:a", "DTSTART:20260310T180000Z", "STATUS:CANCELLED", "SUMMARY:Off"))) == []


def test_floating_time_uses_default_timezone():
    events = run(ics(vevent("UID:a", "DTSTART:20260310T180000", "DTEND:20260310T190000", "SUMMARY:Local")))
    # conftest sets DEFAULT_TIMEZONE=Europe/Berlin; 10 March is before DST, so +01:00.
    assert events[0]["start"] == "2026-03-10T18:00:00+01:00"
    assert events[0]["end"] == "2026-03-10T19:00:00+01:00"


def test_garbage_feed_raises_feed_error():
    with pytest.raises(FeedError):
        run(b"<html>not a calendar</html>")


def test_long_description_is_truncated():
    events = run(ics(vevent("UID:a", "DTSTART:20260310T180000Z", "DTEND:20260310T190000Z", "DESCRIPTION:" + "x" * 5000)))
    assert len(events[0]["description"]) <= subscriptions.DESCRIPTION_MAX_CHARS


# ---------------------------------------------------------------------------
# URL handling and fetching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("webcal://h.example/x.ics", "http://h.example/x.ics"),
        ("webcals://h.example/x.ics", "https://h.example/x.ics"),
        ("https://h.example/x.ics?t=1", "https://h.example/x.ics?t=1"),
    ],
)
def test_normalize_feed_url(raw, expected):
    assert subscriptions._normalize_feed_url(raw) == expected


@pytest.mark.parametrize("raw", ["file:///etc/passwd", "ftp://h.example/x", "https://user:pw@h.example/x", "not a url"])
def test_normalize_feed_url_rejects(raw):
    with pytest.raises(FeedError):
        subscriptions._normalize_feed_url(raw)


def _resolve_to(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port: [(socket.AF_INET, 0, 0, "", (address, 0))])


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "0.0.0.0"])
def test_non_public_destinations_refused(monkeypatch, address):
    _resolve_to(monkeypatch, address)
    with pytest.raises(FeedError, match="non-public"):
        subscriptions._check_destination("https://h.example/x.ics")


def test_public_destination_allowed(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    subscriptions._check_destination("https://h.example/x.ics")


def test_private_destination_allowed_when_configured(monkeypatch):
    _resolve_to(monkeypatch, "10.0.0.5")
    monkeypatch.setattr(config, "SUBSCRIPTIONS_ALLOW_PRIVATE", True)
    subscriptions._check_destination("http://h.example/x.ics")


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.is_redirect = status in (301, 302, 303, 307, 308)

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def public_dns(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")


def test_fetch_returns_body(monkeypatch, public_dns):
    monkeypatch.setattr(subscriptions.requests, "get", lambda url, **kw: FakeResponse(body=b"BEGIN:VCALENDAR"))
    assert subscriptions._fetch_feed(FEED_URL) == b"BEGIN:VCALENDAR"


def test_fetch_does_not_send_credentials(monkeypatch, public_dns):
    seen = {}

    def fake_get(url, **kw):
        seen.update(kw)
        return FakeResponse(body=b"x")

    monkeypatch.setattr(subscriptions.requests, "get", fake_get)
    subscriptions._fetch_feed(FEED_URL)
    assert "auth" not in seen and "headers" not in seen
    assert seen["allow_redirects"] is False


def test_fetch_http_error_has_status_but_not_url(monkeypatch, public_dns):
    monkeypatch.setattr(subscriptions.requests, "get", lambda url, **kw: FakeResponse(status=410))
    with pytest.raises(FeedError) as exc:
        subscriptions._fetch_feed(FEED_URL)
    assert "410" in str(exc.value) and SECRET not in str(exc.value)


def test_fetch_network_error_does_not_leak_url(monkeypatch, public_dns):
    def boom(url, **kw):
        raise subscriptions.requests.ConnectionError(f"failed for {url}")

    monkeypatch.setattr(subscriptions.requests, "get", boom)
    with pytest.raises(FeedError) as exc:
        subscriptions._fetch_feed(FEED_URL)
    assert SECRET not in str(exc.value) and "ConnectionError" in str(exc.value)


def test_fetch_enforces_size_cap(monkeypatch, public_dns):
    monkeypatch.setattr(subscriptions, "MAX_FEED_BYTES", 100)
    monkeypatch.setattr(subscriptions.requests, "get", lambda url, **kw: FakeResponse(body=b"x" * 1000))
    with pytest.raises(FeedError, match="size limit"):
        subscriptions._fetch_feed(FEED_URL)


def test_redirect_to_private_address_refused(monkeypatch):
    answers = {"public.example": "93.184.216.34", "internal.example": "10.0.0.5"}
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda host, port: [(socket.AF_INET, 0, 0, "", (answers[host], 0))]
    )
    monkeypatch.setattr(
        subscriptions.requests,
        "get",
        lambda url, **kw: FakeResponse(status=302, headers={"Location": "http://internal.example/x"}),
    )
    with pytest.raises(FeedError, match="non-public"):
        subscriptions._fetch_feed("https://public.example/cal.ics")


def test_redirect_loop_is_bounded(monkeypatch, public_dns):
    monkeypatch.setattr(
        subscriptions.requests, "get", lambda url, **kw: FakeResponse(status=302, headers={"Location": url})
    )
    with pytest.raises(FeedError, match="redirects"):
        subscriptions._fetch_feed(FEED_URL)


# ---------------------------------------------------------------------------
# Discovery and the tool operations
# ---------------------------------------------------------------------------

HOME = "https://p1-caldav.icloud.com:443/123/calendars/"

PROPFIND = f"""<?xml version="1.0"?>
<multistatus xmlns="DAV:" xmlns:cs="http://calendarserver.org/ns/">
 <response><href>/123/calendars/AAA/</href><propstat><prop>
   <displayname>Packers</displayname>
   <resourcetype><collection/><cs:subscribed/></resourcetype>
   <cs:source><href>{FEED_URL}</href></cs:source>
 </prop></propstat></response>
 <response><href>/123/calendars/BBB/</href><propstat><prop>
   <displayname>Yacht Club</displayname>
   <resourcetype><collection/><cs:subscribed/></resourcetype>
   <cs:source><href>https://other.example/y.ics</href></cs:source>
 </prop></propstat></response>
 <response><href>/123/calendars/CCC/</href><propstat><prop>
   <displayname>Family</displayname>
   <resourcetype><collection/><calendar xmlns="urn:ietf:params:xml:ns:caldav"/></resourcetype>
 </prop></propstat></response>
</multistatus>"""


@pytest.fixture
def fake_account(monkeypatch):
    class FakeClient:
        def principal(self):
            return SimpleNamespace(calendar_home_set=SimpleNamespace(url=HOME))

        def request(self, url, method, body, headers):
            assert method == "PROPFIND" and headers["Depth"] == "1"
            return SimpleNamespace(raw=PROPFIND)

    monkeypatch.setattr(subscriptions, "_get_caldav_client", lambda e, p: FakeClient())
    monkeypatch.setattr(config, "SUBSCRIPTIONS_INCLUDE", frozenset())
    monkeypatch.setattr(config, "SUBSCRIPTIONS_EXCLUDE", frozenset())


def test_discover_finds_only_subscribed_collections(fake_account):
    found = subscriptions._discover("e", "p")
    assert [s["name"] for s in found] == ["Packers", "Yacht Club"]
    assert found[0]["id"] == "https://p1-caldav.icloud.com/123/calendars/AAA/".replace("https://p1-caldav.icloud.com", "https://p1-caldav.icloud.com:443")
    assert found[0]["source"] == FEED_URL


def test_list_subscriptions_hides_feed_url(fake_account):
    result = subscriptions.list_subscriptions()
    assert {s["name"] for s in result} == {"Packers", "Yacht Club"}
    flat = repr(result)
    assert SECRET not in flat and "token" not in flat
    assert {s["feed_host"] for s in result} == {"feeds.example.org", "other.example"}


def test_include_limits_to_named_subscriptions(fake_account, monkeypatch):
    monkeypatch.setattr(config, "SUBSCRIPTIONS_INCLUDE", frozenset({"packers"}))
    assert [s["name"] for s in subscriptions.list_subscriptions()] == ["Packers"]


def test_include_filter_applies_to_event_reads(fake_account, monkeypatch):
    monkeypatch.setattr(config, "SUBSCRIPTIONS_INCLUDE", frozenset({"packers"}))
    monkeypatch.setattr(subscriptions, "_fetch_feed", _feed_for)
    result = subscriptions.list_subscribed_events(None, "2026-03-01", "2026-03-31")
    assert result["feeds_checked"] == 1 and result["feed_errors"] == []


def test_include_and_exclude_filters(fake_account, monkeypatch):
    monkeypatch.setattr(config, "SUBSCRIPTIONS_INCLUDE", frozenset({"packers", "yacht club"}))
    monkeypatch.setattr(config, "SUBSCRIPTIONS_EXCLUDE", frozenset({"yacht club"}))
    assert [s["name"] for s in subscriptions.list_subscriptions()] == ["Packers"]


def _feed_for(source: str) -> bytes:
    if "other.example" in source:
        raise FeedError("HTTP 410")
    return ics(vevent("UID:g", "DTSTART:20260310T180000Z", "DTEND:20260310T190000Z", "SUMMARY:Game"))


def test_list_subscribed_events_reports_failures_and_hides_urls(fake_account, monkeypatch):
    monkeypatch.setattr(subscriptions, "_fetch_feed", _feed_for)
    result = subscriptions.list_subscribed_events(None, "2026-03-01", "2026-03-31")
    assert [e["summary"] for e in result["events"]] == ["Game"]
    assert result["feeds_checked"] == 2
    assert result["feed_errors"] == [{"calendar": "Yacht Club", "error": "HTTP 410"}]
    assert SECRET not in repr(result)


def test_list_subscribed_events_for_one_calendar(fake_account, monkeypatch):
    monkeypatch.setattr(subscriptions, "_fetch_feed", _feed_for)
    result = subscriptions.list_subscribed_events(
        "https://p1-caldav.icloud.com:443/123/calendars/AAA/", "2026-03-01", "2026-03-31"
    )
    assert result["feeds_checked"] == 1 and result["feed_errors"] == []


def test_unknown_calendar_id_is_rejected(fake_account):
    with pytest.raises(ValueError, match="not a subscribed calendar"):
        subscriptions.list_subscribed_events("https://p1-caldav.icloud.com:443/123/calendars/CCC/")


def test_foreign_host_calendar_id_is_rejected(fake_account):
    with pytest.raises(ValueError, match="untrusted host"):
        subscriptions.list_subscribed_events("https://evil.example/123/calendars/AAA/")


def test_end_before_start_is_rejected(fake_account):
    with pytest.raises(ValueError, match="end_date"):
        subscriptions.list_subscribed_events(None, "2026-03-10", "2026-03-01")


def test_default_range_is_now_to_thirty_days(fake_account, monkeypatch):
    seen = {}

    def spy(data, name, cid, start, end):
        seen["span"] = end - start
        return []

    monkeypatch.setattr(subscriptions, "_fetch_feed", lambda s: b"")
    monkeypatch.setattr(subscriptions, "parse_feed", spy)
    subscriptions.list_subscribed_events()
    assert timedelta(days=29) < seen["span"] <= timedelta(days=31)
