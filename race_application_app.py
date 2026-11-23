"""Run with: python -m streamlit run '01 Dashboard\\race_application_app.py'."""

import sys
from html import escape
from pathlib import Path

import streamlit as st

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
from ui import get_db

st.set_page_config(page_title="Race Applications", page_icon=":bike:", layout="wide")
st.markdown("""<style>
[data-testid="stSidebar"] {background: #173e36;}
[data-testid="stSidebar"] p, [data-testid="stSidebar"] a, [data-testid="stSidebar"] span {color: #f5f8f5;}
[data-testid="stSidebar"] h2 {color: #f5f8f5;}
[data-testid="stSidebar"] [aria-current="page"] {border-left: 3px solid #9ac24b; background: #285044;}
[data-testid="stMetricValue"] {font-size: 1.4rem;}
</style>""", unsafe_allow_html=True)

db = get_db()
if "pending_race_id" in st.session_state:
    st.session_state["selected_race_id"] = st.session_state.pop("pending_race_id")
    st.session_state.pop("race_picker", None)
race_label = st.empty()


def show_race_name(name: str) -> None:
    race_label.html(
        '<div style="text-align: right; font-size: 0.875rem; overflow-wrap: anywhere;">'
        f'Active race: {escape(name)}</div>'
    )


race_id = st.session_state.get("selected_race_id")
with st.sidebar:
    st.header("Race Applications")
    if race_id:
        race = db.race(race_id)
        st.caption(race["race_name"] + (f" | {race['race_date']}" if race["race_date"] else ""))
        show_race_name(race["race_name"])

navigation = st.navigation({
    "Project": [
        st.Page("pages/0_races.py", title="Races", icon=":material/flag:"),
        st.Page("pages/0_race_info_and_data.py", title="Race info and Data", icon=":material/edit:"),
    ],
    "Application": [
        st.Page("pages/2_elevation_profile.py", title="Elevation Profile", icon=":material/show_chart:"),
        st.Page("pages/2_segmentation.py", title="Segmentation", icon=":material/segment:"),
        st.Page("pages/2_risk_analysis.py", title="Risk Analysis", icon=":material/analytics:"),
        st.Page("pages/3_maps.py", title="Maps", icon=":material/map:"),
    ],
    "Export": [
        st.Page("pages/4_race_packages.py", title="Race Packages", icon=":material/folder_zip:"),
        st.Page("pages/5_supabase.py", title="Supabase export", icon=":material/cloud_upload:"),
    ],
})
navigation.run()

# The Races page can change the selection during this run.
current_race_id = st.session_state.get("selected_race_id")
if current_race_id and current_race_id != race_id:
    show_race_name(db.race(current_race_id)["race_name"])