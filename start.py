"""Railway start command: Supabase route-processing worker.

Every 5 seconds, look for a racemaster row waiting in one of these route_status values and run the
next step (the same code as the dashboard, in a temporary SQLite database):

    not_ready    -> elevation fit, road lookup, upload route_data         -> lookup_done
    lookup_done  -> elevation fit, smoothing, replace route_data         -> smoothing
    smoothing    -> elevation fit and segmentation, replace
                    route_data and upload route_segments                 -> ready

The status shows the last finished step, so a race moves on until it is 'ready'. While a step runs the
status is 'processing'. To redo the later steps, set the status manually (e.g. 'lookup_done' keeps the
lookup and redoes smoothing and segmentation). A failed step sets 'failed' and keeps the data from earlier
steps; set the status back to the step to continue from. Segment risks are calculated by the Supabase view
route_segment_risk_estimates (supabase/route_segment_risk_estimates.sql).

Credentials: SUPABASE_URL and SUPABASE_KEY (secret/service_role key), from the environment or `.env`.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import pandas as pd  # noqa: E402
from postgrest.types import ReturnMethod  # noqa: E402

from processing.data_handler import POINT_COLUMNS, ROAD_COLUMNS, SEGMENT_COLUMNS, data_handler  # noqa: E402
from processing.elev_profile import (  # noqa: E402
    find_peaks_and_valleys, load_elevation_profile, load_saved_elevation_fit, select_cosine_bic,
)
from processing.processing import job_state, parse_gpx, route_frame, run_lookup  # noqa: E402
from processing.segmentation import suggest_segments  # noqa: E402
from processing.smoothing import smooth_race  # noqa: E402
from processing.supabase_reader import connect, count_rows, download_gpx, read_table  # noqa: E402

# Statuses the worker picks up; each names the last finished step.
PENDING_STATUSES = ("not_ready", "lookup_done", "smoothing")
POLL_SECONDS = 5
PROGRESS_SECONDS = 30
UPLOAD_CHUNK = 1000
# Needed by the local pipeline (lookup resume, NVDB details) but not stored in Supabase route_data.
LOCAL_ONLY_POINT_COLUMNS = {
    "property_lookup_status", "excluded", "speed_bump_object_ids", "speed_limit_object_ids", "speed_limit_count",
}
UPLOAD_POINT_COLUMNS = [c for c in POINT_COLUMNS if c not in LOCAL_ONLY_POINT_COLUMNS]
INTEGER_COLUMNS = {
    "route_order", "path_id", "traffic_year", "lane_count", "functional_road_class", "cycle_lane_count",
    "traffic_count", "tunnel_count", "bridge_count", "accident_count",
    "cattle_grid_count", "speed_bump_count", "smoothed_source_route_order", "segment",
}
BOOLEAN_COLUMNS = {"jump_flag"}
log = logging.getLogger("worker")


def now():
    return datetime.now(timezone.utc).isoformat()


def records(frame, race_id):
    """JSON-safe rows; convert in dictionaries so pandas cannot coerce None back to NaN."""
    rows = frame.astype(object).where(frame.notna(), None).to_dict("records")
    for row in rows:
        for column, value in row.items():
            convert = int if column in INTEGER_COLUMNS else bool if column in BOOLEAN_COLUMNS else None
            if convert and value is not None:
                row[column] = convert(value)
        row["race_id"] = race_id
    return rows


@contextmanager
def log_lookup_progress(db, race_id):
    """Log road lookup progress from a helper thread, since run_lookup blocks for hours on large routes."""
    stop = threading.Event()
    started = time.monotonic()

    def report():
        while not stop.wait(PROGRESS_SECONDS):
            state = job_state(db, race_id)
            if not state or not state["total"]:
                continue
            done, total = state["done"], state["total"]
            elapsed = time.monotonic() - started
            eta = f", ~{elapsed / done * (total - done) / 60:,.0f} min left" if done else ""
            log.info("Road lookup: %s/%s points (%.1f%%), %.0f min elapsed%s",
                     f"{done:,}", f"{total:,}", 100 * done / total, elapsed / 60, eta)

    thread = threading.Thread(target=report, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


def new_local_race(race, content):
    """Create a temporary SQLite race from the GPX; return (db, local_id, gpx_hash)."""
    folder = tempfile.mkdtemp()
    db = data_handler(Path(folder) / "race.sqlite3")
    local_id = db.create_race(race["race_name"], race.get("race_date"))
    digest = hashlib.sha256(content).hexdigest()
    db.save_upload(local_id, race.get("gpx_filename") or f"{race['race_id']}.gpx", content, digest,
                   parse_gpx(content))
    return db, local_id, digest


def restore_route(client, db, local_id, race_id, digest, smoothed):
    """Load the lookup (and smoothing) saved in Supabase route_data into the temporary database."""
    saved = read_table(client, "route_data", race_id)
    if saved.empty:
        raise ValueError("No route_data in Supabase to continue from. Set the status to not_ready.")
    route = saved.reindex(columns=ROAD_COLUMNS)
    # Local-only fields are not stored in Supabase; every point is included.
    route["excluded"] = False
    route["property_lookup_status"] = "skipped"
    db.save_route(local_id, route, digest)
    if smoothed:
        db.save_smoothed_vei_id(local_id, saved, revision=db.race(local_id)["revision"])


def local_points(db, local_id):
    with db.connection() as connection:
        points = pd.read_sql_query("SELECT * FROM route_data WHERE race_id=? ORDER BY route_order",
                                   connection, params=(local_id,))
    return points[UPLOAD_POINT_COLUMNS]


def fit_elevation(db, local_id, digest):
    log.info("Fitting elevation profile")
    profile = load_elevation_profile(local_id, db)
    fitted = select_cosine_bic(profile)
    coefficients = fitted.attrs["cosine_coefficients"]
    extrema = find_peaks_and_valleys(coefficients, float(profile["route_order"].min()),
                                     float(profile["route_order"].max()))
    db.save_elevation_fit(local_id, digest, coefficients.tolist(), fitted, extrema)


def run_stage(client, race, stage):
    """Run the step after `stage`; upload its result and return the new status."""
    race_id = race["race_id"]
    content, path = download_gpx(client, race_id)
    log.info("Downloaded %s (%s KB)", path, f"{len(content) / 1024:,.0f}")
    db, local_id, digest = new_local_race(race, content)
    try:
        # The fit only depends on the GPX; run it in every step so each route_data upload has it.
        fit_elevation(db, local_id, digest)
        if stage == "not_ready":
            clear_race(client, race_id)
            log.info("Road lookup: %s GPX points", f"{len(db.route_points(local_id)):,}")
            with log_lookup_progress(db, local_id):
                run_lookup(db, local_id)
            replace_race(client, race_id, local_points(db, local_id))
            set_status(client, race_id, "lookup_done", gpx_hash=digest)
            return "lookup_done"
        if race.get("gpx_hash") and race["gpx_hash"] != digest:
            raise ValueError("The GPX changed after the lookup. Set the status to not_ready.")
        restore_route(client, db, local_id, race_id, digest, smoothed=stage == "smoothing")
        if stage == "lookup_done":
            log.info("Smoothing roads")
            smooth_race(db, local_id)
            replace_race(client, race_id, local_points(db, local_id))
            set_status(client, race_id, "smoothing")
            return "smoothing"
        log.info("Segmentation")
        fitted, extrema, _ = load_saved_elevation_fit(db, local_id, digest)
        route = route_frame(db, local_id)
        db.save_segments(local_id, db.race(local_id)["revision"], suggest_segments(fitted, extrema, route))
        replace_race(client, race_id, local_points(db, local_id), db.segments(local_id)[SEGMENT_COLUMNS])
        set_status(client, race_id, "ready", gpx_hash=digest)
        return "ready"
    finally:
        shutil.rmtree(Path(db.db_name).parent, ignore_errors=True)


def clear_race(client, race_id):
    for table in ("route_segments", "route_data"):
        client.table(table).delete(returning=ReturnMethod.minimal).eq("race_id", race_id).execute()


def replace_race(client, race_id, points, segments=None):
    log.info("Uploading %s points%s", f"{len(points):,}", f" and {len(segments)} segments" if segments is not None else "")
    clear_race(client, race_id)
    upload(client, "route_data", records(points, race_id))
    if segments is not None:
        upload(client, "route_segments", records(segments, race_id))


def upload(client, table, rows):
    for start in range(0, len(rows), UPLOAD_CHUNK):
        client.table(table).insert(rows[start:start + UPLOAD_CHUNK], returning=ReturnMethod.minimal).execute()


def set_status(client, race_id, status, **fields):
    client.table("racemaster").update({"route_status": status, "updated_at": now(), **fields},
                                      returning=ReturnMethod.minimal).eq("race_id", race_id).execute()


def claim_next(client):
    """Mark the oldest waiting race as processing; return (race, status it was claimed from), or None."""
    waiting = (client.table("racemaster").select("race_id,route_status").in_("route_status", list(PENDING_STATUSES))
               .order("updated_at").limit(1).execute().data)
    if not waiting:
        return None
    stage = waiting[0]["route_status"]
    claimed = (client.table("racemaster").update({"route_status": "processing", "updated_at": now()})
               .eq("race_id", waiting[0]["race_id"]).eq("route_status", stage).execute().data)
    return (claimed[0], stage) if claimed else None


def handle(client, race, stage):
    started = time.monotonic()
    log.info("Processing %s (%s) from %s", race["race_name"], race["race_id"], stage)
    try:
        status = run_stage(client, race, stage)
        log.info("%s is %s after %.0f s", race["race_name"], status, time.monotonic() - started)
    except Exception:
        # Saved data from earlier steps is kept; set the status back to resume from it.
        log.exception("Processing %s from %s failed", race["race_name"], stage)
        try:
            set_status(client, race["race_id"], "failed")
        except Exception:
            log.exception("Could not mark %s as failed", race["race_name"])


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    os.chdir(APP_DIR)
    client = connect()
    # A race left in 'processing' was interrupted (e.g. by a redeploy). Uploaded route data means the
    # lookup finished, so resume after it; otherwise start again.
    stuck = client.table("racemaster").select("race_id,race_name").eq("route_status", "processing").execute().data
    for race in stuck:
        status = "lookup_done" if count_rows(client, "route_data", race["race_id"]) else "not_ready"
        set_status(client, race["race_id"], status)
        log.info("Re-queued interrupted race %s as %s", race["race_name"], status)
    log.info("Watching Supabase for %s races every %s s", ", ".join(PENDING_STATUSES), POLL_SECONDS)
    while True:
        try:
            claimed = claim_next(client)
        except Exception:
            log.exception("Polling Supabase failed")
            claimed = None
        if claimed:
            handle(client, *claimed)
            continue
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
