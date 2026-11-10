"""Multisource analyses page; scientific processing lives in services."""

import streamlit as st

from src.components.data_source import active_data_dir
from src.components.errors import page_guard
from src.components.multisource import show_multisource

with page_guard("analyses_multisources"):
    show_multisource(active_data_dir(), shared_zone=True)
