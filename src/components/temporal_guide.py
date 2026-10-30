"""Reading guide placed before the two-date analyses of the home page; display only.

Three different readings are often confused: class transitions around an NDVI threshold, the
continuous NDVI variation, and the WorldCover land cover (a single map, no evolution). Thresholds
come from the profiles in use, never from literals.
"""

import streamlit as st

from src.components.formatting import format_decimal
from src.geo.scientific_profile import REFERENCE_PROFILES
from src.indicators.profiles import S2_NDVI_TRANSITION_V0

VARIATION_TITLE = "Variation continue du NDVI (analyse détaillée)"
TRANSITIONS_TITLE = "Transitions entre classes NDVI (surface commune)"


def show_temporal_guide(aoi_id: str) -> None:
    class_threshold = format_decimal(S2_NDVI_TRANSITION_V0.threshold, 1)
    variation = format_decimal(REFERENCE_PROFILES[aoi_id].change_threshold, 2)
    st.markdown("**Trois lectures différentes, à ne pas confondre**")
    st.markdown(
        f"- **Transitions entre classes NDVI** (juste en dessous) : surface observée aux deux dates qui passe "
        f"au-dessus ou au-dessous du seuil NDVI {class_threshold}. Une transition n'est ni une déforestation ni "
        "un reboisement.\n"
        f"- **Variation continue du NDVI** (section suivante) : écart de l'indice entre les deux dates, pixel par "
        f"pixel, qualifié de baisse ou de hausse au-delà de ±{variation} (profil standard). Une baisse ne prouve "
        "pas une perte de végétation.\n"
        "- **Couverture du sol WorldCover** (chiffres clés) : une seule carte (2021) ; aucune évolution n'en est "
        "tirée et elle ne se compare pas aux analyses Sentinel-2.")
    st.caption("Les deux analyses Sentinel-2 portent uniquement sur la surface observée aux deux dates (ni nuage, "
               "ni ombre, ni pixel exclu) : la surface non observée n'est ni estimée ni extrapolée.")
