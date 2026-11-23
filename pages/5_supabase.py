import streamlit as st

from supabase_export import SCHEMA_FILE, supabase_package
from ui import get_db

st.title("Supabase export")
db = get_db()
st.info("Four tables: racemaster, route_data, route_segments and contacts. All GPX points are retained, including exclusions.")
st.download_button("Download Supabase SQL schema", SCHEMA_FILE.read_bytes(),
    file_name="supabase_schema.sql", mime="application/sql")
st.caption("Run the schema once in an empty Supabase schema. Import racemaster.csv, then route_data.csv, then route_segments.csv. "
           "contacts.csv holds all saved contacts and can be imported at any point (upsert on contact_id). "
           "Row-level security is enabled without browser-access policies; configure access before connecting Lovable.")
races = db.races()
with db.connection() as connection:
    assessed = {row[0] for row in connection.execute("SELECT DISTINCT race_id FROM route_segments WHERE total_risk IS NOT NULL")}
ready = races.loc[races["route_status"].eq("ready") & races["race_id"].isin(assessed)]
if ready.empty:
    st.info("Fit elevation, generate segments and save segment risks for at least one race to export data.")
    st.stop()
labels = ready.set_index("race_id")["race_name"].to_dict()
selected = st.selectbox("Races to export", [None, *ready["race_id"]],
    format_func=lambda value: "All completed races" if value is None else labels[value])
st.caption("Only assessed routes are exported. GPX uploads and the municipality reference table are not exported. "
           "For an already imported race, replace its previous rows rather than appending a duplicate.")
try:
    package = supabase_package(db, selected)
except (ValueError, OSError) as error:
    st.error(str(error))
    st.stop()
st.download_button("Download Supabase upload package", package,
    file_name="supabase_upload.zip", mime="application/zip", icon=":material/download:")
