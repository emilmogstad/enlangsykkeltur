# Dashboard storage and Supabase export

The app uses `data\races_v2.sqlite3`. On first use it copies data from
`data\races.sqlite3`, if present, in a single migration transaction. The original
database is opened read-only and remains available for rollback with the previous app
version. Restart the app after upgrading. If `RACE_APP_DB` is set, point it at the new
database, not the legacy file.

| Table | Stores |
| --- | --- |
| `racemaster` | Race details, upload metadata, and exact fitted peaks and valleys (including fractional route orders). SQLite also keeps the original GPX compressed for download and the fit coefficients. |
| `route_data` | Every GPX point, original lookup fields, smoothed road ID and donor point, fitted elevation, and a peak/valley marker on the nearest sample. |
| `route_segments` | Saved segment bounds and statistics; fylke, kommune and politidistrikt; maximum Aggregate Risk, the five component maxima, and the five component scores at the aggregate peak. A tie uses the first route point. |

The `contacts` table (kommune and fylke contacts, shared across races) is also exported,
with snake_case columns `contact_type`, `name`, `fylke`, `email`, `phone`, and `link`.
The municipality reference table stays local. Legacy point risk scores are not
copied into the new assessment, since they may use older formulas.

1. Enter race details, upload GPX, and finish road lookup.
2. Smooth the road data and fit the elevation profile.
3. On **Segmentation**, select **Generate and save segments**.
4. On **Risk Analysis > By segment**, select **Calculate and save segment risks**.
5. Download the package from **Supabase export**. Maps and the segment risk plot show
   the saved results without rerunning the segmentation.

Changing the GPX, rerunning lookup or smoothing, or refitting the profile invalidates
the saved segments and risks. Regenerating segments clears only the segment risks.
An unscored segment (including before risks are saved) has a blank total rather than
zero and appears grey on the segment map.

Run [the Supabase schema](./supabase_schema.sql) in an empty schema or a new project,
then import `racemaster.csv`, `route_data.csv`, and `route_segments.csv` in that order.
`contacts.csv` has no foreign keys; import it at any time, upserting on `contact_id`.
The schema does not replace old hosted tables or enable public access. Set explicit
RLS policies for authorized consumers. Use the `effective_route_roads` view for common
smoothed road fields or join the donor point on both race ID and route order for other
lookup fields. Keep coordinates and elevation from the destination point. To refresh
an imported race, replace its old rows rather than appending another copy.
