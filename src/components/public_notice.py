"""Notice shown under the study-zone selector: zone status, Sentinel-2 dates, attribution, limits.

Texts come from src.services.public_notice (also used by the CSV export). The dates are those of the
verified local observations of the zone (the demo pack on the Cloud); nothing is downloaded or computed.
"""

from pathlib import Path

import streamlit as st

from src.services import local_preview
from src.services.local_preview import LocalPreviewError
from src.services.public_notice import (
    INTERPRETATION, SENTINEL_LICENCE, acquisition_dates_text, sentinel2_attribution, zone_statement,
)


def show_data_notice(data_dir, aoi_id: str, label: str) -> None:
    try:   # Resolved at call time: the same discovery as the analyses below, never a stale binding.
        dates = [observation.acquired_at for observation in local_preview.list_local_observations(Path(data_dir), aoi_id)]
    except (LocalPreviewError, OSError):   # The analyses below report the error themselves.
        dates = None
    st.info(zone_statement(label))
    st.caption(" · ".join((acquisition_dates_text(dates),
                           f"Images : {sentinel2_attribution(d.year for d in dates or ())}. {SENTINEL_LICENCE}",
                           INTERPRETATION)))
