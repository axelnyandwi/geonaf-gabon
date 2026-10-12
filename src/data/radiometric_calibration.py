
from pathlib import Path
import xml.etree.ElementTree as ET


# Correspondance entre les bandes Sentinel-2
# et les identifiants utilisés dans le XML.
BAND_IDS = {
    "B02": "1",
    "B03": "2",
    "B04": "3",
    "B08": "7",
    "B11": "11",
    "B12": "12",
}


def read_sentinel2_calibration(xml_path, bands=("B04", "B08")):
    """
    Lit les paramètres de calibration d'un produit Sentinel-2 L2A.

    Retourne :
    {
        "quantification_value": 10000.0,
        "offsets": {"B04": -1000.0, "B08": -1000.0}
    }
    """
    xml_path = Path(xml_path)

    if not xml_path.is_file():
        raise FileNotFoundError(
            "Métadonnées Sentinel-2 introuvables."
        )

    try:
        from src.diagnostics import safe_call

        root = safe_call(ET.parse, xml_path, paths=(xml_path,)).getroot()
    except (OSError, ET.ParseError) as error:
        from src.diagnostics import safe_exception_message

        detail = safe_exception_message(error, paths=(xml_path,))
        raise ValueError(
            f"Lecture des métadonnées Sentinel-2 impossible ({type(error).__name__}) : {detail}"
        ) from None

    quantification = None
    offsets_by_id = {}

    for element in root.iter():
        tag = element.tag.split("}")[-1]

        if tag == "BOA_QUANTIFICATION_VALUE":
            quantification = float(element.text)

        elif tag == "BOA_ADD_OFFSET":
            band_id = element.attrib.get("band_id")
            if band_id is not None:
                offsets_by_id[band_id] = float(element.text)

    if quantification is None or quantification <= 0:
        raise ValueError("Facteur de quantification absent ou invalide.")

    offsets = {}

    for band in bands:
        if band not in BAND_IDS:
            raise ValueError(f"Bande non prise en charge : {band}")

        band_id = BAND_IDS[band]

        if band_id not in offsets_by_id:
            raise ValueError(f"Offset absent pour {band}")

        offsets[band] = offsets_by_id[band_id]

    return {
        "quantification_value": quantification,
        "offsets": offsets,
    }
