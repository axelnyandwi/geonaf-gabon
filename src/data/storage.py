"""Storage used by the application (D4): files (default) or PostGIS, chosen by one explicit setting.

``GEONAF_DB_BACKEND`` (``src.data.database``) selects the mode, ``file`` when unset:

* **file**: the current behaviour, unchanged. Territories from ``data/territories``, acquisitions from
  the strict manifests of ``data/raw/sentinel2``, indicator results from ``data/indicators``. No
  database driver is imported, no connection is needed (Streamlit preproduction, demo pack).
* **postgres**: the same three readers backed by the ``geonaf`` schema (migrations 0001 and 0002).
  ``GEONAF_DATABASE_URL`` and ``GEONAF_DB_ENV`` are required; every read is a guarded transaction
  (:class:`src.data.postgis_session.PostgisSessions`: TLS for a remote host, environment mark,
  READ ONLY, statement timeout, R5 bis search path). The application never writes to the database:
  data reach it through the import tools only.

There is no silent fallback: if PostGIS is selected and unreachable, reads raise
:class:`~src.data.database.DatabaseConnectionError` (message without secret) and the interface says
so; showing file data instead would misstate what is displayed. Nothing connects at import time or
when the store is opened: the first read does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from src.data.database import selected_backend

FILE, POSTGRES = "file", "postgres"
MODE_LABELS = {FILE: "fichiers locaux", POSTGRES: "base PostGIS"}


@dataclass(frozen=True)
class DataStore:
    mode: str
    description: str = field(repr=False)   # user@host for logs and tools only: never shown nor in repr.
    territories: object        # TerritoryRegistry | PostgisTerritoryRegistry (same read methods)
    acquisitions: object       # FileAcquisitionCatalog | PostgisAcquisitionCatalog
    indicators: object         # IndicatorResultStore | PostgisIndicatorStore
    environment: str | None = None   # Declared environment (dev or test) in PostGIS mode.

    @property
    def is_postgis(self) -> bool:
        return self.mode == POSTGRES

    @property
    def writable(self) -> bool:
        """Only the file store accepts the application's own writes (imports, referentials, results)."""
        return self.mode == FILE

    @property
    def label(self) -> str:
        return MODE_LABELS[self.mode]


def file_store(data_dir) -> DataStore:
    from src.data.observations import FileAcquisitionCatalog, IndicatorResultStore
    from src.data.territory_registry import TerritoryRegistry

    data_dir = Path(data_dir)
    return DataStore(FILE, "répertoire de données local", TerritoryRegistry(data_dir),
                     FileAcquisitionCatalog(data_dir), IndicatorResultStore(data_dir))


def postgis_store(data_dir, environ: Mapping[str, str] | None = None, *, connect=None) -> DataStore:
    """PostGIS store for the target configured in ``environ``; validates the configuration, does not connect."""
    from src.data.database import DatabaseConfigError, load_target
    from src.data.postgis_observations import PostgisAcquisitionCatalog, PostgisIndicatorStore
    from src.data.postgis_session import PostgisSessions
    from src.data.postgis_territory_registry import PostgisTerritoryRegistry

    target = load_target(environ)
    if connect is None:
        try:
            import psycopg  # noqa: F401  (checked here for a clear message; used at the first read)
        except ImportError:
            raise DatabaseConfigError("Mode PostGIS : psycopg 3 est requis (pip install -r requirements-db.txt).") \
                from None
    sessions = PostgisSessions(target, connect=connect)
    return DataStore(POSTGRES, target.describe(), PostgisTerritoryRegistry(data_dir=data_dir, sessions=sessions),
                     PostgisAcquisitionCatalog(sessions=sessions), PostgisIndicatorStore(sessions=sessions),
                     target.environment)


def open_store(data_dir, environ: Mapping[str, str] | None = None) -> DataStore:
    """Store selected by ``GEONAF_DB_BACKEND`` (``file`` by default)."""
    if selected_backend(environ) == FILE:
        return file_store(data_dir)
    return postgis_store(data_dir, environ)
