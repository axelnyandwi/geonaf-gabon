
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject


SCL_NO_DATA_CLASSES = frozenset({0})
SCL_OBSERVED_CLASSES = frozenset(range(1, 12))
SCL_DEFECTIVE_CLASSES = frozenset({1})
SCL_UNCERTAIN_CLASSES = frozenset({2, 7})
SCL_CLOUD_SHADOW_CLASSES = frozenset({3, 8, 9, 10})
SCL_LAND_SURFACE_CLASSES = frozenset({2, 4, 5})
SCL_USABLE_LAND_CLASSES = frozenset({4, 5})
SCL_WATER_CLASSES = frozenset({6})
SCL_VEGETATION_CLASSES = frozenset({4})
SCL_BARE_SOIL_CLASSES = frozenset({5})
SCL_SNOW_ICE_CLASSES = frozenset({11})

SCL_MASK_POLICIES = {
    "observation": SCL_OBSERVED_CLASSES,
    "land_surface": SCL_LAND_SURFACE_CLASSES,
    "usable_land": SCL_USABLE_LAND_CLASSES,
    "vegetation": SCL_VEGETATION_CLASSES,
    "bare_soil": SCL_BARE_SOIL_CLASSES,
}


def change_policy_restrictions(policy):
    """Eligibility for land-change interpretation, not a raster-mask change."""
    if policy not in SCL_MASK_POLICIES:
        raise ValueError("Politique SCL inconnue.")
    classes = SCL_MASK_POLICIES[policy]
    restrictions = []
    if classes & (SCL_DEFECTIVE_CLASSES | SCL_UNCERTAIN_CLASSES | SCL_CLOUD_SHADOW_CLASSES):
        restrictions.append("La politique SCL conserve des pixels défectueux, incertains, nuageux ou ombragés.")
    if not SCL_USABLE_LAND_CLASSES.issubset(classes):
        restrictions.append("La politique SCL exclut systématiquement les transitions végétation vers sol nu.")
    return tuple(restrictions)


def scl_mask_from_values(scl_values, policy="observation", valid_mask=None):
    """Build a named SCL mask while respecting class 0 and source NoData.

    ``observation`` means an SCL class was observed (classes 1–11), including
    clouds, water, and uncertain classes. ``land_surface`` keeps dark-area,
    vegetation, and bare-soil classes (2, 4, 5), but excludes unclassified 7.
    More specific policies select reliable land, vegetation, or bare soil.
    """
    if policy not in SCL_MASK_POLICIES:
        allowed = ", ".join(sorted(SCL_MASK_POLICIES))
        raise ValueError(f"Politique SCL inconnue : {policy}. Choix : {allowed}")

    values = np.ma.asarray(scl_values)
    mask = ~np.ma.getmaskarray(values)
    mask &= np.isin(np.ma.getdata(values), tuple(SCL_MASK_POLICIES[policy]))

    if valid_mask is not None:
        valid_mask = np.asarray(valid_mask, dtype=bool)
        if valid_mask.shape != values.shape:
            raise ValueError("Dimensions du masque de validité incorrectes.")
        mask &= valid_mask

    return mask


def create_scl_mask(
    scl_path,
    reference_raster_path,
    policy="usable_land",
):
    """
    Aligne SCL sur une grille de référence et applique une politique nommée.

    La politique par défaut ``usable_land`` conserve le comportement historique
    (classes 4 et 5). Utiliser ``land_surface`` pour inclure aussi la classe 2.
    """

    with rasterio.open(reference_raster_path) as reference:
        if reference.crs is None:
            raise ValueError("CRS du raster de référence manquant.")
        with rasterio.open(scl_path) as scl:
            if scl.crs is None:
                raise ValueError("CRS du raster SCL manquant.")

            source_scl = scl.read(1, masked=True)
            source_validity = ~np.ma.getmaskarray(source_scl)
            target_shape = (reference.height, reference.width)
            aligned_values = np.zeros(target_shape, dtype=np.uint8)
            aligned_validity = np.zeros(target_shape, dtype=np.uint8)
            for source, destination in (
                (np.ma.getdata(source_scl).astype(np.uint8), aligned_values),
                (source_validity.astype(np.uint8), aligned_validity),
            ):
                reproject(
                    source=source,
                    destination=destination,
                    src_transform=scl.transform,
                    src_crs=scl.crs,
                    dst_transform=reference.transform,
                    dst_crs=reference.crs,
                    resampling=Resampling.nearest,
                )

            return scl_mask_from_values(
                aligned_values,
                policy=policy,
                valid_mask=aligned_validity.astype(bool),
            )
