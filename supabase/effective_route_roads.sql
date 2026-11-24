create or replace view public.effective_route_roads
with
  (security_invoker = true) as
select
  p.race_id,
  p.route_order,
  COALESCE(p.smoothed_vei_id, p.vei_id) as vei_id,
  case
    when d.route_order is null then p.roadname
    else d.roadname
  end as roadname,
  case
    when d.route_order is null then p.kommunenr
    else d.kommunenr
  end as kommunenr,
  -- Municipality and county names come from contacts by kommunenr; the looked-up names are a fallback.
  coalesce(
    m.kommunename,
    case
      when d.route_order is null then p.kommunename
      else d.kommunename
    end
  ) as kommunename,
  coalesce(
    m.fylke,
    case
      when d.route_order is null then p.fylke
      else d.fylke
    end
  ) as fylke,
  case
    when d.route_order is null then p.politidistrikt
    else d.politidistrikt
  end as politidistrikt,
  case
    when d.route_order is null then p.traffic_aadt
    else d.traffic_aadt
  end as traffic_aadt,
  case
    when d.route_order is null then p.traffic_long_vehicles_pct
    else d.traffic_long_vehicles_pct
  end as traffic_long_vehicles_pct,
  case
    when d.route_order is null then p.speed_limit_kmh
    else d.speed_limit_kmh
  end as speed_limit_kmh,
  case
    when d.route_order is null then p.functional_road_class
    else d.functional_road_class
  end as functional_road_class
from
  route_data p
  left join route_data d on d.race_id = p.race_id
  and d.route_order = p.smoothed_source_route_order
  left join (
    select distinct on (trim(municipality_id::text))
      trim(municipality_id::text) as kommunenr,
      name as kommunename,
      fylke
    from
      contacts
    where
      contact_type = 'Kommune'
      and municipality_id is not null
    order by
      trim(municipality_id::text),
      contact_id
  ) m on m.kommunenr = trim(
    (
      case
        when d.route_order is null then p.kommunenr
        else d.kommunenr
      end
    )::text
  );
