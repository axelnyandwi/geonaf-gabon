"""Display vocabulary for indicators: natures, denominators, OSM glossary, verified attributions; no Streamlit.

Nothing here feeds a calculation or a profile: renaming a label never changes
a value or a profile fingerprint.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.indicators.model import ESTIMATION, GEOMETRIC_MEASURE, MAPPED_OBSERVATION, PREDICTION

RESULT_KIND_TEXT = {  # (label, explanation)
    GEOMETRIC_MEASURE: ("Mesure géométrique", "Calcul exact sur la géométrie de la zone d'étude."),
    MAPPED_OBSERVATION: ("Observation d'une base cartographique",
                         "Objets présents dans la base à la date de son instantané : ce n'est ni un relevé de "
                         "terrain ni un total réel."),
    ESTIMATION: ("Estimation", "Valeur issue d'un modèle ou d'une classification ; exactitude non vérifiée sur la "
                               "zone d'étude."),
    PREDICTION: ("Prédiction", "Projection d'un état futur. Aucune prédiction dans cette version."),
}
DENOMINATORS = {
    "zone d'étude": "de la zone d'étude (eaux comprises)",
    "surface terrestre": "de la surface terrestre (hors eaux permanentes)",
}

# Road categories of AFRICA_DEFAULT_V1, keyed by their (unchanged) profile label.
ROAD_GLOSSARY = {  # profile label -> (displayed label, explanation)
    "Axes principaux": ("Axes principaux", "Autoroutes, voies rapides et routes principales, avec leurs bretelles."),
    "Axes secondaires": ("Axes secondaires", "Routes secondaires et leurs bretelles."),
    "Axes tertiaires": ("Axes tertiaires", "Routes tertiaires reliant localités ou quartiers, et leurs bretelles."),
    "Voies de desserte": (
        "Rues de desserte locale",
        "Rues qui desservent habitations et quartiers : rues résidentielles, zones de rencontre, voies mineures "
        "(« unclassified ») et voies dont le type n'est pas encore précisé (« road »)."),
    "Voies de service": (
        "Voies d'accès et de service",
        "Voies d'accès à un site, un bâtiment, un parking ou une installation (« service »). Leur statut d'accès "
        "(public ou restreint) n'est pas déduit : il dépend d'autres attributs OSM, non retenus ici."),
    "Pistes": (
        "Pistes",
        "Chemins à usage agricole, forestier ou de desserte rurale (« track »). Le revêtement n'est pas déduit de "
        "cette catégorie : il dépend d'attributs OSM (surface, tracktype) non retenus ici."),
    "Chemins piétons et cyclables": (
        "Chemins piétons et cyclables",
        "Trottoirs et chemins piétons, sentiers, voies piétonnes, pistes cyclables, escaliers, chemins cavaliers."),
    "Autres voies": ("Autres voies", "Valeurs « highway » non regroupées par le profil ; voir le détail."),
}
HIGHWAY_TEXT = {
    "motorway": "autoroute", "motorway_link": "bretelle d'autoroute", "trunk": "voie rapide",
    "trunk_link": "bretelle de voie rapide", "primary": "route principale", "primary_link": "bretelle de route principale",
    "secondary": "route secondaire", "secondary_link": "bretelle de route secondaire", "tertiary": "route tertiaire",
    "tertiary_link": "bretelle de route tertiaire", "residential": "rue résidentielle", "unclassified": "voie mineure",
    "living_street": "zone de rencontre", "road": "type non précisé", "service": "voie d'accès ou de service",
    "track": "piste", "footway": "chemin piéton", "path": "sentier", "pedestrian": "voie piétonne",
    "cycleway": "piste cyclable", "steps": "escalier", "bridleway": "chemin cavalier",
}


def fr(value: float, decimals: int | None = None) -> str:
    """French decimal comma for wording (``0.4`` → « 0,4 »); ``decimals`` fixes the digits."""
    text = f"{value:g}" if decimals is None else f"{value:.{decimals}f}"
    return text.replace(".", ",")


def road_category_label(label: str) -> str:
    return ROAD_GLOSSARY.get(label, (label, ""))[0]


def highway_label(value: str) -> str:
    """French reading with the raw OSM value, e.g. « voie mineure (unclassified) »; unknown values stay raw."""
    return f"{HIGHWAY_TEXT[value]} ({value})" if value in HIGHWAY_TEXT else value


def share_text(denominator: str, percent: float, format_percent) -> str:
    return f"{format_percent(percent)} % {DENOMINATORS.get(denominator, 'de la ' + denominator)}"


@dataclass(frozen=True)
class Attribution:
    text: str
    licence: str
    licence_url: str
    citation: str | None
    verified: bool        # Checked against the producer's terms (see VERIFIED_ON), else the stored descriptor text.


# Attribution terms checked on the producers' pages on 28/10/2026:
# https://esa-worldcover.org/en/data-access and https://www.openstreetmap.org/copyright.
VERIFIED_ON = "28/10/2026"
WORLDCOVER_DOI = {"v100": "https://doi.org/10.5281/zenodo.5571936", "v200": "https://doi.org/10.5281/zenodo.7254221"}


def attribution(source, year: int | None) -> Attribution | None:
    """Verified attribution of an indicator source; the stored text, marked unverified, for any other source."""
    if source is None or source.source_id == "geonaf-aoi":
        return None
    if source.source_id == "esa-worldcover" and year is not None:
        return Attribution(f"© ESA WorldCover project {year} / Contains modified Copernicus Sentinel data ({year}) "
                           "processed by ESA WorldCover consortium", "CC BY 4.0",
                           "https://creativecommons.org/licenses/by/4.0/", WORLDCOVER_DOI.get(source.version), True)
    if source.source_id == "openstreetmap":
        return Attribution("© OpenStreetMap contributors", "ODbL 1.0", "https://www.openstreetmap.org/copyright",
                           None, True)
    return Attribution(source.attribution, "voir le descripteur de la couche", "", None, False)
