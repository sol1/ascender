"""Unit tests for the NetBox HTTP client.

Most of these are about what a failure looks like in the job output. A sync
that cannot read /api/status/ is finished, and the operator needs the one line
that says why: wrong URL, rejected token, or NetBox not answering. Reporting
that as a traceback and then carrying on to fail somewhere further along wastes
the only useful information the run produced.
"""

import pytest
import requests

from ascender.main.netbox.client import NetBoxAuthError, NetBoxClient, NetBoxError


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = str(self._payload)

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308) and 'Location' in self.headers

    def json(self):
        return self._payload


class FakeSession:
    """Returns queued responses, or raises a queued exception, per request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.responses[min(len(self.calls), len(self.responses)) - 1]
        if isinstance(item, Exception):
            raise item
        return item


def client_for(*responses, **kwargs):
    kwargs.setdefault('retries', 1)
    return NetBoxClient('https://netbox.example', token='t', session=FakeSession(*responses), **kwargs)


# -- version ----------------------------------------------------------------


def test_version_is_read_from_status():
    client = client_for(FakeResponse(payload={'netbox-version': '4.7.5'}))
    assert client.version == '4.7.5'
    assert client.version_tuple == (4, 7)


def test_version_is_read_once():
    client = client_for(FakeResponse(payload={'netbox-version': '4.7.5'}))
    client.version
    client.version
    client.require_supported_version()
    assert len(client.session.calls) == 1


def test_a_rejected_token_surfaces_rather_than_being_swallowed():
    client = client_for(FakeResponse(status_code=403))
    with pytest.raises(NetBoxAuthError) as caught:
        client.version
    assert 'rejected the credentials' in str(caught.value)


def test_an_unreachable_netbox_surfaces_rather_than_being_swallowed():
    client = client_for(requests.ConnectionError('no route to host'))
    with pytest.raises(NetBoxError) as caught:
        client.version
    assert 'Could not reach NetBox' in str(caught.value)


def test_require_supported_version_stops_on_an_old_release():
    client = client_for(FakeResponse(payload={'netbox-version': '4.5.9'}))
    with pytest.raises(NetBoxError) as caught:
        client.require_supported_version()
    assert '4.5.9 is too old' in str(caught.value)
    assert '4.6 or newer' in str(caught.value)


def test_require_supported_version_allows_a_netbox_that_reports_none():
    """Answering without a version is not the same as not answering."""
    client = client_for(FakeResponse(payload={}))
    assert client.require_supported_version() == ''
    assert client.version_tuple == ()


def test_require_supported_version_allows_a_current_release():
    client = client_for(FakeResponse(payload={'netbox-version': '4.7.5'}))
    assert client.require_supported_version() == '4.7.5'


# -- URL handling -----------------------------------------------------------


@pytest.mark.parametrize(
    'given, expected',
    [
        ('https://netbox.example', 'https://netbox.example/'),
        ('https://netbox.example/', 'https://netbox.example/'),
        ('netbox.example', 'https://netbox.example/'),
        ('http://netbox:8080', 'http://netbox:8080/'),
        ('https://host/netbox/', 'https://host/netbox/'),
    ],
)
def test_url_is_normalised(given, expected):
    assert NetBoxClient(given, session=FakeSession()).base_url == expected


def test_a_subpath_install_keeps_its_prefix():
    client = NetBoxClient('https://host/netbox', session=FakeSession())
    assert client._url('/api/status/') == 'https://host/netbox/api/status/'


def test_an_empty_url_is_rejected():
    with pytest.raises(NetBoxError):
        NetBoxClient('')


def test_a_non_http_url_is_rejected():
    with pytest.raises(NetBoxError):
        NetBoxClient('ftp://netbox.example')


# -- retries ----------------------------------------------------------------


def test_server_errors_are_retried(monkeypatch):
    monkeypatch.setattr('time.sleep', lambda _: None)
    client = client_for(FakeResponse(status_code=503), FakeResponse(payload={'netbox-version': '4.7.5'}), retries=2)
    assert client.version == '4.7.5'
    assert len(client.session.calls) == 2


def test_a_rejected_token_is_not_retried(monkeypatch):
    monkeypatch.setattr('time.sleep', lambda _: None)
    client = client_for(FakeResponse(status_code=403), retries=3)
    with pytest.raises(NetBoxAuthError):
        client.version
    assert len(client.session.calls) == 1


def test_a_client_error_is_not_retried(monkeypatch):
    monkeypatch.setattr('time.sleep', lambda _: None)
    client = client_for(FakeResponse(status_code=400, payload={'detail': 'bad filter'}), retries=3)
    with pytest.raises(NetBoxError) as caught:
        client.get('/api/dcim/devices/')
    assert 'HTTP 400' in str(caught.value)
    assert len(client.session.calls) == 1


# -- headers ----------------------------------------------------------------


def test_the_token_is_sent_when_there_is_one():
    client = client_for(FakeResponse(payload={}))
    assert client.headers['Authorization'] == 'Token t'


def test_no_authorization_header_without_a_token():
    client = NetBoxClient('https://netbox.example', session=FakeSession())
    assert 'Authorization' not in client.headers


# -- following what NetBox hands back ---------------------------------------
#
# `next` and a redirect Location are both chosen by whatever answered the last
# request. Followed as given they let that host move this client to any
# address it likes, which matters more than usual because the fetch runs on
# the control plane rather than inside an execution environment.


def page(results, next_url=None):
    return FakeResponse(payload={'results': results, 'next': next_url})


def test_pagination_follows_next_on_the_same_host():
    client = client_for(
        page([{'id': 1}], 'https://netbox.example/api/dcim/devices/?offset=1'),
        page([{'id': 2}]),
    )
    assert [o['id'] for o in client.paginate('/api/dcim/devices/')] == [1, 2]


def test_pagination_follows_next_when_the_port_is_spelled_out():
    """https://host and https://host:443 are the same origin."""
    client = client_for(
        page([{'id': 1}], 'https://netbox.example:443/api/dcim/devices/?offset=1'),
        page([{'id': 2}]),
    )
    assert [o['id'] for o in client.paginate('/api/dcim/devices/')] == [1, 2]


@pytest.mark.parametrize(
    'next_url',
    [
        'http://169.254.169.254/latest/meta-data/iam/security-credentials/',
        'http://127.0.0.1:6379/',
        'http://10.0.0.5:8080/internal/',
        'https://attacker.example/api/dcim/devices/',
        'https://netbox.example.evil.test/api/dcim/devices/',
        'http://netbox.example/api/dcim/devices/',  # scheme downgrade
        'https://netbox.example:8443/api/dcim/devices/',  # different port
    ],
)
def test_a_next_pointing_elsewhere_is_pulled_back_to_the_configured_host(next_url):
    """The path and query survive; the origin never does."""
    client = client_for(page([{'id': 1}], next_url), page([{'id': 2}]))
    assert [o['id'] for o in client.paginate('/api/dcim/devices/')] == [1, 2]
    second_request_url = client.session.calls[1][1]
    assert second_request_url.startswith('https://netbox.example/')
    for hostile in ('169.254.169.254', '127.0.0.1', '10.0.0.5', 'attacker.example', 'evil.test', ':8443'):
        assert hostile not in second_request_url


def test_the_internal_origin_of_a_proxied_netbox_is_corrected():
    """A NetBox behind a proxy with no X-Forwarded-Host names itself in `next`."""
    client = client_for(
        page([{'id': 1}], 'http://netbox:8080/api/dcim/devices/?limit=500&offset=500'),
        page([{'id': 2}]),
    )
    assert [o['id'] for o in client.paginate('/api/dcim/devices/')] == [1, 2]
    assert client.session.calls[1][1] == 'https://netbox.example/api/dcim/devices/?limit=500&offset=500'


def test_a_redirect_is_refused_rather_than_followed():
    client = client_for(FakeResponse(status_code=302, headers={'Location': 'http://169.254.169.254/'}))
    with pytest.raises(NetBoxError) as caught:
        client.get('/api/status/')
    assert 'redirected' in str(caught.value)
    assert 'http://169.254.169.254/' in str(caught.value)
    # one request, not two: the redirect was never followed
    assert len(client.session.calls) == 1


def test_redirects_are_not_followed_by_requests_itself():
    client = client_for(FakeResponse(payload={'netbox-version': '4.7.5'}))
    client.version
    _, _, kwargs = client.session.calls[0]
    assert kwargs['allow_redirects'] is False


def test_retry_after_cannot_park_the_job_beyond_the_backoff_ceiling(monkeypatch):
    """NetBox behind a rate limiter answers 429 with values like 3600."""
    slept = []
    monkeypatch.setattr('time.sleep', slept.append)
    client = client_for(
        FakeResponse(status_code=429, headers={'Retry-After': '3600'}),
        FakeResponse(payload={'netbox-version': '4.7.5'}),
        retries=2,
    )
    assert client.version == '4.7.5'
    assert slept == [30]
