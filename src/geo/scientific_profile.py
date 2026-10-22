"""Versioned scientific settings, independent of UI and raster processing.

V1 evaluates eligibility for interpretation, never validates persistent change.
An explicit season calendar describes labels, not a measured phenology model.
"""

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
import re

from src.data.acquisition import SeasonalContext
from src.data.cloud_mask import (
    SCL_CLOUD_SHADOW_CLASSES, SCL_DEFECTIVE_CLASSES, SCL_MASK_POLICIES, SCL_UNCERTAIN_CLASSES,
)


class ScientificStatus(str, Enum):
    EXPLORATION = "exploration"
    NOT_ASSESSED = "not_assessed"
    NOT_COMPARABLE = "not_comparable"
    VALIDATED = "validated"  # Reserved: V1 has no independent persistence validation.


@dataclass(frozen=True)
class InterpretationAssessment:
    status: ScientificStatus
    restrictions: tuple[str, ...]


@dataclass(frozen=True)
class SeasonalRules:
    require_same_season: bool = True
    month_seasons: tuple[str, ...] = ()
    source: str = ""
    version: str = ""
    max_interval_days: float | None = None

    def __post_init__(self):
        if not isinstance(self.require_same_season, bool):
            raise ValueError("require_same_season doit être un booléen.")
        if not isinstance(self.month_seasons, tuple) or (
            self.month_seasons and (len(self.month_seasons) != 12 or any(
                not isinstance(label, str) or not label.strip() for label in self.month_seasons))
        ):
            raise ValueError("Le calendrier doit contenir douze saisons mensuelles explicites.")
        if not isinstance(self.source, str) or not isinstance(self.version, str):
            raise ValueError("Source et version saisonnières doivent être des textes.")
        if self.month_seasons and (not self.source.strip() or not self.version.strip()):
            raise ValueError("Un calendrier nécessite une source et une version.")
        if self.max_interval_days is not None:
            _finite_number(self.max_interval_days, "Intervalle saisonnier", 0, None, strict_lower=True)


def _finite_number(value, label, lower, upper, *, strict_lower=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} doit être un nombre fini.")
    if value < lower or (strict_lower and value == lower) or (upper is not None and value > upper):
        raise ValueError(f"{label} hors des limites autorisées.")


@dataclass(frozen=True)
class ScientificProfile:
    profile_id: str
    version: str
    label: str
    aoi_id: str
    region: str
    climate: str
    ecosystem: str
    usage: str
    scl_policy: str
    change_threshold: float
    min_comparable_coverage_percent: float
    seasonal_rules: SeasonalRules = SeasonalRules()
    experimental: bool = True
    rationale: str = "Paramètres expérimentaux ; validation indépendante non réalisée."

    def __post_init__(self):
        for name in ("profile_id", "version", "label", "aoi_id", "region", "climate", "ecosystem", "usage", "rationale"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Le profil doit renseigner {name}.")
        # AOI identifiers resolve only inside the local AOI directory.
        for value in (self.profile_id, self.aoi_id, self.version):
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", value) or value in {".", ".."}:
                raise ValueError("Identifiant AOI, profil ou version invalide.")
        if not isinstance(self.scl_policy, str) or self.scl_policy not in SCL_MASK_POLICIES:
            raise ValueError("Politique SCL inconnue.")
        _finite_number(self.change_threshold, "Seuil NDVI", 0, 2, strict_lower=True)
        _finite_number(self.min_comparable_coverage_percent, "Couverture minimale", 0, 100)
        if not isinstance(self.experimental, bool) or not isinstance(self.seasonal_rules, SeasonalRules):
            raise ValueError("Nature du profil ou règles saisonnières invalides.")

    @property
    def fingerprint(self):
        """Hash all settings, including unversioned edits; not file identities."""
        payload = json.dumps(asdict(self), sort_keys=True, ensure_ascii=True, allow_nan=False)
        return hashlib.sha256(payload.encode("ascii")).hexdigest()

    def seasonal_context(self, acquired_at: datetime) -> SeasonalContext | None:
        if not self.seasonal_rules.month_seasons:
            return None
        if acquired_at.tzinfo is None or acquired_at.utcoffset() is None:
            raise ValueError("Une acquisition doit être datée avec un fuseau horaire.")
        month = acquired_at.astimezone(timezone.utc).month
        return SeasonalContext(self.region, self.ecosystem,
                               self.seasonal_rules.month_seasons[month - 1],
                               self.seasonal_rules.source, self.seasonal_rules.version)


GABON_EQUATORIAL_EXPERIMENTAL = ScientificProfile(
    profile_id="gabon-equatorial-akanda", version="1.0.0-experimental",
    label="Gabon équatorial · Akanda · expérimental",
    aoi_id="akanda", region="Gabon/Estuaire", climate="équatorial (contexte déclaré)",
    ecosystem="mosaïque forestière et littorale", usage="exploration des variations NDVI",
    scl_policy="usable_land", change_threshold=0.10, min_comparable_coverage_percent=5.0,
    rationale="Seuils opérationnels repris du prototype, non validés par écosystème. "
              "Aucun calendrier phénologique validé fourni ; humidité et marées non corrigées.",
)

# A sensitivity scenario, not a second scientifically calibrated recommendation.
GABON_SENSITIVITY_EXPERIMENTAL = replace(
    GABON_EQUATORIAL_EXPERIMENTAL, profile_id="gabon-equatorial-akanda-sensitivity",
    label="Akanda · sensibilité des paramètres · expérimental",
    usage="analyse de sensibilité exploratoire", change_threshold=0.15,
    min_comparable_coverage_percent=50.0,
    rationale="Scénario de sensibilité : 0,15 et 50 % ne sont pas des seuils validés.",
)

# Same declared context and parameters for the two other Estuaire AOIs; ecosystems
# are described, not calibrated. Akanda's reference profile is unchanged.
GABON_EQUATORIAL_LIBREVILLE = replace(
    GABON_EQUATORIAL_EXPERIMENTAL, profile_id="gabon-equatorial-libreville",
    label="Gabon équatorial · Libreville · expérimental", aoi_id="libreville",
    ecosystem="tissu urbain dense, mangroves et littoral",
    rationale="Paramètres repris du profil Akanda (même contexte déclaré), non validés pour un milieu "
              "majoritairement urbain. Humidité, marées et ombres urbaines non corrigées.",
)
GABON_EQUATORIAL_OWENDO = replace(
    GABON_EQUATORIAL_EXPERIMENTAL, profile_id="gabon-equatorial-owendo",
    label="Gabon équatorial · Owendo · expérimental", aoi_id="owendo",
    ecosystem="zones portuaires et industrielles, mangroves et estuaire",
    rationale="Paramètres repris du profil Akanda (même contexte déclaré), non validés pour les zones "
              "portuaires et l'estuaire. Humidité, marées et surfaces artificielles non corrigées.",
)

LOCAL_SCIENTIFIC_PROFILES = (GABON_EQUATORIAL_EXPERIMENTAL, GABON_SENSITIVITY_EXPERIMENTAL,
                             GABON_EQUATORIAL_LIBREVILLE, GABON_EQUATORIAL_OWENDO)
# Standard mode: one default profile per AOI, selected automatically.
REFERENCE_PROFILES = {profile.aoi_id: profile for profile in
                      (GABON_EQUATORIAL_EXPERIMENTAL, GABON_EQUATORIAL_LIBREVILLE, GABON_EQUATORIAL_OWENDO)}

ADVANCED_CONFIG_VERSION = "advanced-1"
ADVANCED_BOUNDS = {"change_threshold": (0.02, 0.50), "min_comparable_coverage_percent": (1.0, 100.0)}
# Policies keeping defective, uncertain, cloud or shadow classes are never offered:
# they would bypass the fundamental quality mask.
ADVANCED_SCL_POLICIES = tuple(
    name for name, classes in SCL_MASK_POLICIES.items()
    if not classes & (SCL_DEFECTIVE_CLASSES | SCL_UNCERTAIN_CLASSES | SCL_CLOUD_SHADOW_CLASSES))
SENSITIVITY_PRESET = {"change_threshold": 0.15, "min_comparable_coverage_percent": 50.0}


def reference_profile(aoi_id: str) -> ScientificProfile:
    """Default experimental profile for an AOI (standard mode)."""
    try:
        return REFERENCE_PROFILES[aoi_id]
    except KeyError:
        raise ValueError("Aucun profil de référence pour cette AOI.") from None


@dataclass(frozen=True)
class AnalysisConfiguration:
    """Mode, reference and effective profile; the effective fingerprint keys caches and exports."""

    mode: str                       # "standard" or "advanced"
    reference: ScientificProfile
    effective: ScientificProfile
    overrides: tuple[tuple[str, object], ...] = ()

    @property
    def provenance(self) -> dict:
        return {"mode": self.mode, "config_version": ADVANCED_CONFIG_VERSION,
                "reference_profile": f"{self.reference.profile_id} {self.reference.version}",
                "reference_fingerprint": self.reference.fingerprint,
                "effective_profile": f"{self.effective.profile_id} {self.effective.version}",
                "effective_fingerprint": self.effective.fingerprint,
                "overrides": {name: value for name, value in self.overrides},
                "validated": False}


def standard_configuration(aoi_id: str) -> AnalysisConfiguration:
    profile = reference_profile(aoi_id)
    return AnalysisConfiguration("standard", profile, profile)


def advanced_configuration(aoi_id: str, *, change_threshold=None, min_comparable_coverage_percent=None,
                           scl_policy=None) -> AnalysisConfiguration:
    """Derive a new, versioned profile; the reference profile itself is never modified.

    Values are checked against ``ADVANCED_BOUNDS`` and ``ADVANCED_SCL_POLICIES``.
    Without any effective change the reference profile is returned unchanged.
    """
    reference = reference_profile(aoi_id)
    requested = {"change_threshold": change_threshold,
                 "min_comparable_coverage_percent": min_comparable_coverage_percent,
                 "scl_policy": scl_policy}
    overrides = []
    for name, value in requested.items():
        if value is None or value == getattr(reference, name):
            continue
        if name == "scl_policy":
            if value not in ADVANCED_SCL_POLICIES:
                raise ValueError("Politique SCL non autorisée : elle conserverait des pixels nuageux, "
                                 "ombragés, incertains ou défectueux.")
        else:
            low, high = ADVANCED_BOUNDS[name]
            _finite_number(value, name, low, high)
            value = float(value)
        overrides.append((name, value))
    if not overrides:
        return AnalysisConfiguration("advanced", reference, reference)
    overrides = tuple(sorted(overrides))
    digest = hashlib.sha256(json.dumps([reference.fingerprint, ADVANCED_CONFIG_VERSION, overrides],
                                       ensure_ascii=True).encode("ascii")).hexdigest()
    changes = ", ".join(f"{name}={value}" for name, value in overrides)
    effective = replace(
        reference, **dict(overrides), profile_id=f"{reference.profile_id}-avance-{digest[:10]}",
        version=f"{reference.version}.{ADVANCED_CONFIG_VERSION}",
        label=f"{reference.label} · configuration avancée",
        usage="analyse avancée (paramètres choisis par l'utilisateur)", experimental=True,
        rationale=(f"Configuration avancée {ADVANCED_CONFIG_VERSION} dérivée de {reference.profile_id} "
                   f"{reference.version} (empreinte {reference.fingerprint[:16]}) : {changes}. "
                   "Paramètres non validés scientifiquement."))
    return AnalysisConfiguration("advanced", reference, effective, overrides)


def public_scientific_metadata(result):
    """Explicit allowlist: no paths, arbitrary rationale or context source text."""
    profile = getattr(result, "scientific_profile", None)
    interpretation = getattr(result, "interpretation", None)
    seasonal = getattr(result, "seasonal_assessment", None)
    return {
        "profile": None if profile is None else {
            "profile_id": profile.profile_id, "version": profile.version,
            "fingerprint": profile.fingerprint, "aoi_id": profile.aoi_id,
            "experimental": profile.experimental,
        },
        "interpretation_status": interpretation.status.value if interpretation else "not_assessed",
        "seasonal_status": seasonal.status if seasonal else "not_assessed",
        "season_relation": seasonal.season_relation if seasonal else "unknown",
        "aoi_sha256": getattr(result, "aoi_sha256", None),
        "persistent_change_validation": "not_performed_v1",
    }
