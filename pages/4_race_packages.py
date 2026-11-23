import streamlit as st

from processing.data_handler import ROAD_COLUMNS
from processing import excel_bytes, filter_route, fylke_options, grouped_road_list, route_frame
from ui import context

st.title("Race Packages")
db, race_id, race = context()
st.caption(race["race_name"])
try:
	route = route_frame(db, race_id)
except (ValueError, KeyError) as error:
	st.info(str(error))
	st.stop()

fylke = st.selectbox("Fylke", [None, *fylke_options(route)],
	format_func=lambda value: "All fylker" if value is None else value, key=f"package_fylke_{race_id}")
export_route = filter_route(route, fylke)
st.download_button("Download included route data", excel_bytes(export_route[ROAD_COLUMNS], "Veikartreferanser"),
	file_name=f"route_included_{fylke or 'all_fylker'}.xlsx",
	mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", icon=":material/download:",
	on_click="ignore", disabled=export_route.empty)

st.subheader("Roads and municipalities")
municipalities = sorted({str(name).strip() for name in export_route["kommunename"].dropna()
	if str(name).strip()}, key=str.casefold)
sections = [fylke or "All fylker", "Kommuner langs traseen:\n" + (", ".join(municipalities) or "Ingen registrert")]
sections.extend(f"{heading}:\n" + (", ".join(roads) or "Ingen registrert")
	for heading, roads in grouped_road_list(export_route).items())
st.text_area("Road list and municipalities", value="\n\n".join(sections), height=380, disabled=True)