import sqlite3

import numpy as np
import streamlit as st

from processing import excel_bytes, route_frame
from processing.elev_profile import load_saved_elevation_fit
from processing.segmentation import segment_details, suggest_segments
from ui import context

st.title("Segmentation")
db, race_id, race = context()
result = st.session_state.get("elevation_profile_result")
expected_key = (race_id, race["gpx_hash"], race["fit_revision"])
if result is None or result[0] != expected_key:
	try:
		saved = load_saved_elevation_fit(db, race_id, race["gpx_hash"])
		if saved is not None:
			result = (expected_key, *saved)
			st.session_state["elevation_profile_result"] = result
	except (ValueError, np.linalg.LinAlgError, sqlite3.Error) as error:
		st.error(f"Could not load the saved elevation fit: {error}")
		st.stop()
if result is None or result[0] != expected_key:
	st.info("Fit the elevation profile for this race first. Segmentation reuses that fit without fitting again.")
	st.page_link("pages/2_elevation_profile.py", label="Elevation Profile", icon=":material/show_chart:")
	st.stop()

try:
	route = route_frame(db, race_id)
except ValueError as error:
	st.info(str(error))
	st.stop()

st.caption("Only HC, Cat 1 and Cat 2 climbs and descents create elevation boundaries and are kept whole. Runs of at "
	"least 4 points on one EV/RV road become their own segment, named by road, and are not split by other borders. "
	"Elsewhere, split at fylke/municipality borders and at the start/end of continuous private-road (smoothed IDs "
	"starting with P or S) and non-asphalt surface stretches longer than 2 km.")
_, fitted, extrema, _ = result
segments = db.segments(race_id)
if st.button("Generate and save segments", icon=":material/segment:"):
	try:
		db.save_segments(race_id, race["revision"], suggest_segments(fitted, extrema, route))
		st.rerun()
	except (ValueError, sqlite3.Error) as error:
		st.error(str(error))
if segments.empty:
	st.info("Generate and save the segments to review them.")
	st.stop()
try:
	segments = segment_details(fitted, route, segments)
except ValueError as error:
	st.error(f"Could not measure the saved segments: {error}")
	st.stop()

metrics = st.columns(3)
metrics[0].metric("Suggested segments", len(segments))
metrics[1].metric("Distance", f"{segments['distance_km'].sum():,.1f} km")
metrics[2].metric("Elevation fit k", fitted.attrs["k"])
st.dataframe(segments, hide_index=True, width="stretch")
st.caption("Names use the most common smoothed road name. Fully private sections get (Private), "
	"protected climbs get Climb, and other sections use their most common municipality.")
st.download_button("Download segments Excel", excel_bytes(segments, "Segments"),
	file_name="segments.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
	icon=":material/download:", on_click="ignore")
st.caption("Strava-equivalent category uses distance (m) × absolute average gradient (%), based on net "
	"original elevation change. Downhill categories are equivalents, not official Strava climb categories. "
	"Whole peak/valley sections are categorized before other boundaries are added; HC/Cat 1/Cat 2 sections are never split.")
st.caption("From/to describes the boundary reason. Borders use the first point with a new known fylke/municipality; "
	"missing values do not create borders. Administrative and private-road boundaries inside HC/Cat 1/Cat 2 sections "
	"are intentionally ignored. Private stretches use each edge's destination-point road ID, as on the map. "
	"private_distance_km reports private distance even when a protected climb contains mixed roads. "
	"Distance and gain/loss use original GPX data. "
	"Maximum absolute gradient (%) uses the centred five-sample average of signed original gradients.")
missing = route[["fylke", "kommunename", "roadname", "smoothed_vei_id"]].replace(r"^\s*$", None, regex=True).isna().sum()
if missing.any():
	st.warning("Some lookup labels are missing: " + ", ".join(
		f"{column}: {int(count):,} points" for column, count in missing.items() if count
	) + ". Review suggestions in those areas.")
