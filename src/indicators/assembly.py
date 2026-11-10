"""Builds the indicator set of a zone from measurements, provenance and reliability rules.

A missing source gives ``value=None`` (status ``indisponible``), never an estimate.
"""

from __future__ import annotations

from pathlib import Path

from src.indicators.inventory import IndicatorInputs
from src.indicators.landcover import landcover_areas
from src.indicators.model import (
    AOI_NATURE, AVAILABLE, ESTIMATION, GEOMETRIC_MEASURE, HIGH, MAPPED_OBSERVATION, NOT_ASSESSED, UNAVAILABLE,
    BreakdownItem, IndicatorSource, TerritorialIndicator, TerritorialIndicators,
)
from src.indicators.osm import building_figures, road_lengths
from src.indicators.profiles import AFRICA_DEFAULT_V1, IndicatorProfile
from src.indicators.provenance import LANDCOVER_LIMITS, OSM_LIMITS, indicator_source, landcover_dates, osm_dates
from src.indicators.quality import landcover_reliability, osm_reliability
from src.indicators.zone import zone_area_km2, zone_sha256
from src.services.multisource_demo import CLASSIFICATION, COLLABORATIVE

# Limit of the zone itself. Valid for GEONAF acquisition squares, the only zones offered today; another
# zone nature (registry territory, user import) will need its own wording (V1.1).
ACQUISITION_ZONE_LIMITS = ("Zone d'étude (carré d'acquisition), pas une limite administrative.",)


def _unavailable(indicator_id, label, unit, aoi_id, aoi_sha, profile, reason, result_kind):
    return TerritorialIndicator(indicator_id, label, None, unit, UNAVAILABLE, aoi_id, aoi_sha, None, None, None,
                                reason, NOT_ASSESSED, (reason,), (), profile.fingerprint, result_kind=result_kind)


def _landcover_indicators(tiles, aoi_path, aoi_id, aoi_sha, profile, warnings):
    """Land, vegetation, built-up and water indicators from one WorldCover product."""
    from src.data.worldcover import CLASSES

    descriptor = tiles[0][1]
    areas, classified_m2, aoi_m2 = landcover_areas(tiles, aoi_path)
    coverage = 100.0 * classified_m2 / aoi_m2
    labels = dict(descriptor.classes or CLASSES)
    reliability, reasons, warning = landcover_reliability(descriptor, coverage, profile)
    if warning:
        warnings.append(warning)
    common = dict(aoi_id=aoi_id, aoi_sha256=aoi_sha, dates=landcover_dates(descriptor),
                  source=indicator_source(descriptor, CLASSIFICATION), coverage_percent=coverage,
                  reliability=reliability, reliability_reasons=reasons, limits=LANDCOVER_LIMITS,
                  profile_fingerprint=profile.fingerprint, result_kind=ESTIMATION)
    km2 = lambda codes: sum(areas.get(code, 0.0) for code in codes) / 1e6
    detail = lambda codes: tuple((labels.get(code, str(code)), areas[code] / 1e6) for code in codes if code in areas)
    water = km2(profile.water_classes)
    land = classified_m2 / 1e6 - water
    aoi_km2 = aoi_m2 / 1e6
    shares = lambda value: (("zone d'étude", 100.0 * value / aoi_km2),) + (
        (("surface terrestre", 100.0 * value / land),) if land > 0 else ())
    weighting = ("Somme des cellules classées, pondérées par leur intersection exacte avec la zone d'étude "
                 "(aire équivalente EPSG:6933), sur la grille native du produit ; aucun masque nuageux Sentinel-2.")
    vegetation = km2(profile.vegetation_classes)
    groups = tuple(BreakdownItem(label, label, km2(codes), "km²", 100.0 * km2(codes) / aoi_km2, detail(codes))
                   for label, codes in profile.vegetation_groups)
    builtup = km2(profile.builtup_classes)
    return [
        TerritorialIndicator(
            "landcover.land_km2", "Surface terrestre", land, "km²", AVAILABLE, method=(
                f"Surface classée hors eaux permanentes (classes {', '.join(map(str, profile.water_classes))}). "
                + weighting), shares=(("zone d'étude", 100.0 * land / aoi_km2),),
            breakdown=(BreakdownItem("water", "Eaux permanentes", water, "km²", 100.0 * water / aoi_km2,
                                     detail(profile.water_classes)),), **common),
        TerritorialIndicator(
            "landcover.vegetation_km2", "Surface végétalisée", vegetation, "km²", AVAILABLE, method=(
                "Classes " + " ; ".join(f"{label} : {', '.join(map(str, codes))}"
                                        for label, codes in profile.vegetation_groups) + ". " + weighting),
            shares=shares(vegetation), breakdown=groups, **common),
        TerritorialIndicator(
            "landcover.builtup_km2", "Surface bâtie (classe « Bâti »)", builtup, "km²", AVAILABLE, method=(
                f"Classe(s) {', '.join(map(str, profile.builtup_classes))} (bâtiments, routes et surfaces "
                "artificialisées selon la définition du producteur). " + weighting),
            shares=shares(builtup), breakdown=(BreakdownItem("builtup", "Bâti", builtup, "km²",
                                                             100.0 * builtup / aoi_km2,
                                                             detail(profile.builtup_classes)),), **common),
    ], land


def _osm_indicators(frame, descriptor, aoi_path, aoi_id, aoi_sha, aoi_km2, land_km2, land_indicator, profile):
    total_km, categories, excluded_km, area_highways = road_lengths(frame, aoi_path, profile)
    count, footprint, straddling = building_figures(frame, aoi_path)
    reliability, reasons = osm_reliability()
    common = dict(aoi_id=aoi_id, aoi_sha256=aoi_sha, dates=osm_dates(descriptor),
                  source=indicator_source(descriptor, COLLABORATIVE), coverage_percent=None, reliability=reliability,
                  reliability_reasons=reasons, profile_fingerprint=profile.fingerprint,
                  result_kind=MAPPED_OBSERVATION)
    road_limits = OSM_LIMITS + (
        "Longueur de tronçons cartographiés, pas longueur de réseau : une voie à chaussées séparées "
        "cartographiées par sens compte deux fois.",
        "Aucune information sur l'état, le revêtement ou la praticabilité des voies.",
    )
    if excluded_km:
        road_limits += (f"{excluded_km:.1f} km exclus (voies projetées, en construction ou abandonnées).",)
    if area_highways:
        road_limits += (f"{area_highways} objet(s) « highway » surfacique(s) non comptés en longueur.",)
    indicators = [
        TerritorialIndicator(
            "osm.roads_km", "Voies cartographiées (OSM)", total_km, "km", AVAILABLE, method=(
                "Somme des longueurs des chemins OSM « highway » découpés à la zone d'étude, en projection UTM "
                "locale ; regroupement des valeurs « highway » selon le profil d'indicateurs."),
            limits=road_limits, breakdown=tuple(
                BreakdownItem(label, label, km, "km", 100.0 * km / total_km if total_km else None, detail)
                for label, km, detail in categories), **common),
        TerritorialIndicator(
            "osm.buildings_count", "Bâtiments cartographiés (OSM)", float(count), "bâtiments", AVAILABLE, method=(
                "Nombre de bâtiments OSM (chemins et multipolygones valides) dont le point représentatif est dans "
                "la zone d'étude."), limits=OSM_LIMITS + (
                "Ce n'est pas le nombre total réel de bâtiments.",
                f"{straddling} bâtiment(s) à cheval sur la limite, attribué(s) selon leur point représentatif."),
            **common),
        TerritorialIndicator(
            "osm.buildings_footprint_km2", "Emprise au sol des bâtiments cartographiés (OSM)", footprint, "km²",
            AVAILABLE, method="Surface des polygones de bâtiments OSM découpés à la zone d'étude (UTM locale).",
            shares=(("zone d'étude", 100.0 * footprint / aoi_km2),), limits=OSM_LIMITS + (
                "Emprise des toits tracés, différente de la classe « Bâti » de WorldCover (routes, sols scellés).",),
            **common),
        TerritorialIndicator(
            "osm.buildings_density_aoi", "Densité de bâtiments cartographiés (zone d'étude)", count / aoi_km2,
            "bâtiments/km²", AVAILABLE, method="Nombre de bâtiments OSM / surface totale de la zone d'étude.",
            limits=OSM_LIMITS + ("Dénominateur incluant les surfaces en eau.",), **common),
    ]
    if land_km2 is None:
        indicators.append(_unavailable(
            "osm.buildings_density_land", "Densité de bâtiments cartographiés (surface terrestre)",
            "bâtiments/km²", aoi_id, aoi_sha, profile,
            "Surface terrestre indisponible (ESA WorldCover absent localement).", ESTIMATION))
    else:
        indicators.append(TerritorialIndicator(
            "osm.buildings_density_land", "Densité de bâtiments cartographiés (surface terrestre)",
            count / land_km2 if land_km2 > 0 else None, "bâtiments/km²", AVAILABLE, method=(
                "Nombre de bâtiments OSM / surface terrestre WorldCover (zone d'étude hors eaux permanentes)."),
            limits=OSM_LIMITS + (
                f"Combine deux référentiels de dates différentes : OSM {descriptor.temporal.end_utc:%Y} et "
                f"surface terrestre {land_indicator.dates.observation_start_utc:%Y}.",),
            **{**common, "result_kind": ESTIMATION}))
    return indicators


def build_indicators(*, aoi_id: str, aoi_path, worldcover=None, osm=None, aoi_label: str | None = None,
                     profile: IndicatorProfile = AFRICA_DEFAULT_V1, temporal_notes=()) -> TerritorialIndicators:
    """``worldcover``: [(path, descriptor)] of one product version; ``osm``: (GeoDataFrame, descriptor)."""
    aoi_path = Path(aoi_path)
    aoi_sha = zone_sha256(aoi_path)
    aoi_km2 = zone_area_km2(aoi_path)
    indicators = [TerritorialIndicator(
        "aoi.area_km2", "Surface de la zone d'étude", aoi_km2, "km²", AVAILABLE, aoi_id, aoi_sha,
        None, IndicatorSource("geonaf-aoi", "Zone d'étude GEONAF", aoi_sha[:12], AOI_NATURE, "GEONAF", aoi_sha),
        100.0, "Aire géodésique de la géométrie de la zone d'étude (aire équivalente EPSG:6933).", HIGH,
        ("Géométrie exacte du fichier de zone d'étude.",), ACQUISITION_ZONE_LIMITS, profile.fingerprint,
        result_kind=GEOMETRIC_MEASURE)]
    missing, warnings = [], []
    land_km2 = land_indicator = None
    tiles = list(worldcover or [])
    if tiles:
        if any(d.source_id != profile.landcover_source for _, d in tiles):
            raise ValueError("Source d'occupation du sol incompatible avec le profil d'indicateurs.")
        if len({(d.source_id, d.version) for _, d in tiles}) != 1:
            raise ValueError("Tuiles d'occupation du sol de versions différentes : calcul refusé.")
        landcover, land_km2 = _landcover_indicators(tiles, aoi_path, aoi_id, aoi_sha, profile, warnings)
        land_indicator = landcover[0]
        indicators += landcover
    else:
        missing.append("ESA WorldCover")
        for indicator_id, label in (("landcover.land_km2", "Surface terrestre"),
                                    ("landcover.vegetation_km2", "Surface végétalisée"),
                                    ("landcover.builtup_km2", "Surface bâtie (classe « Bâti »)")):
            indicators.append(_unavailable(indicator_id, label, "km²", aoi_id, aoi_sha, profile,
                                           "ESA WorldCover absent localement pour cette zone d'étude.", ESTIMATION))
    if osm is not None:
        frame, descriptor = osm
        indicators += _osm_indicators(frame, descriptor, aoi_path, aoi_id, aoi_sha, aoi_km2, land_km2,
                                      land_indicator, profile)
    else:
        missing.append("OpenStreetMap")
        for indicator_id, label, unit, kind in (
                ("osm.roads_km", "Voies cartographiées (OSM)", "km", MAPPED_OBSERVATION),
                ("osm.buildings_count", "Bâtiments cartographiés (OSM)", "bâtiments", MAPPED_OBSERVATION),
                ("osm.buildings_footprint_km2", "Emprise au sol des bâtiments cartographiés (OSM)", "km²",
                 MAPPED_OBSERVATION),
                ("osm.buildings_density_aoi", "Densité de bâtiments cartographiés (zone d'étude)", "bâtiments/km²",
                 MAPPED_OBSERVATION),
                ("osm.buildings_density_land", "Densité de bâtiments cartographiés (surface terrestre)",
                 "bâtiments/km²", ESTIMATION)):
            indicators.append(_unavailable(indicator_id, label, unit, aoi_id, aoi_sha, profile,
                                           "Instantané OpenStreetMap absent localement pour cette zone d'étude.",
                                           kind))
    return TerritorialIndicators(aoi_id, aoi_label or aoi_id, aoi_sha, aoi_km2, profile, tuple(indicators),
                                 tuple(temporal_notes), tuple(missing), tuple(warnings))


def compute_territorial_indicators(inputs: IndicatorInputs, *, aoi_label: str | None = None,
                                   profile: IndicatorProfile = AFRICA_DEFAULT_V1) -> TerritorialIndicators:
    """Indicators of an inventoried zone; stored layers only, no network."""
    from src.data.openstreetmap import load_osm_snapshot
    from src.services.external_layers import open_store

    osm = None
    if inputs.osm is not None:
        store, _ = open_store(inputs.data_dir)
        osm = load_osm_snapshot(store, inputs.osm.storage_key, inputs.bounds)
    return build_indicators(aoi_id=inputs.aoi_id, aoi_path=inputs.aoi_path,
                            worldcover=[(layer.path, layer.descriptor) for layer in inputs.worldcover] or None,
                            osm=osm, aoi_label=aoi_label, profile=profile, temporal_notes=inputs.temporal_notes)


def build_s2_vegetation_indicator(estimate, inventory) -> TerritorialIndicator:
    """Indicator of one Sentinel-2 vegetation estimate; separate from :func:`build_indicators`.

    ``value`` is the observed area with NDVI ≥ threshold, ``None`` when the
    estimate may not be published (measurements stay in ``estimate.measure``).
    The only share is relative to the usable observed area, never to the zone.
    """
    import hashlib
    import json

    from src.indicators.provenance import (
        SENTINEL2_ATTRIBUTION, SENTINEL2_NATURE, SENTINEL2_SOURCE_ID, VEGETATION_S2_LIMITS, sentinel2_dates,
    )
    from src.indicators.presentation import fr
    from src.indicators.s2_inventory import BREAKDOWN_UNAVAILABLE

    profile, measure, assessment = inventory.profile, estimate.measure, estimate.assessment
    km2 = lambda m2: m2 / 1e6
    zone_share = lambda m2: 100.0 * m2 / measure.zone_m2
    parts = [("observed", "Surface observée exploitable", measure.observed_m2),
             ("vegetated", f"Surface à NDVI ≥ {fr(profile.threshold)}", measure.vegetated_m2),
             ("not_vegetated", f"Surface observée à NDVI < {fr(profile.threshold)}", measure.not_vegetated_m2),
             ("excluded_observed", "Observée mais exclue par la politique SCL (eau, zones sombres, non classées…)",
              measure.excluded_observed_m2),
             ("not_observed", "Non observée selon le SCL (nuages, ombres, cirrus, pixels défectueux)",
              measure.not_observed_m2),
             ("other_invalid", "Autres pixels invalides (réflectance ou NDVI)", measure.other_invalid_m2),
             ("unsplit", "Non exploitable, motif non ventilé", measure.unsplit_m2),
             ("no_data", "Sans donnée (SCL absent ou hors de la grille analysée)", measure.no_data_m2)]
    sensitivity = tuple((f"NDVI ≥ {fr(t)}", km2(m2)) for t, m2 in measure.sensitivity_m2)
    breakdown = tuple(BreakdownItem(key, label, km2(m2), "km²", zone_share(m2),
                                    sensitivity if key == "vegetated" else ())
                      for key, label, m2 in parts if m2 is not None)
    limits = VEGETATION_S2_LIMITS + estimate.mosaic_notes
    if estimate.breakdown_status == BREAKDOWN_UNAVAILABLE:
        limits += ("Ventilation non disponible : mosaïque de plusieurs tuiles, classes SCL non fusionnées ; "
                   "seules les surfaces observée et à NDVI ≥ seuil sont publiées.",)
    available = assessment.available
    fingerprint = hashlib.sha256(json.dumps(
        [estimate.fingerprint, profile.fingerprint, inventory.scientific_profile_fingerprint]).encode()).hexdigest()
    return TerritorialIndicator(
        "s2.vegetation_km2", f"Surface à NDVI ≥ {fr(profile.threshold)} (Sentinel-2, surface observée)",
        km2(measure.vegetated_m2) if available else None, "km²", AVAILABLE if available else UNAVAILABLE,
        inventory.aoi_id, inventory.aoi_sha256,
        sentinel2_dates(estimate.acquired_at_utc, estimate.product_ids, estimate.ingested_at_utc),
        IndicatorSource(SENTINEL2_SOURCE_ID, "Copernicus Sentinel-2 L2A", " + ".join(estimate.product_ids),
                        SENTINEL2_NATURE, SENTINEL2_ATTRIBUTION.format(year=estimate.acquired_at_utc.year),
                        fingerprint),
        measure.observed_coverage_percent,
        (f"Somme des surfaces exactes (aire équivalente EPSG:6933, pixels partiels compris) des pixels valides "
         f"selon la politique SCL « {inventory.scl_policy} » du profil {inventory.scientific_profile_id} "
         f"{inventory.scientific_profile_version}, dont le NDVI calibré est ≥ {fr(profile.threshold)} (comparaison "
         f"en float32). Part rapportée à la seule surface observée exploitable ; aucune extrapolation. Profil "
         f"{profile.profile_id} {profile.version}."),
        assessment.reliability, assessment.reasons, limits, profile.fingerprint,
        shares=((("surface observée exploitable", measure.vegetated_share_percent),) if available else ()),
        breakdown=breakdown, result_kind=ESTIMATION, scientific_status=assessment.scientific_status)


def build_s2_transition_indicators(pair, selection) -> tuple[TerritorialIndicator, ...]:
    """Indicators of one Sentinel-2 acquisition pair; separate from :func:`build_indicators` and from L3.

    Three records sharing dates, source, coverage, reliability and limits: the
    common comparable surface, the area-weighted ΔNDVI mean and the class
    transitions around the NDVI class threshold. Values are ``None`` when the
    pair may not be published (measurements stay in ``pair.measure``).
    """
    import hashlib
    import json

    from src.indicators.presentation import fr
    from src.indicators.provenance import (
        SENTINEL2_ATTRIBUTION, SENTINEL2_PAIR_NATURE, SENTINEL2_SOURCE_ID, TRANSITION_S2_LIMITS,
        sentinel2_pair_dates,
    )
    from src.indicators.s2_pairs import PUBLISHABLE

    profile, measure, assessment = selection.profile, pair.measure, pair.assessment
    available = pair.status == PUBLISHABLE and measure is not None
    t = fr(profile.threshold)
    years = sorted({pair.earlier.acquired_at_utc.year, pair.later.acquired_at_utc.year})
    fingerprint = hashlib.sha256(json.dumps(
        [pair.earlier.fingerprint, pair.later.fingerprint, profile.fingerprint,
         selection.scientific_profile_fingerprint]).encode()).hexdigest()
    limits = TRANSITION_S2_LIMITS + (pair.seasonal_notes or (
        "Comparabilité saisonnière non évaluée par la chaîne de comparaison.",))
    reasons = pair.reasons + (() if assessment is None else assessment.warnings)
    common = dict(
        status=AVAILABLE if available else UNAVAILABLE, aoi_id=selection.aoi_id, aoi_sha256=selection.aoi_sha256,
        dates=sentinel2_pair_dates(pair.earlier, pair.later),
        source=IndicatorSource(SENTINEL2_SOURCE_ID, "Copernicus Sentinel-2 L2A",
                               " + ".join(pair.earlier.product_ids) + " → " + " + ".join(pair.later.product_ids),
                               SENTINEL2_PAIR_NATURE,
                               SENTINEL2_ATTRIBUTION.format(year="–".join(map(str, years))), fingerprint),
        coverage_percent=None if measure is None else measure.common_coverage_percent,
        reliability=assessment.reliability if available else NOT_ASSESSED, reliability_reasons=reasons,
        limits=limits, profile_fingerprint=profile.fingerprint, result_kind=ESTIMATION,
        scientific_status=profile.scientific_status if assessment is None else assessment.scientific_status)
    method = (f"Pixels valides aux deux dates selon la politique SCL « {selection.scl_policy} » du profil "
              f"{selection.scientific_profile_id} {selection.scientific_profile_version}, sur le masque commun de "
              f"la chaîne de comparaison historique (grilles alignées, aucun rééchantillonnage) ; surfaces exactes "
              f"en aire équivalente EPSG:6933, pixels partiels compris. Profil {profile.profile_id} "
              f"{profile.version} (seuil de classe NDVI {t}, couverture commune minimale "
              f"{fr(profile.min_common_coverage_percent)} %).")
    km2 = lambda m2: m2 / 1e6
    value = lambda v: v if available else None
    indicators = [TerritorialIndicator(
        "s2.pair.common_km2", "Surface observée commune aux deux dates (Sentinel-2)",
        value(None if measure is None else km2(measure.common_m2)), "km²", method=method,
        shares=((("zone d'étude", measure.common_coverage_percent),) if available else ()), **common)]
    breakdown = ()
    if available:
        breakdown = tuple(BreakdownItem(key, label, v, "NDVI") for key, label, v in (
            ("mean_1", f"NDVI moyen pondéré au {pair.earlier.acquired_at_utc:%d/%m/%Y}", measure.mean_1),
            ("mean_2", f"NDVI moyen pondéré au {pair.later.acquired_at_utc:%d/%m/%Y}", measure.mean_2),
            ("median_1", f"NDVI médian pondéré au {pair.earlier.acquired_at_utc:%d/%m/%Y}", measure.median_1),
            ("median_2", f"NDVI médian pondéré au {pair.later.acquired_at_utc:%d/%m/%Y}", measure.median_2),
            ("delta_median", "ΔNDVI médian pondéré", measure.delta_median)))
    indicators.append(TerritorialIndicator(
        "s2.pair.delta_ndvi_mean", "ΔNDVI moyen pondéré sur la surface commune (Sentinel-2)",
        value(None if measure is None else measure.delta_mean), "NDVI", method=method + (
            " Moyenne des NDVI calibrés pondérée par la surface de chaque pixel ; date 2 − date 1."),
        breakdown=breakdown, **common))
    breakdown = ()
    if available:
        tr, share = measure.transitions, lambda m2: 100.0 * m2 / measure.common_m2
        detail = lambda name: tuple((f"Seuil {fr(s.threshold)}", km2(getattr(s, name))) for s in measure.sensitivity)
        breakdown = tuple(BreakdownItem(key, label, km2(getattr(tr, key)), "km²", share(getattr(tr, key)),
                                        detail(key)) for key, label in (
            ("down_m2", f"NDVI ≥ {t} puis < {t}"), ("up_m2", f"NDVI < {t} puis ≥ {t}"),
            ("stayed_above_m2", f"NDVI ≥ {t} aux deux dates"), ("stayed_below_m2", f"NDVI < {t} aux deux dates")))
        breakdown += (BreakdownItem("net_m2", "Solde des transitions (vers le haut − vers le bas), pas un gain ni "
                                    "une perte de végétation", km2(tr.net_m2), "km²"),)
    indicators.append(TerritorialIndicator(
        "s2.pair.class_transitions_km2", f"Surface ayant changé de classe NDVI {t} (Sentinel-2, surface commune)",
        value(None if measure is None else km2(measure.transitions.down_m2 + measure.transitions.up_m2)), "km²",
        method=method + f" Comparaison au seuil en float32, égalité comptée comme ≥ {t}.",
        shares=((("surface commune", 100.0 * (measure.transitions.down_m2 + measure.transitions.up_m2)
                  / measure.common_m2),) if available else ()), breakdown=breakdown, **common))
    return tuple(indicators)
