"""Native calendars and explicit acquisition disambiguation for Streamlit."""

import streamlit as st

from src.services.observation_selection import (
    catalogue_bounds, observation_label, observations_on_day,
    resolve_observation, validate_chronology,
)


def pick_observations(observations):
    minimum, maximum = catalogue_bounds(observations)
    st.caption(f'Catalogue local : {len(observations)} acquisitions vérifiées, '
               f'du {minimum:%d/%m/%Y} au {maximum:%d/%m/%Y} UTC. '
               'Une journée du calendrier peut ne contenir aucune acquisition.')
    columns = st.columns(2)
    dates, choices, ambiguous = [], [], False
    for index, (column, default) in enumerate(zip(columns, (minimum, maximum)), 1):
        with column:
            day = st.date_input(f'Date {index}', value=default, min_value=minimum,
                                max_value=maximum, format='DD/MM/YYYY', key=f'acquisition_date_{index}',
                                help='Journée UTC demandée. Seules les acquisitions réellement présentes '
                                     'dans le catalogue sont proposées ; aucune date voisine n’est substituée.')
            candidates = observations_on_day(observations, day)
            choice = None
            if len(candidates) > 1:
                ambiguous = True
                st.info(f'{len(candidates)} acquisitions disponibles pour cette journée UTC.')
                choice = st.selectbox(f'Acquisition de la date {index}', candidates, index=None,
                                      key=f'acquisition_choice_{index}_{day.isoformat()}',
                                      format_func=observation_label,
                                      placeholder='Choisir une heure UTC et un produit')
            elif candidates:
                choice = candidates[0]
            if choice is not None:
                st.caption('Acquisition sélectionnée : '+observation_label(choice))
            elif day is not None and not candidates:
                st.warning(f'Aucune acquisition le {day:%d/%m/%Y} UTC. Choisir une autre journée explicitement.')
            dates.append(day); choices.append(choice)
    submitted = st.button('Afficher les dates sélectionnées')
    if None not in dates and dates[0] > dates[1]:
        from src.services.local_preview import LocalPreviewError
        raise LocalPreviewError('Dates inversées : respecter l’ordre chronologique ; aucune permutation automatique.')
    if any(len(observations_on_day(observations, day)) > 1 and choice is None
           for day, choice in zip(dates, choices)):
        st.info('Choisir explicitement une acquisition pour chaque journée multiple, puis afficher les dates sélectionnées.')
        st.stop()
    earlier = resolve_observation(observations, dates[0], choices[0])
    later = resolve_observation(observations, dates[1], choices[1])
    validate_chronology(earlier, later)
    return earlier, later, submitted, ambiguous


def observation_quality_notices(result):
    """Explain existing masks/coverage, without calculating a new quality score."""
    from src.geo.pair_selection import PairSelectionConfig

    messages = []
    for name, mask in (('initiale', result.comparison.valid_date_1),
                       ('finale', result.comparison.valid_date_2)):
        if not mask.any():
            messages.append(f'Observation {name} : aucun pixel NDVI exploitable après les masques de validité et SCL. '
                            'Cette acquisition est conservée ; aucune autre date ne la remplace.')
    analysis = result.analysis
    policy = PairSelectionConfig()
    coverage = analysis.comparable_coverage_percent
    if analysis.statistics['count'] == 0:
        messages.append('Aucun pixel comparable aux deux dates : ΔNDVI et changements non interprétables. '
                        'Les cartes individuelles conservent leurs observations propres.')
    elif coverage < policy.exploratory_coverage_percent:
        messages.append(f'Couverture comparable insuffisante ({coverage:.2f} %) selon le repère opérationnel existant '
                        f'de {policy.exploratory_coverage_percent:g} %. Les résultats ne représentent pas toute l’AOI. '
                        'Ce repère n’est pas un seuil scientifique universel.')
    elif coverage < policy.representative_coverage_percent:
        messages.append(f'Couverture comparable limitée ({coverage:.2f} %) : lecture exploratoire, sous le repère '
                        f'opérationnel existant de {policy.representative_coverage_percent:g} %. '
                        'Ce repère ne valide pas la représentativité scientifique.')
    return tuple(messages)
