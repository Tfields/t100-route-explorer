"""About the data - what T-100 is, what is counted, and how carriers,
families and regions are defined across the app."""
import pathlib
import sys

import streamlit as st

sys.path.append(str(pathlib.Path(__file__).resolve().parent.parent))
from warehouse import about_data

st.set_page_config(page_title="T-100 About the Data", page_icon="ℹ️", layout="wide")

st.title("ℹ️ About the data")
about_data()
