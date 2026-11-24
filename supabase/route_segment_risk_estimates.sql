-- Segment risk estimates calculated from route_data and route_segments. This view is the source of truth for
-- segment risk; the route-processing worker only uploads route points and segments.
-- Crash risk uses elevation sections rebuilt from route_data.elevation_extremum (fitted extrema stored at their
-- nearest GPX point).
-- Dropped first because create or replace view cannot insert or rename columns.
drop view if exists public.route_segment_risk_estimates;
create view public.route_segment_risk_estimates
with (security_invoker = true) as
with points as (
    select
        p.race_id,
        p.route_order,
        p.elevation_m::double precision as elevation_m,
        p.distance_cum_km::double precision as distance_cum_km,
        p.elevation_extremum,
        p.fitted_elevation_m::double precision as fitted_elevation_m,
        -- Smoothed points use their donor point's road attributes, as in the dashboard.
        (case when p.smoothed_source_route_order is null then p.traffic_aadt else d.traffic_aadt end)::double precision
            as traffic_aadt,
        (case when p.smoothed_source_route_order is null then p.traffic_long_vehicles_pct
              else d.traffic_long_vehicles_pct end)::double precision as traffic_long_vehicles_pct,
        (case when p.smoothed_source_route_order is null then p.speed_limit_kmh else d.speed_limit_kmh end)::double precision
            as speed_limit_kmh,
        (case when p.smoothed_source_route_order is null then p.functional_road_class
              else d.functional_road_class end)::double precision as functional_road_class
    from public.route_data p
    left join public.route_data d
        on d.race_id = p.race_id and d.route_order = p.smoothed_source_route_order
),
route_bounds as (
    select race_id, min(route_order) as first_order, max(route_order) as last_order
    from points
    group by race_id
),
section_points as (
    select
        p.*,
        sum(case when p.route_order = b.first_order
                   or (p.elevation_extremum is not null and p.route_order < b.last_order) then 1 else 0 end)
            over (partition by p.race_id order by p.route_order rows unbounded preceding) as elevation_section,
        -- A marked point belongs to the previous section when the fitted extremum lies just after it.
        case
            when p.route_order > b.first_order and p.route_order < b.last_order
                 and ((p.elevation_extremum = 'peak'
                       and lead(p.fitted_elevation_m) over route_points > lag(p.fitted_elevation_m) over route_points)
                      or (p.elevation_extremum = 'valley'
                          and lead(p.fitted_elevation_m) over route_points < lag(p.fitted_elevation_m) over route_points))
            then 1 else 0
        end as section_offset
    from points p
    join route_bounds b on b.race_id = p.race_id
    window route_points as (partition by p.race_id order by p.route_order)
),
section_edges as (
    select
        race_id,
        elevation_section,
        elevation_m,
        row_number() over (partition by race_id, elevation_section order by route_order) as first_rank,
        row_number() over (partition by race_id, elevation_section order by route_order desc) as last_rank
    from section_points
),
section_elevations as (
    select
        race_id,
        elevation_section,
        max(case when first_rank = 1 then elevation_m end) as start_elevation_m,
        max(case when last_rank = 1 then elevation_m end) as last_elevation_m
    from section_edges
    group by race_id, elevation_section
),
section_rises as (
    select
        race_id,
        elevation_section,
        coalesce(lead(start_elevation_m) over (partition by race_id order by elevation_section), last_elevation_m)
            - start_elevation_m as rise_m
    from section_elevations
),
included_points as (
    select
        p.race_id,
        p.route_order,
        p.elevation_m,
        p.distance_cum_km,
        case
            when s.rise_m >= 0 then 0
            when 100 * abs(s.rise_m) > 80000 then 5
            when 100 * abs(s.rise_m) > 64000 then 4
            when 100 * abs(s.rise_m) > 32000 then 3
            when 100 * abs(s.rise_m) > 16000 then 2
            when 100 * abs(s.rise_m) > 8000 then 1
            else 0
        end::double precision as crash_risk,
        case when p.traffic_aadt >= 0 and p.traffic_aadt < 'Infinity'::double precision
             then p.traffic_aadt end as traffic_aadt,
        case when p.traffic_long_vehicles_pct >= 0 and p.traffic_long_vehicles_pct < 'Infinity'::double precision
             then p.traffic_long_vehicles_pct end as traffic_long_vehicles_pct,
        case when p.speed_limit_kmh >= 0 and p.speed_limit_kmh < 'Infinity'::double precision
             then p.speed_limit_kmh end as speed_limit_kmh,
        case when p.functional_road_class between 0 and 9 then round(p.functional_road_class)::integer end
            as functional_road_class,
        first_value(p.distance_cum_km) over (partition by p.race_id order by p.route_order) as first_distance_km
    from section_points p
    join section_rises s
        on s.race_id = p.race_id and s.elevation_section = p.elevation_section - p.section_offset
),
-- Typical traffic per NVDB functional road class (0 = most important, 9 = least), from the medians of
-- measured points on the processed routes. Classes 7-9 (mostly private and forest roads) have no counts.
road_class_defaults (functional_road_class, traffic_aadt, traffic_long_vehicles_pct, speed_limit_kmh) as (
    values (0, 2500.0, 20.0, 80.0), (1, 1800.0, 14.0, 80.0), (2, 1300.0, 13.0, 80.0), (3, 900.0, 12.0, 80.0),
           (4, 400.0, 10.0, 80.0), (5, 350.0, 9.0, 80.0), (6, 200.0, 5.0, 50.0), (7, 50.0, 3.0, 50.0),
           (8, 25.0, 2.0, 50.0), (9, 10.0, 1.0, 50.0)
),
traffic_inputs as (
    -- Missing traffic inputs are estimated from the road class.
    select
        p.race_id,
        p.route_order,
        p.elevation_m,
        p.distance_cum_km,
        p.crash_risk,
        p.first_distance_km,
        coalesce(p.traffic_aadt, c.traffic_aadt) as traffic_aadt,
        coalesce(p.traffic_long_vehicles_pct, c.traffic_long_vehicles_pct) as traffic_long_vehicles_pct,
        coalesce(p.speed_limit_kmh, c.speed_limit_kmh) as speed_limit_kmh,
        c.functional_road_class is not null
            and (p.traffic_aadt is null or p.traffic_long_vehicles_pct is null or p.speed_limit_kmh is null)
            as traffic_estimated
    from included_points p
    left join road_class_defaults c on c.functional_road_class = p.functional_road_class
),
help_points as (
    -- Help is a point on a busy road: AADT (measured or class-estimated) of at least 1000.
    select
        *,
        max(case when traffic_aadt >= 1000 then distance_cum_km end)
            over (partition by race_id order by route_order rows unbounded preceding) as previous_help_km,
        min(case when traffic_aadt >= 1000 then distance_cum_km end)
            over (partition by race_id order by route_order rows between current row and unbounded following)
            as next_help_km
    from traffic_inputs
),
help_distances as (
    -- Distance along the route to the nearest busy road, behind or ahead; null if the route has none.
    select
        *,
        case
            when previous_help_km is null then next_help_km - distance_cum_km
            when next_help_km is null then distance_cum_km - previous_help_km
            else least(distance_cum_km - previous_help_km, next_help_km - distance_cum_km)
        end as help_km
    from help_points
),
point_risks as (
    select
        race_id,
        route_order,
        -- least/greatest ignore nulls in Postgres, so test missing traffic inputs explicitly.
        case
            when traffic_aadt is null or traffic_long_vehicles_pct is null or speed_limit_kmh is null then 0
            else least(5, 2 * (least(traffic_aadt / 10000, 5) + least(traffic_long_vehicles_pct / 20, 5)
                               + least(speed_limit_kmh / 80, 5)))
        end as traffic_risk,
        greatest(0, least(5, 5 * (elevation_m - 200) / 800)) as environmental_risk,
        crash_risk,
        greatest(0, least(5, 5 * (distance_cum_km - first_distance_km) / 300)) as fatigue_risk,
        -- Avstand til hjelp: 0 on a busy road, rising linearly to 5 at 20 km from one.
        coalesce(least(5, 5 * help_km / 20), 5) as remoteness_risk,
        help_km,
        traffic_estimated
    from help_distances
),
aggregate_risks as (
    select
        *,
        (traffic_risk + environmental_risk + crash_risk + fatigue_risk + remoteness_risk) / 5 as aggregate_risk
    from point_risks
),
segment_marks as (
    -- Shared boundary points belong to the next segment; the final segment includes the finish point.
    select race_id, start_route_order::double precision as position, 0 as mark_kind, segment, null::integer as route_order
    from public.route_segments
    union all
    select race_id, route_order::double precision, 1, null::integer, route_order
    from aggregate_risks
),
point_segments as (
    select
        race_id,
        route_order,
        mark_kind,
        max(segment) over (partition by race_id order by position, mark_kind rows unbounded preceding) as segment
    from segment_marks
),
segment_points as (
    select
        r.*,
        s.segment,
        row_number() over (partition by r.race_id, s.segment order by r.aggregate_risk desc, r.route_order) as peak_rank
    from aggregate_risks r
    join point_segments s on s.race_id = r.race_id and s.route_order = r.route_order and s.mark_kind = 1
),
segment_risks as (
    select
        race_id,
        segment,
        max(aggregate_risk) as total_risk,
        max(case when peak_rank = 1 then route_order end) as risk_peak_route_order,
        max(traffic_risk) as max_traffic_risk,
        max(environmental_risk) as max_environmental_risk,
        max(crash_risk) as max_crash_risk,
        max(fatigue_risk) as max_fatigue_risk,
        max(remoteness_risk) as max_remoteness_risk,
        max(case when peak_rank = 1 then traffic_risk end) as peak_traffic_risk,
        max(case when peak_rank = 1 then environmental_risk end) as peak_environmental_risk,
        max(case when peak_rank = 1 then crash_risk end) as peak_crash_risk,
        max(case when peak_rank = 1 then fatigue_risk end) as peak_fatigue_risk,
        max(case when peak_rank = 1 then remoteness_risk end) as peak_remoteness_risk,
        100.0 * avg(case when traffic_estimated then 1 else 0 end) as traffic_estimated_pct,
        max(help_km) as max_help_km
    from segment_points
    group by race_id, segment
),
segment_bounds as (
    select
        s.race_id,
        s.segment,
        s.segment_name,
        s.segment_desc,
        s."from",
        s."to",
        s.start_route_order::double precision as start_route_order,
        coalesce(lead(s.start_route_order) over (partition by s.race_id order by s.segment), b.last_order)
            ::double precision as end_route_order
    from public.route_segments s
    join route_bounds b on b.race_id = s.race_id
),
segment_measures as (
    -- Boundaries may be fractional route orders; interpolate along the GPX edge, as in the dashboard.
    select
        sb.*,
        st.distance_km as start_distance_km,
        en.distance_km as end_distance_km,
        en.elevation_m - st.elevation_m as rise_m
    from segment_bounds sb
    cross join lateral (
        select
            a.distance_cum_km + case when z.route_order = a.route_order then 0 else
                (z.distance_cum_km - a.distance_cum_km) * (sb.start_route_order - a.route_order)
                / (z.route_order - a.route_order) end as distance_km,
            a.elevation_m + case when z.route_order = a.route_order then 0 else
                (z.elevation_m - a.elevation_m) * (sb.start_route_order - a.route_order)
                / (z.route_order - a.route_order) end as elevation_m
        from (select route_order, distance_cum_km, elevation_m from points
              where race_id = sb.race_id and route_order <= sb.start_route_order
              order by route_order desc limit 1) a
        cross join (select route_order, distance_cum_km, elevation_m from points
              where race_id = sb.race_id and route_order >= sb.start_route_order
              order by route_order limit 1) z
    ) st
    cross join lateral (
        select
            a.distance_cum_km + case when z.route_order = a.route_order then 0 else
                (z.distance_cum_km - a.distance_cum_km) * (sb.end_route_order - a.route_order)
                / (z.route_order - a.route_order) end as distance_km,
            a.elevation_m + case when z.route_order = a.route_order then 0 else
                (z.elevation_m - a.elevation_m) * (sb.end_route_order - a.route_order)
                / (z.route_order - a.route_order) end as elevation_m
        from (select route_order, distance_cum_km, elevation_m from points
              where race_id = sb.race_id and route_order <= sb.end_route_order
              order by route_order desc limit 1) a
        cross join (select route_order, distance_cum_km, elevation_m from points
              where race_id = sb.race_id and route_order >= sb.end_route_order
              order by route_order limit 1) z
    ) en
)
select
    s.race_id,
    s.segment,
    s.segment_name,
    s.segment_desc,
    s."from",
    s."to",
    s.start_route_order,
    s.end_route_order,
    s.start_distance_km,
    s.end_distance_km,
    s.end_distance_km - s.start_distance_km as distance_km,
    case when s.rise_m > 0 then 'Uphill' when s.rise_m < 0 then 'Downhill' else 'Level' end as direction,
    case
        when 100 * abs(s.rise_m) > 80000 then 'HC'
        when 100 * abs(s.rise_m) > 64000 then 'Cat 1'
        when 100 * abs(s.rise_m) > 32000 then 'Cat 2'
        when 100 * abs(s.rise_m) > 16000 then 'Cat 3'
        when 100 * abs(s.rise_m) > 8000 then 'Cat 4'
        else 'Uncategorized'
    end as strava_equivalent_category,
    r.total_risk,
    r.risk_peak_route_order,
    r.max_traffic_risk,
    r.max_environmental_risk,
    r.max_crash_risk,
    r.max_fatigue_risk,
    r.max_remoteness_risk,
    r.peak_traffic_risk,
    r.peak_environmental_risk,
    r.peak_crash_risk,
    r.peak_fatigue_risk,
    r.peak_remoteness_risk,
    r.traffic_estimated_pct,
    r.max_help_km
from segment_measures s
left join segment_risks r on r.race_id = s.race_id and r.segment = s.segment;
