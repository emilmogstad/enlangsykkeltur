import sqlite3

import matplotlib.pyplot as plt
import numpy as np
import streamlit as st

from processing.elev_profile import (
	add_strava_categories,
	elevation_segments,
	find_peaks_and_valleys,
	load_elevation_profile,
	load_saved_elevation_fit,
	plot_elevation_profile,
	select_cosine_bic,
)
from ui import context

st.title("Elevation Profile")
db, race_id, race = context()
try:
	profile = load_elevation_profile(race_id, db)
except ValueError as error:
	st.info(str(error))
	st.stop()

st.caption("Automatic BIC fitting: steps 20, 5, 1; maximum k is the number of points divided by 100.")
if len(profile) < 100:
	st.info("At least 100 GPX points are required for automatic fitting.")

result_key = (race_id, race["gpx_hash"], race["fit_revision"])
if st.button("Fit Elevation-profile", icon=":material/show_chart:", disabled=len(profile) < 100):
	ceiling = len(profile) // 100
	progress = st.progress(0.0, text=f"Starting fit: k=0 / {ceiling}")

	def update_progress(k: int) -> None:
		progress.progress(k / ceiling, text=f"Testing k={k} / {ceiling}")

	try:
		with st.spinner("Fitting elevation profile and generating segments..."):
			fitted = select_cosine_bic(profile, on_progress=update_progress)
			progress.progress(1.0, text=f"Fit complete: selected k={fitted.attrs['k']}. Generating segments...")
			extrema = find_peaks_and_valleys(
				fitted.attrs["cosine_coefficients"],
				float(profile["route_order"].min()), float(profile["route_order"].max()))
			segments = elevation_segments(profile, extrema)
			db.save_elevation_fit(race_id, race["gpx_hash"], fitted.attrs["cosine_coefficients"].tolist(), fitted, extrema)
			result_key = (race_id, race["gpx_hash"], db.race(race_id)["fit_revision"])
			st.session_state["elevation_profile_result"] = (result_key, fitted, extrema, segments)
			progress.progress(1.0, text=f"Saved fit: selected k={fitted.attrs['k']}")
	except (ValueError, np.linalg.LinAlgError, sqlite3.Error) as error:
		progress.empty()
		st.error(str(error))

result = st.session_state.get("elevation_profile_result")
if result is None or result[0] != result_key:
	try:
		saved = load_saved_elevation_fit(db, race_id, race["gpx_hash"], profile)
		if saved is not None:
			result = (result_key, *saved)
			st.session_state["elevation_profile_result"] = result
	except (ValueError, np.linalg.LinAlgError, sqlite3.Error) as error:
		st.error(f"Could not load the saved elevation fit: {error}")
if result is not None and result[0] == result_key:
	_, fitted, extrema, segments = result
	segments = add_strava_categories(segments)
	metrics = st.columns(3)
	metrics[0].metric("Selected k", fitted.attrs["k"])
	metrics[1].metric("Peaks", int(extrema["kind"].eq("peak").sum()))
	metrics[2].metric("Valleys", int(extrema["kind"].eq("valley").sum()))
	figure = plot_elevation_profile(fitted, extrema)
	st.pyplot(figure, width="stretch")
	plt.close(figure)
	st.subheader("Elevation segments")
	st.caption("Boundaries follow fitted peaks and valleys, including route start/end. Distance, gain/loss and "
		"gradient (%) use original GPX data. Maximum gradient is the largest absolute centred five-sample "
		"average of signed point-to-point gradients, using available samples at route endpoints. "
		"Fractional boundaries split edges proportionally; averaging spans segment boundaries. "
		"Original elevation noise can produce large gradients. The fit is saved separately from the original elevations.")
	st.dataframe(segments, hide_index=True, width="stretch")
	st.caption("Strava-equivalent category uses distance (m) × absolute average gradient (%), based on net "
		"original elevation change. Downhill categories are equivalents, not official Strava climb categories.")
