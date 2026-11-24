"""Read the Supabase race tables and GPX files in Supabase Storage.

Credentials come from SUPABASE_URL and SUPABASE_KEY, either as environment variables
(e.g. Railway) or from a git-ignored `.env` file in the `01 Dashboard` folder.

Run from `01 Dashboard`:
    python processing/supabase_reader.py                 # connection, table and GPX file report
    python processing/supabase_reader.py --race "Norseman 2019"
    python processing/supabase_reader.py --gpx "Norseman 2027"   # download the race's GPX file

In code:
    from processing.supabase_reader import connect, read_table, load_race, gpx_files, download_gpx
    client = connect()
    segments = read_table(client, "route_segments", race_id)
"""

import argparse
import base64
import json
import os
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import pandas as pd
from dotenv import load_dotenv
from postgrest.exceptions import APIError
from storage3.exceptions import StorageApiError
from supabase import Client, create_client

APP_DIR = Path(__file__).resolve().parent.parent
PAGE_SIZE = 1000
# Primary-key ordering keeps paging stable.
TABLES = {
    "racemaster": ["race_id"],
    "route_data": ["race_id", "route_order"],
    "route_segments": ["race_id", "segment"],
}
RACE_TABLES = ("route_data", "route_segments")


def credentials():
    load_dotenv(APP_DIR / ".env", override=False)
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    missing = [name for name, value in (("SUPABASE_URL", url), ("SUPABASE_KEY", key)) if not value]
    if missing:
        raise RuntimeError(f"Missing {' and '.join(missing)}. Set them as environment variables "
                           f"or in {APP_DIR / '.env'}.")
    return url, key


def key_role(key):
    if key.startswith("sb_secret_"):
        return "secret"
    if key.startswith("sb_publishable_"):
        return "publishable"
    try:
        payload = key.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("role", "unknown")
    except (IndexError, ValueError):
        return "unknown"


def connect() -> Client:
    return create_client(*credentials())


def _query(client, table, race_id=None, columns="*", **select):
    query = client.table(table).select(columns, **select)
    return query.eq("race_id", race_id) if race_id is not None else query


def count_rows(client, table, race_id=None):
    return _query(client, table, race_id, count="exact", head=True).execute().count


def read_table(client, table, race_id=None, columns="*"):
    rows = []
    while True:
        query = _query(client, table, race_id, columns)
        for column in TABLES.get(table, []):
            query = query.order(column)
        page = query.range(len(rows), len(rows) + PAGE_SIZE - 1).execute().data
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return pd.DataFrame(rows)


def find_race(client, race):
    races = read_table(client, "racemaster", columns="race_id,race_name")
    if races.empty:
        raise LookupError("No races are visible in Supabase.")
    names = races["race_name"].str.casefold()
    match = races.loc[races["race_id"].eq(race) | names.eq(race.casefold())]
    if match.empty:
        match = races.loc[names.str.contains(race.casefold(), regex=False)]
    if len(match) != 1:
        found = "matches several races" if len(match) else "not found"
        raise LookupError(f"Race {race!r} {found}. Visible races: {', '.join(races['race_name'])}")
    return match.iloc[0]["race_id"]


def storage_files(client, suffix=".gpx"):
    """Every file in every Storage bucket whose name ends with `suffix`."""
    files = []

    def walk(bucket, prefix):
        offset = 0
        while True:
            items = client.storage.from_(bucket).list(prefix, {"limit": PAGE_SIZE, "offset": offset})
            for item in items:
                path = f"{prefix}/{item['name']}" if prefix else item["name"]
                if item.get("id") is None:
                    walk(bucket, path)
                elif path.casefold().endswith(suffix):
                    files.append({"bucket": bucket, "path": path, "size_bytes": (item.get("metadata") or {}).get("size"),
                                  "updated_at": item.get("updated_at")})
            if len(items) < PAGE_SIZE:
                return
            offset += PAGE_SIZE

    for bucket in client.storage.list_buckets():
        walk(bucket.name, "")
    return pd.DataFrame(files, columns=["bucket", "path", "size_bytes", "updated_at"])


def gpx_files(client):
    """GPX files in Storage, matched to races by file name (race_id.gpx or racemaster.gpx_filename)."""
    files = storage_files(client)
    races = read_table(client, "racemaster", columns="race_id,race_name,gpx_filename")
    owners = {}
    for race in races.itertuples(index=False):
        owners.setdefault(f"{race.race_id}.gpx".casefold(), race)
        if race.gpx_filename:
            owners.setdefault(PurePosixPath(race.gpx_filename).name.casefold(), race)
    matched = [owners.get(PurePosixPath(path).name.casefold()) for path in files["path"]]
    files["race_id"] = [race.race_id if race else None for race in matched]
    files["race_name"] = [race.race_name if race else None for race in matched]
    return files


def download_gpx(client, race):
    """GPX bytes and storage path for a race name or race_id."""
    race_id = find_race(client, race)
    match = gpx_files(client).query("race_id == @race_id")
    if match.empty:
        raise LookupError(f"No GPX file in Storage for race {race!r}.")
    row = match.iloc[0]
    return client.storage.from_(row["bucket"]).download(row["path"]), row["path"]


def load_race(client, race_id):
    """Race row plus its route points and segments."""
    race = read_table(client, "racemaster").query("race_id == @race_id")
    return {"racemaster": race, **{table: read_table(client, table, race_id) for table in RACE_TABLES}}


def report(client, url, key):
    role = key_role(key)
    print(f"Project: {urlparse(url).hostname}  key: {role}")
    if role == "unknown":
        print(f"SUPABASE_KEY ({len(key)} characters) is not a Supabase API key. Copy the secret key (sb_secret_...) "
              "or legacy service_role key (eyJ...) from Project Settings > API Keys; not the database password.")
    counts = {}
    for table in TABLES:
        try:
            counts[table] = count_rows(client, table)
            print(f"  {table:<15} {counts[table]:>8,} rows")
        except APIError as error:
            print(f"  {table:<15} unavailable: {error.message}")
    if role in ("anon", "publishable") and not any(counts.values()):
        print("No rows are visible. RLS is enabled without policies; use a secret/service_role key "
              "or add SELECT policies.")
    if counts.get("racemaster"):
        races = read_table(client, "racemaster", columns="race_id,race_name,race_date,route_status")
        print("\nRaces:")
        for race in races.itertuples(index=False):
            points, segments = (count_rows(client, table, race.race_id) for table in RACE_TABLES)
            print(f"  {race.race_name} ({race.race_date or 'no date'}, {race.route_status}): "
                  f"{points:,} points, {segments} segments")
    try:
        files = gpx_files(client) if counts.get("racemaster") is not None else storage_files(client)
    except (StorageApiError, APIError) as error:
        print(f"\nGPX files: unavailable: {error.message}")
        return
    print(f"\nGPX files in Storage: {len(files)}")
    for file in files.itertuples(index=False):
        owner = getattr(file, "race_name", None) or "no matching race"
        print(f"  {file.bucket}/{file.path}  {(file.size_bytes or 0) / 1024:,.0f} KB  -> {owner}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--race", help="race name or race_id to load and summarize")
    parser.add_argument("--gpx", metavar="RACE", help="download the race's GPX file from Storage")
    parser.add_argument("--out", default=".", help="folder for --gpx downloads (default: current folder)")
    args = parser.parse_args()
    url, key = credentials()
    client = create_client(url, key)
    if args.gpx:
        data, path = download_gpx(client, args.gpx)
        target = Path(args.out) / PurePosixPath(path).name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        print(f"Downloaded {path} ({len(data) / 1024:,.0f} KB) to {target.resolve()}")
    elif args.race:
        data = load_race(client, find_race(client, args.race))
        for table, frame in data.items():
            print(f"{table}: {len(frame):,} rows x {frame.shape[1]} columns")
        segments = data["route_segments"]
        if not segments.empty:
            columns = [c for c in ("segment", "segment_name", "from", "to", "start_route_order")
                       if c in segments]
            print(segments[columns].to_string(index=False))
    else:
        report(client, url, key)


if __name__ == "__main__":
    main()
