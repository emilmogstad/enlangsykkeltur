"""Shared race context and lookup progress controls."""

import os

import streamlit as st
from processing.data_handler import data_handler
from processing import cancel_job, job_state


@st.cache_resource
def database(path):
    return data_handler(path)


def get_db():
    return database(os.environ.get("RACE_APP_DB"))


def context():
    db = get_db()
    race_id = st.session_state.get("selected_race_id")
    if race_id is None:
        st.info("No race selected.")
        st.page_link("pages/0_races.py", label="Races", icon=":material/flag:")
        st.stop()
    return db, race_id, db.race(race_id)


@st.fragment(run_every="2s")
def lookup_progress(db, race_id):
    state = job_state(db, race_id)
    race = db.race(race_id)
    status_key = f"lookup_status_{race_id}"
    previous = st.session_state.get(status_key)
    st.session_state[status_key] = race["route_status"]
    if previous == "processing" and race["route_status"] != "processing":
        st.rerun()
    if state and race["route_status"] in ("processing", "ready", "failed", "cancelled"):
        fraction = min(state["done"] / max(state["total"], 1), 1)
        st.progress(fraction,
                    text=f"{state['message']} - {fraction:.0%} ({state['done']:,} / {state['total']:,} GPX points)")
    if state and state["status"] == "running":
        if st.button("Cancel lookup", icon=":material/stop:", key=f"cancel_{race_id}"):
            cancel_job(db, race_id)
    elif race["route_status"] == "ready":
        st.success("Route lookup complete.")
    elif race["route_status"] in ("failed", "cancelled"):
        st.error(race["route_error"] or "Lookup did not complete.")
    elif race["route_status"] == "processing":
        st.warning("Lookup was interrupted by an app restart. Resume the lookup below.")