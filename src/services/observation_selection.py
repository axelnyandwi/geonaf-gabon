"""Exact UTC-day selection of verified catalogue observations; no raster I/O."""

from datetime import date, datetime, timezone

from src.services.local_preview import LocalPreviewError

from datetime import timezone

def acquisition_day(observation):
    return observation.acquired_at.astimezone(timezone.utc).date()


def catalogue_bounds(observations):
    if not observations:
        raise LocalPreviewError('Catalogue local sans acquisition vérifiée.')
    days = [acquisition_day(item) for item in observations]
    return min(days), max(days)


def observations_on_day(observations, requested_day):
    """Never choose a nearest day; None means no calendar date requested."""
    if requested_day is None:
        return ()
    if not isinstance(requested_day, date) or isinstance(requested_day, datetime):
        raise LocalPreviewError('Une journée de calendrier est requise.')
    return tuple(item for item in observations if acquisition_day(item) == requested_day)


def resolve_observation(observations, requested_day, choice=None):
    if requested_day is None:
        raise LocalPreviewError('Choisir une date pour chaque calendrier.')
    if not isinstance(requested_day, date) or isinstance(requested_day, datetime):
        raise LocalPreviewError('Une journée de calendrier est requise.')
    minimum, maximum = catalogue_bounds(observations)
    if not minimum <= requested_day <= maximum:
        raise LocalPreviewError('Date demandée hors des bornes du catalogue local.')
    candidates = observations_on_day(observations, requested_day)
    if not candidates:
        raise LocalPreviewError(f'Aucune acquisition le {requested_day:%d/%m/%Y} UTC ; aucune autre date ne la remplace.')
    if choice is not None:
        if choice not in candidates:
            raise LocalPreviewError('L’acquisition choisie ne correspond pas à la journée demandée.')
        return choice
    if len(candidates) > 1:
        raise LocalPreviewError(f'Plusieurs acquisitions le {requested_day:%d/%m/%Y} UTC : choisir explicitement une heure et un produit.')
    return candidates[0]


def observation_label(observation):
    timestamp = observation.acquired_at.astimezone(timezone.utc)
    product_id = getattr(observation, "product_id", None)

    label = timestamp.strftime("%d/%m/%Y · %H:%M:%S UTC")

    if product_id:
        label += f" · {product_id}"

    return label


def validate_chronology(earlier, later):
    if earlier.acquired_at >= later.acquired_at or earlier.product_id == later.product_id:
        raise LocalPreviewError('Choisir deux acquisitions distinctes dans l’ordre chronologique ; aucune inversion automatique.')
