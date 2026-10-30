"""Territories offered for selection, map payloads and comparison layers; no UI, no implicit network.

Three families stay distinct:

* GEONAF acquisition zones (historical 12 km squares), read from
  ``data/aoi`` and never written to the registry here;
* administrative units of a referential (gbOpen, gbHumanitarian), present
  only after an explicit ``load_referential`` (network, cached snapshot);
* custom zones imported by the user (``import_custom_zone``), always
  « fourni par l'utilisateur », never official.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path

from src.data.territory_registry import (
    ACQUISITION_ZONE, ADMIN_UNIT, COMMUNITY, CUSTOM_IMPORT, GEONAF_ZONE, HISTORICAL_ZONES,
    HUMANITARIAN, USER_PROVIDED, TerritoryRegistry, TerritorySource,
)

REFERENTIALS = {"gbOpen": "geoBoundaries gbOpen (communautaire)",
                "gbHumanitarian": "geoBoundaries gbHumanitarian (humanitaire, OCHA COD-AB)"}
LEVELS = {"ADM0": "Pays (ADM0)", "ADM1": "Provinces (ADM1)", "ADM2": "Départements (ADM2)"}
STATUS_TEXT = {
    GEONAF_ZONE: "Zone d'acquisition GEONAF : carré de travail, pas une limite administrative.",
    COMMUNITY: "Référentiel communautaire : limites non officielles et non vérifiées.",
    HUMANITARIAN: "Référentiel humanitaire (COD) : limites non officielles et non vérifiées.",
    USER_PROVIDED: "Zone fournie par l'utilisateur : statut administratif non vérifié.",
}
COLORS = {"a": "#f59e0b", "b": "#38bdf8", "both": "#a3a3a3", "zone": "#f8fafc"}


class ZoneImportRefused(ValueError):
    """Import refused by a size, field or validity check; the message is written for users."""


@dataclass(frozen=True)
class TerritoryOption:
    key: str                 # Unique selection key: "aoi:<id>" or "territory:<id>".
    territory_id: str
    label: str
    kind: str
    legal_status: str
    geometry_path: Path
    area_km2: float
    record: object | None    # TerritoryRecord for registered territories; None for historical zones.

    @property
    def status_text(self) -> str:
        return STATUS_TEXT.get(self.legal_status, self.legal_status)


def historical_zones(data_dir) -> tuple[TerritoryOption, ...]:
    """The historical acquisition squares present in ``data/aoi`` (read only)."""
    from src.geo.change_analysis import load_equal_area_aoi

    output = []
    for aoi_id, name in HISTORICAL_ZONES.items():
        path = Path(data_dir) / "aoi" / f"{aoi_id}.geojson"
        if path.is_file():
            output.append(TerritoryOption(f"aoi:{aoi_id}", aoi_id, f"Zone d'acquisition {name} (12 × 12 km)",
                                          ACQUISITION_ZONE, GEONAF_ZONE, path,
                                          load_equal_area_aoi(path).area_m2 / 1e6, None))
    return tuple(output)


def _option(registry, record) -> TerritoryOption:
    return TerritoryOption(f"territory:{record.territory_id}", record.territory_id, record.label, record.kind,
                           record.legal_status, registry.store.path(record.storage_key), record.area_km2, record)


def admin_territories(data_dir, release: str, level: str, iso3: str = "GAB") -> tuple[TerritoryOption, ...]:
    registry = TerritoryRegistry(Path(data_dir))
    prefix = f"{iso3.lower()}-{release.lower()}-{level.lower()}-"
    records = [r for r in registry.territories(ADMIN_UNIT) if r.territory_id.startswith(prefix)
               and r.source.source_id == f"geoboundaries-{release.lower()}" and r.admin.level_code == level]
    return tuple(_option(registry, r) for r in sorted(records, key=lambda r: r.label.lower()))


def custom_zones(data_dir) -> tuple[TerritoryOption, ...]:
    registry = TerritoryRegistry(Path(data_dir))
    return tuple(_option(registry, r) for r in sorted(registry.territories(CUSTOM_IMPORT), key=lambda r: r.label))


def load_referential(data_dir, release: str, level: str, iso3: str = "GAB", client=None):
    """Explicit user action: retrieve (or reuse the cached snapshot of) one referential level."""
    from src.data.geoboundaries import import_geoboundaries

    return import_geoboundaries(Path(data_dir), release=release, iso3=iso3, level=level, client=client)


def geometry_wgs84(option: TerritoryOption):
    import geopandas as gpd

    if option.record is not None:
        TerritoryRegistry(option.geometry_path.parents[2]).geometry_path(option.record)   # Digest check.
    return gpd.read_file(option.geometry_path).to_crs("EPSG:4326").geometry.union_all()


def _round(geometry):
    import numpy as np
    import shapely

    return shapely.transform(geometry, lambda xy: np.round(xy, 6)).__geo_interface__


def map_payload(layers, *, note: str, attributions=()) -> dict:
    """``layers``: [(id, label, geometry WGS84, color, style)] with style "outline", "fill" or "dashed"."""
    from shapely.ops import unary_union

    drawn = [(i, label, g, color, style) for i, label, g, color, style in layers if g is not None and not g.is_empty]
    if not drawn:
        raise ValueError("Aucune géométrie à afficher.")
    left, bottom, right, top = unary_union([g for _, _, g, _, _ in drawn]).bounds
    return {"schema_version": "geonaf.territory-map/v1", "bounds": [[left, bottom], [right, top]],
            "layers": [{"id": i, "label": label, "color": color, "style": style,
                        "geojson": {"type": "Feature", "properties": {}, "geometry": _round(g)}}
                       for i, label, g, color, style in drawn],
            "note": note, "attributions": list(attributions)}


def selection_payload(option: TerritoryOption) -> dict:
    attribution = () if option.record is None else (option.record.source.attribution,)
    return map_payload([("zone", option.label, geometry_wgs84(option), COLORS["zone"], "outline")],
                       note="Contour exact enregistré (coordonnées arrondies à 1e-6° pour l'affichage).",
                       attributions=attribution)


def comparison_payload(a: TerritoryOption, b: TerritoryOption) -> dict:
    """Two outlines kept separate, plus their common part and each side's own part (display only)."""
    geometry_a, geometry_b = geometry_wgs84(a), geometry_wgs84(b)
    source = lambda o: o.record.source.product if o.record is not None else "GEONAF"
    return map_payload([
        ("both", "Partie commune", geometry_a.intersection(geometry_b), COLORS["both"], "fill"),
        ("a-only", f"Seulement {source(a)}", geometry_a.difference(geometry_b), COLORS["a"], "fill"),
        ("b-only", f"Seulement {source(b)}", geometry_b.difference(geometry_a), COLORS["b"], "fill"),
        ("a", f"Contour {source(a)}", geometry_a, COLORS["a"], "outline"),
        ("b", f"Contour {source(b)}", geometry_b, COLORS["b"], "dashed"),
    ], note="Deux représentations d'un même territoire, non fusionnées ; l'écart n'est pas un changement réel.",
        attributions=[o.record.source.attribution for o in (a, b) if o.record is not None])


def referential_pairs(data_dir, level: str, iso3: str = "GAB"):
    """(pairs, unmatched gbOpen, unmatched gbHumanitarian) by normalized name; nothing is merged or hidden."""
    from src.services.territory_comparison import match_by_normalized_name

    open_ = admin_territories(data_dir, "gbOpen", level, iso3)
    humanitarian = admin_territories(data_dir, "gbHumanitarian", level, iso3)
    pairs = match_by_normalized_name(open_, humanitarian)
    matched_a = {a.key for a, _ in pairs}
    matched_b = {b.key for _, b in pairs}
    return (pairs, tuple(o for o in open_ if o.key not in matched_a),
            tuple(o for o in humanitarian if o.key not in matched_b))


def import_custom_zone(data_dir, content: bytes, *, label: str, author: str, license_spdx: str, source_date: str,
                       limits=None, now: datetime | None = None):
    """Register one user GeoJSON (one zone per operation) after size, validity and complexity checks.

    Returns (record, assessment). The size is checked before parsing; the
    status is always « fourni par l'utilisateur ». Nothing is stored when a
    check fails.
    """
    import tempfile

    from src.data.geoboundaries import slug
    from src.services.resource_guards import ResourceLimits, assess_import

    limits = limits or ResourceLimits.from_environment()
    for value, name in ((label, "Nom de la zone"), (author, "Auteur ou organisme source"),
                        (license_spdx, "Licence ou droits de réutilisation"), (source_date, "Date de la source")):
        if not isinstance(value, str) or not value.strip():
            raise ZoneImportRefused(f"{name} obligatoire.")
    if len(content) > limits.max_import_bytes:
        raise ZoneImportRefused(f"Fichier de {len(content) / 2**20:.1f} Mio > limite "
                                f"{limits.max_import_bytes / 2**20:.0f} Mio.")
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "import.geojson"
        path.write_bytes(content)
        assessment = assess_import(path, limits=limits)
        if not assessment.allowed:
            raise ZoneImportRefused(" ".join(check.reason for check in assessment.refusals))
        from src.geo.aoi_identity import load_aoi_identity

        identity = load_aoi_identity(path)
        territory_id = f"import-{(slug(label)[:40] or 'zone')}-{identity.geometry_sha256[:8]}"
        record = TerritoryRegistry(Path(data_dir)).register(
            territory_id=territory_id, label=" ".join(label.split()), kind=CUSTOM_IMPORT,
            legal_status=USER_PROVIDED,
            source=TerritorySource("user-import", "Import GeoJSON utilisateur", source_date.strip(),
                                   license_spdx.strip(), author.strip(),
                                   retrieved_at_utc=(now or datetime.now(timezone.utc)).isoformat()),
            geometry_path=path,
            limits=("Zone fournie par l'utilisateur ; statut administratif non vérifié.",
                    f"Fichier importé : sha256 {hashlib.sha256(content).hexdigest()[:16]}…"), now=now)
    return record, assessment
