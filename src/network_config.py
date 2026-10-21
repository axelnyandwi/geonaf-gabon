"""Network settings, loaded explicitly when a network client is requested."""
from dataclasses import dataclass, field
import math
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

STAC_API_URL = 'https://stac.dataspace.copernicus.eu/v1'
CDSE_S3_ENDPOINT = 'https://eodata.dataspace.copernicus.eu'
_ALLOWED_STAC = {'stac.dataspace.copernicus.eu'}
_ALLOWED_S3 = {'eodata.dataspace.copernicus.eu', 'eodata.ams.dataspace.copernicus.eu'}


class NetworkConfigurationError(ValueError):
    """Public configuration failure; never includes values or private paths."""


def validate_endpoint(value, service):
    try:
        parsed = urlsplit(value)
        allowed = _ALLOWED_STAC if service == 'stac' else _ALLOWED_S3
        valid = (parsed.scheme == 'https' and parsed.hostname in allowed
                 and parsed.port in (None, 443) and not parsed.username
                 and not parsed.password and not parsed.query and not parsed.fragment
                 and (service == 'stac' or parsed.path in ('', '/')))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise NetworkConfigurationError('Endpoint HTTPS Copernicus non autorisé.') from None
    return 'https://' + parsed.hostname + parsed.path.rstrip('/')


@dataclass(frozen=True)
class NetworkSettings:
    stac_endpoint: str = STAC_API_URL
    s3_endpoint: str = CDSE_S3_ENDPOINT
    tls_mode: str = 'system'
    ca_bundle: Path | None = field(default=None, repr=False)
    max_attempts: int = 3
    backoff_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    wait_budget_seconds: float = 60.0
    connect_timeout: float = 10.0
    read_timeout: float = 30.0

    def __post_init__(self):
        validate_endpoint(self.stac_endpoint, 'stac')
        validate_endpoint(self.s3_endpoint, 's3')
        if self.tls_mode not in ('system', 'bundle'):
            raise NetworkConfigurationError('GEONAF_TLS_MODE doit être system ou bundle.')
        if (self.tls_mode == 'bundle') != (self.ca_bundle is not None):
            raise NetworkConfigurationError('GEONAF_CA_BUNDLE est requis uniquement en mode bundle.')
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int) or not 1 <= self.max_attempts <= 10:
            raise NetworkConfigurationError('Le budget doit être de 1 à 10 tentatives.')
        for value in (self.backoff_seconds, self.max_delay_seconds, self.wait_budget_seconds,
                      self.connect_timeout, self.read_timeout):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise NetworkConfigurationError('Paramètre réseau numérique invalide.')
        if not 0 < self.connect_timeout <= 120 or not 0 < self.read_timeout <= 120:
            raise NetworkConfigurationError('Les timeouts doivent être compris entre 0 et 120 secondes.')
        if self.max_delay_seconds > 300 or self.wait_budget_seconds > 600:
            raise NetworkConfigurationError('Le budget de temporisation dépasse le plafond autorisé.')

    @classmethod
    def from_environment(cls, *, load_file=True):
        if load_file:
            load_dotenv(Path(__file__).resolve().parents[1] / '.env', override=False)
        try:
            bundle = os.getenv('GEONAF_CA_BUNDLE', '').strip()
            return cls(
                stac_endpoint=validate_endpoint(os.getenv('GEONAF_STAC_ENDPOINT', STAC_API_URL), 'stac'),
                s3_endpoint=validate_endpoint(os.getenv('GEONAF_S3_ENDPOINT', CDSE_S3_ENDPOINT), 's3'),
                tls_mode=os.getenv('GEONAF_TLS_MODE', 'system').strip().lower(),
                ca_bundle=Path(bundle).expanduser() if bundle else None,
                max_attempts=int(os.getenv('GEONAF_NETWORK_MAX_ATTEMPTS', '3')),
                backoff_seconds=float(os.getenv('GEONAF_NETWORK_BACKOFF_SECONDS', '1')),
                max_delay_seconds=float(os.getenv('GEONAF_NETWORK_MAX_DELAY_SECONDS', '30')),
                wait_budget_seconds=float(os.getenv('GEONAF_NETWORK_WAIT_BUDGET_SECONDS', '60')),
                connect_timeout=float(os.getenv('GEONAF_NETWORK_CONNECT_TIMEOUT', '10')),
                read_timeout=float(os.getenv('GEONAF_NETWORK_READ_TIMEOUT', '30')),
            )
        except NetworkConfigurationError:
            raise
        except (TypeError, ValueError):
            raise NetworkConfigurationError('Configuration réseau invalide.') from None
