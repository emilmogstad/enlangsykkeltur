import math
import pandas as pd
import streamlit as st

from processing import filter_route, fylke_options, route_frame, viewer
from processing.map_profiles import RISK_COLOR_STOPS, colored_elevation_chart, colored_runs, map_paths
from ui import context

# The map and elevation chart expect a points-of-interest table; there are none.
NO_POIS = pd.DataFrame(columns=["name", "lat", "lon", "distance_cum_km", "elevation_m", "number", "route_index"])

st.title("Maps")
db, race_id, race = context()
st.caption(race["race_name"])
try:
	full_route = route_frame(db, race_id)
except (ValueError, KeyError) as error:
	st.info(str(error))
	st.stop()
controls = st.columns(2)
fylke = controls[0].selectbox("Fylke", [None, *fylke_options(full_route)],
	format_func=lambda value: "All fylker" if value is None else value, key=f"map_fylke_{race_id}")
basemap = controls[1].toggle("Topographic background", value=True)
maximum = max(1, math.ceil(float(full_route["distance_cum_km"].iloc[-1])))
distance_range = st.slider("Distance (km)", 0, maximum, (0, maximum), key=f"map_distance_{race_id}_{maximum}")
route = filter_route(full_route, fylke, distance_range)
if len(route) < 2:
	st.warning("Select a wider distance range.")
	st.stop()
metrics = st.columns(3)
paths = route.groupby("_route_path")
distance = paths["distance_cum_km"].agg(lambda values: values.iloc[-1] - values.iloc[0]).sum()
ascent = paths["elev_gain_cum_m"].agg(lambda values: values.iloc[-1] - values.iloc[0]).sum()
metrics[0].metric("Distance", f"{distance:,.1f} km")
metrics[1].metric("Ascent", f"{ascent:,.0f} m")
metrics[2].metric("Highest point", f"{route.elevation_m.max():,.0f} m")
def show_route(runs):
	st.pydeck_chart(viewer.route_map(route, NO_POIS, basemap, colored_paths=map_paths(runs)),
		height=580, width="stretch")
	st.altair_chart(colored_elevation_chart(runs), width="stretch")


plain, private, segmented = st.tabs(["Route", "Private roads", "Segments"])
with plain:
	show_route(colored_runs(route))
with private:
	st.caption("Roads whose smoothed_vei_id starts with P or S are highlighted.")
	show_route(colored_runs(route, private=True))
with segmented:
	segments = db.segments(race_id)
	if segments.empty:
		st.info("Generate and save segments to display them on the map.")
		st.page_link("pages/2_segmentation.py", label="Segmentation", icon=":material/segment:")
	else:
		st.caption("Segment risk is the highest Aggregate Risk in the full segment. Grey means unscored.")
		st.html(f'<div style="max-width:22rem">'
			f'<div style="height:.5rem;border-radius:.25rem;background:linear-gradient(to right, '
			f'{", ".join(RISK_COLOR_STOPS)})"></div>'
			'<div style="display:flex;justify-content:space-between"><span>0 (low)</span>'
			'<span>5 (high)</span></div></div>')
		show_route(colored_runs(route, segments=segments))
		with st.expander("Total risk by segment (full route)"):
			st.dataframe(segments[["segment", "segment_name", "total_risk"]], hide_index=True,
				column_config={"total_risk": st.column_config.NumberColumn("Total risk (0-5)", format="%.2f")})
