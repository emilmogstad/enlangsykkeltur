"""Railway start command: Supabase route-processing worker.

Every 5 seconds, look for a racemaster row with route_status = 'not_ready'. For such a race:
clear its route_data and route_segments, set it to 'processing', download its GPX from Supabase
Storage, run road lookup, smoothing, elevation fit, segmentation and segment risks (the same code
as the dashboard, in a temporary SQLite database), upload the results and set it to 'ready'.
A failed run sets route_status = 'failed' and logs the error.

Credentials: SUPABASE_URL and SUPABASE_KEY (secret/service_role key), from the environment or `.env`.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import pandas as pd  # noqa: E402
from postgrest.types import ReturnMethod  # noqa: E402

from processing.data_handler import POINT_COLUMNS, SEGMENT_COLUMNS, data_handler  # noqa: E402
from processing.elev_profile import (  # noqa: E402
    find_peaks_and_valleys, load_elevation_profile, load_saved_elevation_fit, select_cosine_bic,
)
from processing.processing import job_state, parse_gpx, route_frame, run_lookup  # noqa: E402
from processing.risk_analysis import calculate_risks, summarize_segment_risks  # noqa: E402
from processing.segmentation import suggest_segments  # noqa: E402
from processing.smoothing import smooth_race  # noqa: E402
from processing.supabase_reader import connect, download_gpx  # noqa: E402

POLL_SECONDS = 5
PROGRESS_SECONDS = 30
UPLOAD_CHUNK = 1000
INTEGER_COLUMNS = {
    "route_order", "path_id", "traffic_year", "lane_count", "functional_road_class", "cycle_lane_count",
    "traffic_count", "speed_limit_count", "tunnel_count", "bridge_count", "accident_count",
    "cattle_grid_count", "speed_bump_count", "smoothed_source_route_order", "segment", "risk_peak_route_order",
}
BOOLEAN_COLUMNS = {"jump_flag", "excluded"}
log = logging.getLogger("worker")


def now():
    return datetime.now(timezone.utc).isoformat()


def records(frame, race_id):
    """JSON-safe rows: None for missing values, Python int/bool/float matching the Supabase column types."""
    rows = frame.astype(object).where(frame.notna(), None)
    for column in rows.columns:
        convert = int if column in INTEGER_COLUMNS else bool if column in BOOLEAN_COLUMNS else None
        if convert:
            rows[column] = [None if value is None else convert(value) for value in rows[column]]
    rows["race_id"] = race_id
    return rows.to_dict("records")


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


def process_gpx(race, content):
    """Run the full dashboard pipeline on one GPX; return (route_data, route_segments, gpx_hash)."""
    with tempfile.TemporaryDirectory() as folder:
        db = data_handler(Path(folder) / "race.sqlite3")
        local_id = db.create_race(race["race_name"], race.get("race_date"))
        digest = hashlib.sha256(content).hexdigest()
        db.save_upload(local_id, race.get("gpx_filename") or f"{race['race_id']}.gpx", content, digest,
                       parse_gpx(content))
        log.info("Road lookup: %s GPX points", f"{len(db.route_points(local_id)):,}")
        with log_lookup_progress(db, local_id):
            run_lookup(db, local_id)
        log.info("Road lookup finished")
        log.info("Smoothing roads")
        smooth_race(db, local_id)
        log.info("Fitting elevation profile")
        profile = load_elevation_profile(local_id, db)
        fitted = select_cosine_bic(profile)
        coefficients = fitted.attrs["cosine_coefficients"]
        extrema = find_peaks_and_valleys(coefficients, float(profile["route_order"].min()),
                                         float(profile["route_order"].max()))
        db.save_elevation_fit(local_id, digest, coefficients.tolist(), fitted, extrema)
        log.info("Segmentation and risk analysis")
        fitted, extrema, sections = load_saved_elevation_fit(db, local_id, digest)
        route = route_frame(db, local_id)
        db.save_segments(local_id, db.race(local_id)["revision"], suggest_segments(fitted, extrema, route))
        segments = db.segments(local_id)
        summary = summarize_segment_risks(segments, calculate_risks(route, sections))
        db.save_segment_risks(local_id, db.race(local_id)["revision"], summary.assign(segment=segments["segment"]))
        with db.connection() as connection:
            points = pd.read_sql_query("SELECT * FROM route_data WHERE race_id=? ORDER BY route_order",
                                       connection, params=(local_id,))
        return points[POINT_COLUMNS], db.segments(local_id)[SEGMENT_COLUMNS], digest


def clear_race(client, race_id):
    for table in ("route_segments", "route_data"):
        client.table(table).delete(returning=ReturnMethod.minimal).eq("race_id", race_id).execute()


def upload(client, table, rows):
    for start in range(0, len(rows), UPLOAD_CHUNK):
        client.table(table).insert(rows[start:start + UPLOAD_CHUNK], returning=ReturnMethod.minimal).execute()


def set_status(client, race_id, status, **fields):
    client.table("racemaster").update({"route_status": status, "updated_at": now(), **fields},
                                      returning=ReturnMethod.minimal).eq("race_id", race_id).execute()


def claim_next(client):
    """Mark the oldest not_ready race as processing; return it, or None if there is nothing to do."""
    waiting = (client.table("racemaster").select("race_id").eq("route_status", "not_ready")
               .order("updated_at").limit(1).execute().data)
    if not waiting:
        return None
    claimed = (client.table("racemaster").update({"route_status": "processing", "updated_at": now()})
               .eq("race_id", waiting[0]["race_id"]).eq("route_status", "not_ready").execute().data)
    return claimed[0] if claimed else None


def handle(client, race):
    race_id = race["race_id"]
    started = time.monotonic()
    log.info("Processing %s (%s)", race["race_name"], race_id)
    try:
        clear_race(client, race_id)
        content, path = download_gpx(client, race_id)
        log.info("Downloaded %s (%s KB)", path, f"{len(content) / 1024:,.0f}")
        points, segments, digest = process_gpx(race, content)
        log.info("Uploading %s points and %s segments", f"{len(points):,}", len(segments))
        upload(client, "route_data", records(points, race_id))
        upload(client, "route_segments", records(segments, race_id))
        set_status(client, race_id, "ready", gpx_hash=digest)
        log.info("Finished %s in %.0f s", race["race_name"], time.monotonic() - started)
    except Exception:
        log.exception("Processing %s failed", race["race_name"])
        try:
            clear_race(client, race_id)
            set_status(client, race_id, "failed")
        except Exception:
            log.exception("Could not mark %s as failed", race["race_name"])


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    os.chdir(APP_DIR)
    client = connect()
    # A race left in 'processing' was interrupted (e.g. by a redeploy); queue it again.
    stuck = client.table("racemaster").update({"route_status": "not_ready", "updated_at": now()}) \
        .eq("route_status", "processing").execute().data
    for race in stuck:
        log.info("Re-queued interrupted race %s", race["race_name"])
    log.info("Watching Supabase for not_ready races every %s s", POLL_SECONDS)
    while True:
        try:
            race = claim_next(client)
        except Exception:
            log.exception("Polling Supabase failed")
            race = None
        if race:
            handle(client, race)
            continue
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
