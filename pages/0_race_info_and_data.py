from datetime import date
import hashlib

import streamlit as st

from processing import excel_bytes, job_state, start_lookup, store_gpx
from processing.data_handler import RACE_METADATA_FIELDS
from processing.smoothing import smooth_race
from ui import context, lookup_progress

st.title("Race info and Data")
db, race_id, race = context()
st.caption(race["race_name"])

with st.form(f"metadata_{race_id}"):
	columns = st.columns([3, 1])
	edited_name = columns[0].text_input("Race name", race["race_name"], key=f"edit_name_{race_id}")
	edited_date = columns[1].date_input(
		"Race date", date.fromisoformat(race["race_date"]) if race["race_date"] else None,
	)
	locations = st.columns(2)
	start = locations[0].text_input("Start Location", race["start_location"], key=f"edit_{race_id}_start")
	finish = locations[1].text_input("Finish Location", race["finish_location"], key=f"edit_{race_id}_finish")
	organizer = st.text_input("Organizer", race["organizer"], key=f"edit_{race_id}_organizer")
	address = st.text_area("Organizer Address", race["organizer_address"], key=f"edit_{race_id}_address")
	contacts = st.columns(2)
	email = contacts[0].text_input("Organizer e-mail", race["organizer_email"], key=f"edit_{race_id}_email")
	phone = contacts[1].text_input("Organizer phone number", race["organizer_phone"], key=f"edit_{race_id}_phone")
	if st.form_submit_button("Save metadata", icon=":material/save:"):
		try:
			metadata = dict(zip(RACE_METADATA_FIELDS, [start, finish, organizer, address, email, phone]))
			db.update_race(race_id, edited_name, edited_date, **metadata)
			st.rerun()
		except ValueError as error:
			st.error(str(error))

st.subheader("Route data")
state = job_state(db, race_id)
running = bool(state and state["status"] == "running")
include_properties = st.checkbox("Include property lookup (Kartverket)",
	value=bool(race["lookup_properties"]), disabled=running, key=f"lookup_properties_{race_id}",
	help="Optional cadastral numbers. Adds an API lookup per GPX point. Enable and resume to fill skipped points.")
if include_properties != bool(race["lookup_properties"]):
	db.set_lookup_properties(race_id, include_properties)
	st.rerun()
upload = st.file_uploader("GPX track", type=["gpx"], key=f"gpx_{race_id}")
if upload:
	content = upload.getvalue()
	digest = hashlib.sha256(content).hexdigest()
	if digest != race["gpx_hash"]:
		try:
			store_gpx(db, race_id, upload.name, content)
			st.rerun()
		except (ValueError, OSError) as error:
			st.error(str(error))
lookup_progress(db, race_id)
state = job_state(db, race_id)
running = bool(state and state["status"] == "running")
if race["gpx_hash"]:
	st.caption(f"GPX: {race['gpx_filename']}")
	st.download_button("Download GPX", db.gpx_content(race_id), file_name=race["gpx_filename"],
		mime="application/gpx+xml", icon=":material/download:")
	if st.button("Resume / rerun lookup", icon=":material/refresh:", disabled=running):
		start_lookup(db, race_id)
		st.rerun()

if st.button("Run smoothing", icon=":material/timeline:", disabled=running or race["route_status"] != "ready",
		help="Select roads using BIC and inherit road attributes from donor points, preserving original lookup data."):
	try:
		with st.spinner("Smoothing route"):
			smoothed = smooth_race(db, race_id)
		changed = int(smoothed["smoothed_vei_id"].ne(smoothed["vei_id"]).sum())
		roads = smoothed["smoothed_vei_id"].dropna().unique()
		st.success(f"Smoothing complete: {len(roads)} roads selected, {changed:,} points assigned a different road.")
	except ValueError as error:
		st.error(str(error))

frame = db.read_table("road_data", race_id, smoothed=True)
excluded = int(frame["excluded"].eq(True).sum())
st.caption(f"{len(frame):,} route points; {excluded:,} marked for exclusion")
st.info("All lookup data is retained. Maps and packages ignore rows marked excluded "
	"(none are, until you add your own smoothing).")
st.dataframe(frame, hide_index=True, width="stretch")
if not frame.empty:
	st.download_button("Download Excel", excel_bytes(frame, "Veikartreferanser"),
		file_name="route_data.xlsx", icon=":material/download:", key=f"download_road_data_{race_id}")
with st.expander("Municipality reference"):
	municipalities = db.municipalities()
	st.dataframe(municipalities, hide_index=True, width="stretch")
	st.download_button("Download municipalities Excel", excel_bytes(municipalities, "Municipalities"),
		file_name="municipalities.xlsx", icon=":material/download:")