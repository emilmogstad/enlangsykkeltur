import sqlite3

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

from processing import route_frame
from processing.elev_profile import load_saved_elevation_fit
from processing.map_profiles import RISK_COLOR_STOPS
from processing.risk_analysis import calculate_risks, summarize_segment_risks
from ui import context

RISK_LABELS = {
	"traffic_risk": "Traffic Risk",
	"environmental_risk": "Environmental Risk",
	"crash_risk": "Crash Risk",
	"fatigue_risk": "Fatigue Risk",
	"remoteness_risk": "Remoteness Risk",
}


def show_risk(scores: pd.DataFrame, risk: str, label: str) -> None:
	st.subheader(label)
	values = scores[risk]
	scored = int(values.count())
	metrics = st.columns(3)
	metrics[0].metric("Mean score", f"{values.mean():.2f} / 5" if scored else "Not scored")
	metrics[1].metric("Highest score", f"{values.max():.2f} / 5" if scored else "Not scored")
	metrics[2].metric("Points scored", f"{scored:,} / {len(scores):,}")

	if scored:
		chart = alt.Chart(scores[["route_order", "distance_cum_km", "_route_path", risk]]).mark_line(
			color="#176b72", invalid="break-paths-filter-domains", point=scored == 1,
			interpolate="step-after" if risk == "crash_risk" else "linear",
		).encode(
			x=alt.X("distance_cum_km:Q", title="Distance along route (km)"),
			y=alt.Y(f"{risk}:Q", title=label, scale=alt.Scale(domain=[0, 5])),
			detail="_route_path:N",
			order="route_order:Q",
			tooltip=[alt.Tooltip("distance_cum_km:Q", title="Distance (km)", format=".1f"),
				alt.Tooltip(f"{risk}:Q", title="Risk score", format=".2f")],
		).properties(height=280)
		st.altair_chart(chart, width="stretch")


def show_segment_risk(segments: pd.DataFrame) -> None:
	st.subheader("Aggregate Risk by segment")
	st.caption("Each horizontal line spans the full segment at its highest point-by-point Aggregate Risk.")
	plot_data = segments[["segment", "segment_name", "start_distance_km", "end_distance_km", "total_risk"]].copy()
	if plot_data["total_risk"].isna().all():
		st.info("No saved segment scores to plot.")
		return
	chart = alt.Chart(plot_data).mark_rule(strokeWidth=4).encode(
		x=alt.X("start_distance_km:Q", title="Distance along route (km)"),
		x2="end_distance_km:Q",
		y=alt.Y("total_risk:Q", title="Aggregate Risk", scale=alt.Scale(domain=[0, 5])),
		color=alt.Color("total_risk:Q", title="Segment risk",
			scale=alt.Scale(domain=[0, 2.5, 5], range=list(RISK_COLOR_STOPS))),
		tooltip=[
			alt.Tooltip("segment:N", title="Segment"),
			alt.Tooltip("segment_name:N", title="Name"),
			alt.Tooltip("start_distance_km:Q", title="Start (km)", format=".2f"),
			alt.Tooltip("end_distance_km:Q", title="End (km)", format=".2f"),
			alt.Tooltip("total_risk:Q", title="Maximum Aggregate Risk", format=".2f"),
		],
	).properties(height=280)
	st.altair_chart(chart, width="stretch")


st.title("Risk Analysis")
db, race_id, race = context()
try:
	route = route_frame(db, race_id)
except ValueError as error:
	st.info(str(error))
	st.stop()

fit_key = (race_id, race["gpx_hash"], race["fit_revision"])
fit = st.session_state.get("elevation_profile_result")
if fit is None or fit[0] != fit_key:
	fit = None
	try:
		saved = load_saved_elevation_fit(db, race_id, race["gpx_hash"])
		if saved is not None:
			fit = (fit_key, *saved)
			st.session_state["elevation_profile_result"] = fit
	except (ValueError, np.linalg.LinAlgError, sqlite3.Error) as error:
		st.error(f"Could not load the saved elevation fit: {error}")
if fit is None:
	st.info("Crash and Aggregate Risk need a fitted elevation profile. The other risks are still available.")
	st.page_link("pages/2_elevation_profile.py", label="Elevation Profile", icon=":material/show_chart:")

try:
	scores = calculate_risks(route, fit[3] if fit is not None else None)
except ValueError as error:
	st.info(str(error))
	st.stop()

st.caption("Scores run from 0 to 5 and use smoothed road attributes where available. Point scores are calculated on demand.")
st.caption("If AADT, heavy-vehicle share or speed limit is missing or invalid, Traffic Risk counts as 0 in Aggregate Risk.")

pointwise, segmentwise = st.tabs(["Point-by-point", "By segment"])
with pointwise:
	show_risk(scores, "aggregate_risk", "Aggregate Risk")
with segmentwise:
	if fit is None:
		st.info("Fit the elevation profile to see the segment-wise risks.")
	else:
		segments = db.segments(race_id)
		if segments.empty:
			st.info("Generate and save segments first.")
			st.page_link("pages/2_segmentation.py", label="Segmentation", icon=":material/segment:")
		else:
			if st.button("Calculate and save segment risks", icon=":material/save:"):
				try:
					summary = summarize_segment_risks(segments, scores)
					db.save_segment_risks(race_id, race["revision"], summary.assign(segment=segments["segment"]))
					segments = db.segments(race_id)
					st.success("Segment risks saved.")
				except (ValueError, sqlite3.Error) as error:
					st.error(str(error))
			show_segment_risk(segments)

with st.expander("Individual risks"):
	for risk, label in RISK_LABELS.items():
		show_risk(scores, risk, label)

with st.expander("How the scores are calculated"):
	st.markdown("""
- **Aggregate:** average of all five scores, including any Traffic Risk scores set to 0 due to missing inputs.
- **Traffic:** twice the sum of AADT / 10,000, heavy-vehicle percentage / 20 and speed limit / 80.
  Each factor and the final score are capped at 5. If any of these inputs is missing or invalid,
  the entire Traffic Risk score is 0. Accident history is not used.
- **Environmental:** rises from 0 at 200 m to 5 at 1,000 m, based on GPX elevation.
- **Crash:** uses the Strava-equivalent category of each downhill section from the fitted elevation
  profile: HC = 5, Cat 1 = 4, Cat 2 = 3, Cat 3 = 2, Cat 4 = 1. Uncategorised sections,
  climbs and flat sections score 0.
- **Fatigue:** rises from 0 to 5 over the first 300 km, then stays at 5.
- **Remoteness:** falls from 5 at zero AADT to 0 at 400 AADT. For a run with missing AADT, measure
  from the first to the last missing point: up to 2 km scores 0; beyond that the score rises from
  2 to 5 at 10 km and stays at 5.
""")
