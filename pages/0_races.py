import streamlit as st

from ui import get_db

st.title("Races")
db = get_db()
races = db.races()

if not races.empty:
	options = races["race_id"].tolist()
	if st.session_state.get("selected_race_id") not in options:
		st.session_state["selected_race_id"] = options[0]
	labels = races.set_index("race_id")
	race_id = st.selectbox(
		"Active race", options, index=options.index(st.session_state["selected_race_id"]), key="race_picker",
		format_func=lambda value: labels.loc[value, "race_name"],
	)
	st.session_state["selected_race_id"] = race_id
	st.page_link("pages/0_race_info_and_data.py", label="Race info and Data", icon=":material/edit:")

st.subheader("Create race")
with st.form("create_race", clear_on_submit=True):
	name = st.text_input("Race name", key="create_name")
	if st.form_submit_button("Create race", icon=":material/add:"):
		try:
			st.session_state["pending_race_id"] = db.create_race(name)
			st.rerun()
		except ValueError as error:
			st.error(str(error))