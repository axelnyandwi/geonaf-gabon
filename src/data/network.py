"""Verified, client-scoped TLS and bounded retries. No initialization on import."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import math
import random
import ssl
import time
from urllib.parse import urlsplit

from botocore import exceptions as boto_errors
from botocore.config import Config
from botocore.httpsession import URLLib3Session
from botocore.utils import get_environ_proxies
from pystac import Link
from pystac_client.stac_api_io import StacApiIO
import requests
from requests.adapters import HTTPAdapter
from requests.utils import get_environ_proxies as requests_proxies

from src.network_config import NetworkSettings, NetworkConfigurationError

logger = logging.getLogger(__name__)
_TRANSIENT_HTTP = {429, 500, 502, 503, 504}
_S3_CODES = {'AccessDenied', 'InvalidAccessKeyId', 'SignatureDoesNotMatch',
             'ExpiredToken', 'RequestTimeTooSkewed', 'SlowDown', 'ServiceUnavailable',
             'InternalError', 'NoSuchKey', 'NoSuchBucket', 'RequestTimeout'}


class NetworkError(RuntimeError):
    """Structured public failure. Original exceptions are never formatted/logged."""
    def __init__(self, message=None, *, service='stac', category='protocol',
                 status_code=None, s3_code=None, retry_after=None, attempts=1):
        self.service = service
        self.category = category
        self.status_code = status_code if isinstance(status_code, int) and not isinstance(status_code, bool) else None
        self.s3_code = s3_code if s3_code in _S3_CODES else None
        self.retry_after = retry_after
        self.attempts = attempts
        if message is None:
            label = {'tls': 'SSLError', 'timeout': 'Timeout', 'connection': 'ConnectionError',
                     'proxy': 'ProxyError', 'http': 'HTTPError', 's3': 'S3Error'}.get(category, 'NetworkError')
            status = f', HTTP {self.status_code}' if self.status_code is not None else ''
            message = f'Connexion {service.upper()} interrompue ({label}{status}).'
        super().__init__(message)


def quiet_transport_logs():
    # Opt in at client creation, never on business-module import. Upstream SDK
    # DEBUG records can include Authorization, URLs, payloads and proxy secrets.
    namespaces = ('botocore', 'boto3', 's3transfer', 'urllib3', 'requests', 'pystac_client')
    names = set(logging.Logger.manager.loggerDict) | set(namespaces)
    for name in names:
        if any(name == prefix or name.startswith(prefix + '.') for prefix in namespaces):
            logging.getLogger(name).setLevel(logging.CRITICAL + 1)


def tls_context(settings):
    """Build trust before creating pools; never download or install authorities."""
    try:
        if settings.tls_mode == 'system':
            # Reuse the already injected implementation, if any, instead of
            # nesting external truststore around pip's vendored truststore.
            if ssl.SSLContext.__module__ in ('truststore._api', 'pip._vendor.truststore._api'):
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            else:
                import truststore
                context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        else:
            # An injected SSLContext may silently add system roots to a bundle.
            # Refuse that ambiguity; clean environments use only the given CA.
            if ssl.SSLContext.__module__ != 'ssl':
                raise NetworkConfigurationError('Mode bundle incompatible avec une injection SSL tierce ; utiliser un environnement Python propre.')
            path = settings.ca_bundle
            if path is None or not path.is_file() or path.stat().st_size > 8*1024*1024:
                raise NetworkConfigurationError('Bundle CA absent ou trop volumineux.')
            with path.open('rb') as source:
                content = source.read(8*1024*1024 + 1)
            if len(content) > 8*1024*1024 or b'PRIVATE KEY' in content or b'BEGIN CERTIFICATE' not in content:
                raise NetworkConfigurationError('Le bundle doit contenir uniquement des certificats CA publics.')
            context = ssl.create_default_context(cadata=content.decode('ascii'))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        return context
    except NetworkConfigurationError:
        raise
    except (OSError, ValueError, ImportError, ssl.SSLError):
        raise NetworkConfigurationError('Impossible de charger la confiance TLS configurée.') from None


def retry_after_seconds(value, *, now=None):
    if not value:
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = (date - (now or datetime.now(timezone.utc))).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def classify_error(error, service):
    if isinstance(error, NetworkError):
        return error
    underlying = getattr(error, 'last_exception', None)
    if isinstance(underlying, Exception) and underlying is not error:
        return classify_error(underlying, service)
    response = getattr(error, 'response', None)
    if isinstance(response, dict):
        metadata = response.get('ResponseMetadata', {})
        return NetworkError(service=service, category='s3',
                            status_code=metadata.get('HTTPStatusCode'),
                            s3_code=response.get('Error', {}).get('Code'),
                            retry_after=retry_after_seconds(metadata.get('HTTPHeaders', {}).get('retry-after')))
    status = getattr(error, 'status_code', None)
    if status is None and response is not None:
        status = getattr(response, 'status_code', None)
    category = 'protocol'
    if isinstance(error, (ssl.SSLError, requests.exceptions.SSLError, boto_errors.SSLError)):
        category = 'tls'
    elif isinstance(error, (requests.exceptions.ProxyError, boto_errors.ProxyConnectionError)):
        category = 'proxy'
    elif isinstance(error, (requests.exceptions.Timeout, boto_errors.ConnectTimeoutError, boto_errors.ReadTimeoutError)):
        category = 'timeout'
    elif isinstance(error, (requests.exceptions.ConnectionError, boto_errors.EndpointConnectionError,
                            boto_errors.ConnectionClosedError, boto_errors.ResponseStreamingError,
                            boto_errors.IncompleteReadError)):
        category = 'connection'
    elif status is not None:
        category = 'http'
    return NetworkError(service=service, category=category, status_code=status)


def run_with_retry(action, settings, *, service):
    waited = 0.0
    for attempt in range(1, settings.max_attempts + 1):
        try:
            return action()
        except (NetworkConfigurationError, ValueError):
            # Domain validation (e.g. metadata size/checksum) is not a transport
            # failure and must preserve its original scientific contract.
            raise
        except Exception as original:
            error = classify_error(original, service)
            error.attempts = attempt
            transient = (error.category in ('timeout', 'connection') or
                         error.status_code in _TRANSIENT_HTTP)
            # TLS/proxy/authentication failures must never become status retries.
            if error.category in ('tls', 'proxy') or error.status_code in (401, 403):
                transient = False
            retry = transient and attempt < settings.max_attempts
            delay = random.uniform(0, min(settings.max_delay_seconds,
                                          settings.backoff_seconds * 2**(attempt-1)))
            if error.retry_after is not None:
                delay = max(delay, error.retry_after)
            if delay > settings.max_delay_seconds or waited + delay > settings.wait_budget_seconds:
                retry = False
            logger.warning('network_failure service=%s category=%s status=%s code=%s attempt=%s retry=%s',
                           service, error.category, error.status_code, error.s3_code, attempt, retry)
            if not retry:
                raise error from None
            time.sleep(delay)
            waited += delay


class TLSAdapter(HTTPAdapter):
    """Requests pool with explicit trust, independent of CA environment variables."""
    def __init__(self, context):
        self.context = context
        super().__init__(max_retries=0)

    def init_poolmanager(self, *args, **kwargs):
        kwargs['ssl_context'] = self.context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **kwargs):
        kwargs['ssl_context'] = self.context
        kwargs['proxy_ssl_context'] = self.context
        return super().proxy_manager_for(proxy, **kwargs)

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        if verify is not True:
            raise NetworkConfigurationError('TLS vérifié et contexte explicite obligatoires.')
        host, kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        kwargs.pop('ca_certs', None)
        kwargs.pop('ca_cert_dir', None)
        kwargs.update(ssl_context=self.context, cert_reqs='CERT_REQUIRED')
        return host, kwargs

    def cert_verify(self, conn, url, verify, cert):
        if verify is not True or not url.startswith('https://'):
            raise NetworkConfigurationError('TLS vérifié obligatoire.')
        # Do not let Requests add certifi or an ambient CA bundle to this pool.
        conn.cert_reqs = 'CERT_REQUIRED'


class _RejectHTTP(HTTPAdapter):
    def send(self, *args, **kwargs):
        raise NetworkConfigurationError('Les connexions HTTP non chiffrées sont interdites.')


def secure_session(settings):
    context = tls_context(settings)
    quiet_transport_logs()
    session = requests.Session()
    session.trust_env = False  # no netrc authentication or implicit CA override
    session.mount('https://', TLSAdapter(context))
    session.mount('http://', _RejectHTTP(max_retries=0))
    return session


class SecureStacIO(StacApiIO):
    def __init__(self, settings):
        # Avoid StacApiIO's implicit retry adapters and debug request logging.
        super().__init__(timeout=(settings.connect_timeout, settings.read_timeout), max_retries=0)
        self.session.close()
        self.session = secure_session(settings)
        self.settings = settings

    def read_text(self, source, *args, **kwargs):
        # StacApiIO otherwise permits plain local filenames. Catalog links must
        # never make this network reader open a local secret/configuration file.
        if not isinstance(source, Link):
            try:
                allowed = urlsplit(str(source)).scheme == 'https'
            except ValueError:
                allowed = False
            if not allowed:
                raise NetworkConfigurationError('Source STAC locale ou non chiffrée interdite.')
        return super().read_text(source, *args, **kwargs)

    def request(self, href, method=None, headers=None, parameters=None):
        expected = urlsplit(self.settings.stac_endpoint)
        try:
            parsed = urlsplit(href)
            valid = (parsed.scheme == 'https' and parsed.netloc == expected.netloc
                     and not parsed.username and not parsed.password and not parsed.fragment)
        except (TypeError, ValueError):
            valid = False
        if not valid or (method or 'GET') not in ('GET', 'POST'):
            raise NetworkConfigurationError('Lien ou méthode STAC non autorisé.')
        def action():
            options = {'json': parameters} if method == 'POST' else {'params': parameters}
            with self.session.request(method or 'GET', href, headers=headers, timeout=(self.settings.connect_timeout, self.settings.read_timeout),
                                      proxies=requests_proxies(href), verify=True,
                                      allow_redirects=False, **options) as response:
                if response.status_code != 200:
                    raise NetworkError(service='stac', category='http', status_code=response.status_code,
                                       retry_after=retry_after_seconds(response.headers.get('Retry-After')))
                return response.content.decode('utf-8')
        return run_with_retry(action, self.settings, service='stac')


class _S3TLS(URLLib3Session):
    """Small isolated botocore integration: explicit SSLContext for every pool."""
    def __init__(self, settings, *, context=None, **kwargs):
        self.settings = settings
        # Validate trust before initializing any pool.
        self.context = context if context is not None else tls_context(settings)
        super().__init__(**kwargs)

    def send(self, request):
        parsed = urlsplit(request.url)
        expected = urlsplit(self.settings.s3_endpoint)
        if (parsed.scheme != 'https' or parsed.netloc != expected.netloc
                or parsed.username or parsed.password):
            raise NetworkConfigurationError('Redirection S3 vers un endpoint non autorisé.')
        return super().send(request)

    def _get_ssl_context(self):
        return self.context

    def _setup_ssl_cert(self, conn, url, verify):
        if verify is not True or not url.startswith('https://'):
            raise NetworkConfigurationError('TLS S3 vérifié obligatoire.')
        conn.cert_reqs = 'CERT_REQUIRED'

    def _setup_proxy_ssl_context(self, proxy_url):
        return self.context


def install_s3_tls(client, settings, *, context=None):
    # Botocore has no public SSLContext argument. Confine its internal seam to
    # this function and test it against the installed SDK, failing closed.
    try:
        old = client._endpoint.http_session
        replacement = _S3TLS(settings, context=context, verify=True,
                              proxies=get_environ_proxies(settings.s3_endpoint),
                              timeout=(settings.connect_timeout, settings.read_timeout),
                              max_pool_connections=client.meta.config.max_pool_connections)
        client._endpoint.http_session = replacement
        old.close()
    except NetworkConfigurationError:
        raise
    except (AttributeError, TypeError):
        raise NetworkConfigurationError('Version du transport botocore non prise en charge.') from None


def create_s3_client(boto3_module, settings, access_key, secret_key):
    # The context is validated before constructing the client; no SSL injection.
    context = tls_context(settings)
    quiet_transport_logs()
    try:
        client = boto3_module.client('s3', endpoint_url=settings.s3_endpoint,
                                     aws_access_key_id=access_key, aws_secret_access_key=secret_key,
                                     region_name='default', verify=True,
                                     config=Config(signature_version='s3v4',
                                                   retries={'total_max_attempts': 1, 'mode': 'standard'},
                                                   connect_timeout=settings.connect_timeout,
                                                   read_timeout=settings.read_timeout,
                                                   s3={'addressing_style': 'path'}))
    except Exception:
        raise NetworkConfigurationError('Impossible de créer le client S3 configuré.') from None
    try:
        install_s3_tls(client, settings, context=context)
    except Exception:
        client.close()
        raise
    return client
