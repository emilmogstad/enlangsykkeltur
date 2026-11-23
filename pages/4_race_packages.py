import streamlit as st

from processing.data_handler import ROAD_COLUMNS
from processing import excel_bytes, filter_contacts, filter_route, fylke_options, grouped_road_list, route_frame
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
contacts = db.sync_contacts(race_id)
visible_contacts = filter_contacts(contacts, fylke)
export_route = filter_route(route, fylke)
st.download_button("Download included route data", excel_bytes(export_route[ROAD_COLUMNS], "Veikartreferanser"),
	file_name=f"route_included_{fylke or 'all_fylker'}.xlsx",
	mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", icon=":material/download:",
	on_click="ignore", disabled=export_route.empty)

st.subheader("Contacts")
if st.session_state.pop(f"contacts_saved_{race_id}", False):
	st.success("Contacts saved.")
missing_emails = visible_contacts["Epost"].fillna("").str.strip().eq("").sum()
if missing_emails:
	st.warning(f"{missing_emails} contacts have no email address.")
with st.form(f"contacts_form_{race_id}_{fylke}"):
	edited_contacts = st.data_editor(visible_contacts, hide_index=True, width="stretch",
		disabled=["contact_id", "Type", "Navn", "Fylke"], column_config={"contact_id": None},
		key=f"contacts_editor_{race_id}_{fylke}_{st.session_state.get('contacts_version', 0)}")
	if st.form_submit_button("Save contacts", icon=":material/save:"):
		try:
			db.save_contacts(edited_contacts)
			st.session_state["contacts_version"] = st.session_state.get("contacts_version", 0) + 1
			st.session_state[f"contacts_saved_{race_id}"] = True
			st.rerun()
		except ValueError as error:
			st.error(str(error))
st.download_button("Download contacts Excel", excel_bytes(visible_contacts.drop(columns="contact_id"), "Contacts"),
	file_name=f"contacts_{fylke or 'all_fylker'}.xlsx", icon=":material/download:", on_click="ignore")

st.subheader("Roads and municipalities")
municipalities = sorted({str(name).strip() for name in export_route["kommunename"].dropna()
	if str(name).strip()}, key=str.casefold)
sections = [fylke or "All fylker", "Kommuner langs traseen:\n" + (", ".join(municipalities) or "Ingen registrert")]
sections.extend(f"{heading}:\n" + (", ".join(roads) or "Ingen registrert")
	for heading, roads in grouped_road_list(export_route).items())
st.text_area("Road list and municipalities", value="\n\n".join(sections), height=380, disabled=True)