BEGIN;

CREATE TABLE public.racemaster (
    race_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    race_name text NOT NULL CHECK (length(btrim(race_name)) > 0),
    race_date date,
    start_location text,
    finish_location text,
    organizer text,
    organizer_address text,
    organizer_email text,
    organizer_phone text,
    gpx_filename text,
    gpx_hash text,
    lookup_properties boolean NOT NULL DEFAULT false,
    route_status text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    elevation_extrema jsonb
);

CREATE TABLE public.route_data (
    race_id uuid NOT NULL REFERENCES public.racemaster(race_id) ON DELETE CASCADE,
    route_order bigint NOT NULL CHECK (route_order > 0),
    lat double precision NOT NULL CHECK (lat BETWEEN -90 AND 90),
    lon double precision NOT NULL CHECK (lon BETWEEN -180 AND 180),
    elevation_m double precision NOT NULL,
    path_id bigint NOT NULL,
    kortform text,
    kommunenr text,
    vei_id text,
    distance_km double precision,
    distance_cum_km double precision,
    elev_gain_cum_m double precision,
    elev_loss_cum_m double precision,
    matrikkelnummer text,
    veitype text,
    kommunename text,
    fylke text,
    politidistrikt text,
    jump_flag boolean,
    roadname text,
    nvdb_reference text,
    nvdb_lookup_status text,
    traffic_aadt double precision,
    traffic_long_vehicles_pct double precision,
    traffic_year bigint,
    speed_limit_kmh double precision,
    calculated_road_width_m double precision,
    lane_count bigint,
    functional_road_class bigint,
    cycle_lane_count bigint,
    tunnel_length_m double precision,
    tunnel_width_m double precision,
    bridge_length_m double precision,
    bridge_width_m double precision,
    tunnel_names text,
    bridge_names text,
    accident_dates text,
    traffic_count bigint,
    speed_limit_count bigint,
    speed_limit_object_ids text,
    tunnel_count bigint,
    bridge_count bigint,
    accident_count bigint,
    cattle_grid_count bigint,
    speed_bump_count bigint,
    speed_bump_object_ids text,
    property_lookup_status text,
    excluded boolean NOT NULL DEFAULT false,
    smoothed_vei_id text,
    smoothed_source_route_order bigint,
    fitted_elevation_m double precision NOT NULL,
    elevation_extremum text CHECK (elevation_extremum IN ('peak', 'valley', 'both')),
    PRIMARY KEY (race_id, route_order)
);

CREATE TABLE public.route_segments (
    race_id uuid NOT NULL REFERENCES public.racemaster(race_id) ON DELETE CASCADE,
    segment integer NOT NULL CHECK (segment > 0),
    segment_name text NOT NULL,
    "from" text,
    "to" text,
    start_route_order double precision NOT NULL,
    end_route_order double precision NOT NULL CHECK (end_route_order > start_route_order),
    start_distance_km double precision NOT NULL,
    end_distance_km double precision NOT NULL CHECK (end_distance_km >= start_distance_km),
    distance_km double precision NOT NULL,
    elevation_gain_m double precision,
    elevation_loss_m double precision,
    max_abs_gradient_pct double precision,
    direction text,
    average_gradient_pct double precision,
    strava_score double precision,
    strava_equivalent_category text,
    private_distance_km double precision,
    fylke text,
    kommunename text,
    politidistrikt text,
    roadname text,
    total_risk double precision CHECK (total_risk BETWEEN 0 AND 5),
    risk_peak_route_order bigint,
    max_traffic_risk double precision,
    max_environmental_risk double precision,
    max_crash_risk double precision,
    max_fatigue_risk double precision,
    max_remoteness_risk double precision,
    peak_traffic_risk double precision,
    peak_environmental_risk double precision,
    peak_crash_risk double precision,
    peak_fatigue_risk double precision,
    peak_remoteness_risk double precision,
    PRIMARY KEY (race_id, segment)
);

CREATE TABLE public.contacts (
    contact_id text PRIMARY KEY,
    contact_type text NOT NULL CHECK (contact_type IN ('Kommune', 'Fylke')),
    name text NOT NULL CHECK (length(btrim(name)) > 0),
    fylke text NOT NULL DEFAULT '',
    email text NOT NULL DEFAULT '',
    phone text NOT NULL DEFAULT '',
    link text NOT NULL DEFAULT ''
);

CREATE INDEX route_data_race_distance_idx ON public.route_data (race_id, distance_cum_km);
CREATE INDEX contacts_fylke_idx ON public.contacts (fylke, contact_type, name);

ALTER TABLE public.racemaster ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.route_data ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.route_segments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.contacts ENABLE ROW LEVEL SECURITY;

CREATE VIEW public.effective_route_roads WITH (security_invoker = true) AS
SELECT p.race_id, p.route_order,
       coalesce(p.smoothed_vei_id, p.vei_id) AS vei_id,
       CASE WHEN d.route_order IS NULL THEN p.roadname ELSE d.roadname END AS roadname,
       CASE WHEN d.route_order IS NULL THEN p.kommunenr ELSE d.kommunenr END AS kommunenr,
       CASE WHEN d.route_order IS NULL THEN p.kommunename ELSE d.kommunename END AS kommunename,
       CASE WHEN d.route_order IS NULL THEN p.fylke ELSE d.fylke END AS fylke,
       CASE WHEN d.route_order IS NULL THEN p.politidistrikt ELSE d.politidistrikt END AS politidistrikt,
       CASE WHEN d.route_order IS NULL THEN p.traffic_aadt ELSE d.traffic_aadt END AS traffic_aadt,
       CASE WHEN d.route_order IS NULL THEN p.traffic_long_vehicles_pct ELSE d.traffic_long_vehicles_pct END AS traffic_long_vehicles_pct,
       CASE WHEN d.route_order IS NULL THEN p.speed_limit_kmh ELSE d.speed_limit_kmh END AS speed_limit_kmh
FROM public.route_data p
LEFT JOIN public.route_data d
  ON d.race_id = p.race_id AND d.route_order = p.smoothed_source_route_order;

COMMENT ON TABLE public.racemaster IS 'Race metadata. Browser access requires explicitly configured RLS policies.';
COMMENT ON TABLE public.route_data IS 'All GPX points, original lookup fields, donor references for smoothed attributes, and fitted elevation.';
COMMENT ON COLUMN public.route_data.smoothed_source_route_order IS 'Join to route_data on the same race_id and this route_order to resolve effective road attributes; keep geometry from the destination row.';
COMMENT ON COLUMN public.racemaster.elevation_extrema IS 'Exact fractional route orders for fitted peaks and valleys. Route point indicators mark the nearest sample.';
COMMENT ON TABLE public.route_segments IS 'Saved segments, with component maxima and component scores at the point of maximum aggregate risk.';
COMMENT ON TABLE public.contacts IS 'Municipality (kommune) and county (fylke) contacts. Shared across races; match route data on name and fylke.';
COMMENT ON COLUMN public.contacts.contact_id IS 'Stable key: lower-case "<type>:<name>", e.g. kommune:bergen.';
COMMENT ON COLUMN public.contacts.email IS 'One or more addresses separated by semicolons.';

COMMIT;
