"""Territory selection and boundary comparison page; logic lives in services."""

import streamlit as st

from src.components.data_source import active_data_dir
from src.components.errors import page_guard
from src.components.territory_explorer import show_territories

with page_guard("territoires"):
    show_territories(active_data_dir())
