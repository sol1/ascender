"""HTTP client for the NetBox REST API.

REST rather than GraphQL. NetBox 4.0 swapped Graphene for
Strawberry, which changed the filter syntax, renamed fields and brought query
performance regressions, so the GraphQL schema is the less stable of the two
to build on.

NetBox 4.6 is the floor. Older releases have REST performance problems worth
avoiding, and holding the line at 4.6 keeps the client free of compatibility
shims for endpoints and field names that moved during 3.x and early 4.x.
"""

import logging
import time
from urllib.parse import urljoin, urlparse, urlunparse

import requests

logger = logging.getLogger('ascender.main.netbox')

#: Objects fetched per page. NetBox caps a page at MAX_PAGE_SIZE, which
#: defaults to 1000, and a request for more is silently clamped.
DEFAULT_PAGE_SIZE = 500

#: Give up on a single request after this long.
DEFAULT_TIMEOUT = 60

#: Retry an idempotent GET this many times on a connection error or a 5xx.
DEFAULT_RETRIES = 3

#: Oldest NetBox this integration targets, as (major, minor).
MINIMUM_VERSION = (4, 6)

#: Changelog endpoint. It lived under /api/extras/ before NetBox 4.0.
CHANGELOG_PATH = '/api/core/object-changes/'

#: Used to compare two URLs' origins when neither states its port.
DEFAULT_PORTS = {'http': 80, 'https': 443}

#: Longest a retry will wait, including when NetBox asks for longer.
MAX_BACKOFF_SECONDS = 30


class NetBoxError(RuntimeError):
    """Any failure talking to NetBox that the caller should surface."""


class NetBoxAuthError(NetBoxError):
    """Credentials were rejected."""


class NetBoxClient:
    def __init__(self, url, token=None, verify_ssl=True, timeout=DEFAULT_TIMEOUT, retries=DEFAULT_RETRIES, session=None):
        if not url:
            raise NetBoxError('A NetBox URL is required.')
        parsed = urlparse(url if '://' in url else 'https://{}'.format(url))
        if parsed.scheme not in ('http', 'https'):
            raise NetBoxError('NetBox URL must be http or https, got {!r}.'.format(url))
        # Keep a trailing slash so urljoin treats the whole thing as a base.
        self.base_url = '{}://{}{}/'.format(parsed.scheme, parsed.netloc, parsed.path.rstrip('/'))
        self.token = token
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self.session = session or requests.Session()
        self._version = None

    # -- plumbing ---------------------------------------------------------

    @property
    def headers(self):
        headers = {'Accept': 'application/json', 'User-Agent': 'ascender-netbox-integration'}
        if self.token:
            headers['Authorization'] = 'Token {}'.format(self.token)
        return headers

    def _url(self, path):
        return urljoin(self.base_url, path.lstrip('/'))

    @staticmethod
    def _origin(url):
        """(scheme, host, port) for `url`, with the default port filled in."""
        parsed = urlparse(url)
        port = parsed.port or DEFAULT_PORTS.get(parsed.scheme)
        return (parsed.scheme, (parsed.hostname or '').lower(), port)

    def _pin_to_base(self, url):
        """Put `url` back on the NetBox this client was configured for.

        NetBox hands back absolute URLs: `next` on every paginated list. It
        builds them from the incoming request's Host header, so they are not
        trustworthy twice over. A NetBox behind a proxy that does not set
        X-Forwarded-Host returns its own internal origin, which would send the
        API token to a different address than the credential named. And a host
        that is not NetBox at all can put anything it likes there, which on
        the control plane means any address the control plane can reach.

        Either way the only part worth keeping is the path and query, which is
        what carries the pagination offset and the filters. The origin comes
        from the credential.
        """
        target = urlparse(url)
        if not target.scheme and not target.netloc:
            return url  # already relative, so it resolves against base_url
        if self._origin(url) != self._origin(self.base_url):
            logger.warning(
                'NetBox returned a pagination link to %s://%s, which is not the %s this source is configured for. Continuing against the configured address.',
                target.scheme,
                target.netloc,
                urlparse(self.base_url).netloc,
            )
        return urlunparse(('', '', target.path, target.params, target.query, ''))

    def _request(self, method, path, **kwargs):
        url = self._url(path)
        kwargs.setdefault('timeout', self.timeout)
        kwargs.setdefault('verify', self.verify_ssl)
        # A redirect is the other way the far end gets to choose the next
        # address, so they are not followed. An operator whose NetBox really
        # does redirect gets told where to, and puts that in the credential.
        kwargs.setdefault('allow_redirects', False)
        headers = self.headers
        headers.update(kwargs.pop('headers', None) or {})

        last_exc = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.request(method, url, headers=headers, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt == self.retries:
                    break
                self._backoff(attempt, 'connection error')
                continue

            if response.status_code in (401, 403):
                raise NetBoxAuthError('NetBox rejected the credentials for {} {} (HTTP {}).'.format(method, path, response.status_code))
            # Retry server errors and rate limiting, but nothing the caller can fix.
            if response.status_code in (429,) or response.status_code >= 500:
                if attempt == self.retries:
                    raise NetBoxError('NetBox returned HTTP {} for {} {} after {} attempts.'.format(response.status_code, method, path, attempt))
                self._backoff(attempt, 'HTTP {}'.format(response.status_code), response)
                continue
            if response.is_redirect:
                location = response.headers.get('Location', '')
                raise NetBoxError(
                    'NetBox redirected {} {} to {}. Set the credential URL to the address NetBox actually serves.'.format(
                        method, path, location or 'an unnamed location'
                    )
                )
            if response.status_code >= 400:
                raise NetBoxError('NetBox returned HTTP {} for {} {}: {}'.format(response.status_code, method, path, response.text[:500]))
            return response

        raise NetBoxError('Could not reach NetBox at {} after {} attempts: {}'.format(url, self.retries, last_exc))

    def _backoff(self, attempt, reason, response=None):
        delay = min(2 ** (attempt - 1), MAX_BACKOFF_SECONDS)
        if response is not None:
            try:
                # Honour Retry-After, but under the same ceiling. NetBox behind
                # nginx or Cloudflare answers 429 with values like 3600, and
                # this sleep does not watch the job's cancel flag, so obeying
                # it literally parks the update for an hour with no output.
                delay = max(delay, min(int(response.headers.get('Retry-After', 0)), MAX_BACKOFF_SECONDS))
            except (TypeError, ValueError):
                pass
        logger.warning('NetBox request failed (%s), retrying in %ss', reason, delay)
        time.sleep(delay)

    # -- reads ------------------------------------------------------------

    def get(self, path, params=None):
        return self._request('GET', path, params=params).json()

    def paginate(self, path, params=None, page_size=DEFAULT_PAGE_SIZE):
        """Yield every result across NetBox's offset pagination.

        NetBox returns an absolute `next` URL. Its path and query are followed
        so that filters and the offset stay applied, but the origin always
        comes from the credential rather than from the response.
        """
        params = dict(params or {})
        params.setdefault('limit', page_size)
        url = path
        seen = 0
        while url:
            payload = self._request('GET', url, params=params).json()
            results = payload.get('results')
            if results is None:
                # A detail endpoint, not a list. Hand it back and stop.
                yield payload
                return
            for item in results:
                seen += 1
                yield item
            url = payload.get('next')
            if url:
                url = self._pin_to_base(url)
            # `next` carries its own query string; sending params again would
            # duplicate limit/offset.
            params = None
        logger.debug('NetBox %s returned %s object(s)', path, seen)

    # -- capability detection ---------------------------------------------

    @property
    def version(self):
        """NetBox version string, from /api/status/.

        A failure reading /api/status/ is not something to carry on from. It
        means the URL is wrong, the token is wrong, or NetBox is not up, and
        every request after it will fail the same way. Letting NetBoxError
        through puts the one line that says which in the job output, instead
        of a traceback followed by a second failure further along.

        A NetBox that answers but reports no version is a different case, and
        returns '' so require_supported_version can let it through.
        """
        if self._version is None:
            self._version = str(self.get('/api/status/').get('netbox-version', ''))
        return self._version

    @property
    def version_tuple(self):
        parts = []
        for chunk in self.version.split('.')[:2]:
            try:
                parts.append(int(chunk))
            except ValueError:
                return ()
        return tuple(parts)

    def require_supported_version(self):
        """Fail early and clearly on a NetBox older than this client targets."""
        found = self.version_tuple
        if found and found < MINIMUM_VERSION:
            raise NetBoxError(
                'NetBox {} is too old for this integration, which needs {}.{} or newer.'.format(self.version, MINIMUM_VERSION[0], MINIMUM_VERSION[1])
            )
        if not found:
            logger.warning('Could not determine the NetBox version; continuing and assuming %s.%s or newer.', *MINIMUM_VERSION)
        return self.version

    def check(self):
        """Confirm the URL and token work. Returns the NetBox version."""
        status = self.get('/api/status/')
        return str(status.get('netbox-version', ''))

    # -- writes -----------------------------------------------------------

    def patch(self, path, payload):
        response = self._request('PATCH', path, json=payload, headers={'Content-Type': 'application/json'})
        return response.json() if response.content else {}
