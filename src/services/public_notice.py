"""Texts that accompany every result shown or exported: zone status, data dates, source, licence, limits.

Pure functions (no Streamlit), shared by the pages (src/components/public_notice.py) and the CSV export
(src/services/ndvi_time_series.py), so that the wording is the same on screen and in a downloaded file.

The Sentinel-2 attribution reuses src.indicators.provenance.SENTINEL2_ATTRIBUTION; like that constant, the
licence sentence below has not been re-checked against the Copernicus legal notice (PDF): to verify before
the public release.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from src.indicators.provenance import SENTINEL2_ATTRIBUTION

SENTINEL_LICENCE = ("Données Copernicus Sentinel : accès libre, complet et gratuit (règlement délégué (UE) "
                    "n° 1159/2013) ; voir l'avis juridique Copernicus.")
INTERPRETATION = ("Résultats exploratoires, non validés : une variation de NDVI n'est pas un changement territorial "
                  "démontré (nuages résiduels, marée, humidité, saison et versions de traitement non corrigés).")


def zone_statement(label: str) -> str:
    """Status of a pilot square: an acquisition zone, never an administrative unit or a municipal statistic."""
    return (f"{label} : carré d'acquisition GEONAF de 12 × 12 km, pas une limite administrative ; aucune valeur "
            "n'est attribuable à la collectivité.")


def sentinel2_attribution(years: Iterable[int]) -> str:
    """Attribution with every acquisition year used (sorted, unique)."""
    values = sorted({int(year) for year in years})
    if not values:
        return SENTINEL2_ATTRIBUTION.format(year="").rstrip()
    return SENTINEL2_ATTRIBUTION.format(year=", ".join(str(year) for year in values))


def acquisition_dates_text(dates: Iterable[datetime] | None) -> str:
    """Acquisition dates (UTC) available for a zone; the most recent one is stated explicitly."""
    if dates is None:
        return "Dates des acquisitions Sentinel-2 indisponibles."
    ordered = sorted(dates)
    if not ordered:
        return "Aucune acquisition Sentinel-2 vérifiée pour cette zone."
    first, last = ordered[0], ordered[-1]
    span = f"{first:%d/%m/%Y}" if first.date() == last.date() else f"{first:%d/%m/%Y} → {last:%d/%m/%Y}"
    return (f"Acquisitions Sentinel-2 vérifiées : {len(ordered)} ({span}, dates d'acquisition UTC) ; dernière "
            f"acquisition le {last:%d/%m/%Y}, aucune donnée plus récente n'est chargée.")
