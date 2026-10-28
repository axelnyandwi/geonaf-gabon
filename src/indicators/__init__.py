"""Territorial indicators, split by responsibility; no Streamlit, no network.

* ``model``      — indicator records and status / reliability vocabulary;
* ``profiles``   — versioned groupings (a new territory or ecosystem gets a new profile);
* ``zone``       — the study zone as a geometry file (acquisition square today, any geometry later);
* ``landcover``, ``osm`` — measurements on loaded layers (areas, lengths, counts);
* ``provenance`` — dates, sources and limits attached to each value;
* ``quality``    — reliability rules and temporal comparability;
* ``inventory``  — offline inventory of stored layers for a zone (acquisition side);
* ``assembly``   — builds the indicator set from the pieces above.

``src.services.territorial_indicators`` re-exports the public names for existing callers.
"""
