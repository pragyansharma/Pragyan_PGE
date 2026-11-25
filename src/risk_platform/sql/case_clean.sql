-- case_clean keeps one row per input row. A dropped row fails the build.
-- record_id, structure_number, and parcel_number are VARCHAR. 1018 and 010/072 are identifiers.
-- in_risk_population is a column on the row. This table is not filtered by it.
-- DuckDB list_filter. On Spark that is filter(array(...), x -> x is not null).

CREATE OR REPLACE TABLE case_typed AS
WITH source_text AS (
    SELECT
        source_row_id,
        load_id,
        source_file,
        NULLIF(trim(record_id), '') AS record_id,
        NULLIF(trim(record_type), '') AS record_type,
        NULLIF(trim(review_status), '') AS review_status,
        NULLIF(trim(created_date), '') AS created_date_raw,
        NULLIF(trim(support_type), '') AS support_type,
        NULLIF(trim(encroachment_type), '') AS encroachment_type,
        -- Text, so a leading zero or a mixed identifier is preserved.
        NULLIF(trim(structure_number), '') AS structure_number,
        NULLIF(trim(circuit), '') AS circuit,
        NULLIF(trim(latitude), '') AS latitude_raw,
        NULLIF(trim(longitude), '') AS longitude_raw,
        NULLIF(trim(parcel_number), '') AS parcel_number,
        NULLIF(trim(is_deleted), '') AS is_deleted_raw,
        NULLIF(trim(unmapped_source_columns), '') AS unmapped_source_columns
    FROM case_source
),
parsed AS (
    SELECT
        *,
        regexp_extract(created_date_raw, '^([0-9]{4}-[0-9]{2}-[0-9]{2})T', 1) AS created_civil_text,
        regexp_extract(created_date_raw, 'T([0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]+)', 1) AS created_time_text,
        regexp_extract(created_date_raw, '([+-][0-9]{2}:[0-9]{2})$', 1) AS source_utc_offset,
        TRY_CAST(latitude_raw AS DOUBLE) AS latitude,
        TRY_CAST(longitude_raw AS DOUBLE) AS longitude
    FROM source_text
)
SELECT
    source_row_id,
    load_id,
    source_file,
    record_id,
    record_type,
    review_status,
    created_date_raw,
    TRY_CAST(created_civil_text AS DATE) AS created_civil_date,
    -- UTC is the source clock adjusted by the offset on the timestamp.
    -- The population rule uses the civil date, not this value.
    CASE
        WHEN TRY_CAST(created_civil_text AS DATE) IS NULL
          OR created_time_text IS NULL
          OR source_utc_offset IS NULL
        THEN NULL
        ELSE
            (TRY_CAST(created_civil_text || ' ' || created_time_text AS TIMESTAMP))
            - (
                CASE WHEN substr(source_utc_offset, 1, 1) = '-' THEN -1 ELSE 1 END
                * (
                    CAST(substr(source_utc_offset, 2, 2) AS INTEGER) * INTERVAL 1 HOUR
                    + CAST(substr(source_utc_offset, 5, 2) AS INTEGER) * INTERVAL 1 MINUTE
                )
            )
    END AS created_at_utc,
    source_utc_offset,
    support_type,
    encroachment_type,
    structure_number,
    circuit,
    latitude_raw,
    longitude_raw,
    latitude,
    longitude,
    parcel_number,
    is_deleted_raw,
    CASE
        WHEN lower(is_deleted_raw) = 'true' THEN TRUE
        WHEN lower(is_deleted_raw) = 'false' THEN FALSE
        ELSE NULL
    END AS is_deleted,
    CASE review_status
        WHEN 'Not Started' THEN 'active'
        WHEN 'In Process' THEN 'active'
        WHEN 'On Hold' THEN 'active'
        WHEN 'Completed' THEN 'closed'
        WHEN 'Cancelled' THEN 'closed'
        WHEN 'Not Needed' THEN 'closed'
        ELSE 'other'
    END AS status_bucket,
    unmapped_source_columns,
    md5(concat_ws(
        '||',
        coalesce(record_id, ''),
        coalesce(record_type, ''),
        coalesce(review_status, ''),
        coalesce(created_date_raw, ''),
        coalesce(support_type, ''),
        coalesce(encroachment_type, ''),
        coalesce(structure_number, ''),
        coalesce(circuit, ''),
        coalesce(latitude_raw, ''),
        coalesce(longitude_raw, ''),
        coalesce(parcel_number, ''),
        coalesce(is_deleted_raw, '')
    )) AS record_fingerprint
FROM parsed;

CREATE OR REPLACE TABLE case_id_stats AS
SELECT
    record_id,
    COUNT(*) AS record_id_copies,
    COUNT(DISTINCT record_fingerprint) AS distinct_versions,
    MIN(source_row_id) AS first_source_row_id
FROM case_typed
WHERE record_id IS NOT NULL
  AND regexp_matches(record_id, '^CASE-[0-9]{6}$')
GROUP BY record_id;

CREATE OR REPLACE TABLE case_clean AS
WITH flagged AS (
    SELECT
        t.*,
        COALESCE(s.record_id_copies, 1) AS record_id_copies,
        COALESCE(s.distinct_versions, 1) AS distinct_versions,
        s.first_source_row_id,
        (
            t.latitude BETWEEN -90 AND 90
            AND t.longitude BETWEEN -180 AND 180
        ) AS coordinates_in_range,
        (
            t.latitude NOT BETWEEN -90 AND 90
            AND t.longitude BETWEEN -90 AND 90
            AND t.longitude BETWEEN -180 AND 180
        ) AS coordinates_appear_swapped,
        (
            t.latitude BETWEEN 32 AND 42.5
            AND t.longitude BETWEEN 114 AND 125
        ) AS longitude_missing_negative_sign
    FROM case_typed t
    LEFT JOIN case_id_stats s USING (record_id)
)
SELECT
    source_row_id,
    load_id,
    source_file,
    record_id,
    record_type,
    review_status,
    status_bucket,
    created_date_raw,
    created_civil_date,
    created_at_utc,
    source_utc_offset,
    support_type,
    encroachment_type,
    structure_number,
    circuit,
    latitude_raw,
    longitude_raw,
    latitude,
    longitude,
    -- coordinates_usable is what a spatial job may consume.
    -- A suspicious point stays in the numeric columns.
    (
        coordinates_in_range
        AND NOT coordinates_appear_swapped
        AND NOT longitude_missing_negative_sign
    ) AS coordinates_usable,
    parcel_number,
    is_deleted_raw,
    is_deleted,
    record_id_copies,
    distinct_versions,
    CASE
        WHEN record_id_copies = 1 THEN 'unique'
        WHEN distinct_versions = 1 AND source_row_id = first_source_row_id THEN 'exact_copy_keeper'
        WHEN distinct_versions = 1 THEN 'exact_copy'
        ELSE 'unresolved_conflict'
    END AS duplicate_resolution,
    (
        record_id_copies = 1
        OR (distinct_versions = 1 AND source_row_id = first_source_row_id)
    ) AS duplicate_keeper,
    (
        record_type IS NOT DISTINCT FROM 'Land Rights Support'
        AND support_type IS NOT DISTINCT FROM 'Encroachment Management'
        AND created_civil_date > DATE '2019-12-31'
    ) AS in_risk_population,
    list_filter(
        [
            CASE WHEN record_type IS DISTINCT FROM 'Land Rights Support'
                THEN 'Expected record_type ''Land Rights Support'' for the risk population.' END,
            CASE WHEN support_type IS DISTINCT FROM 'Encroachment Management'
                THEN 'Expected support_type ''Encroachment Management'' for the risk population.' END,
            CASE WHEN created_civil_date IS NULL OR created_civil_date <= DATE '2019-12-31'
                THEN 'Expected a created civil date after 2019-12-31 for the risk population. The cutoff uses the date written on the source timestamp, not a UTC conversion.' END
        ],
        lambda x: x IS NOT NULL
    ) AS population_exclusion_reasons,
    list_filter(
        [
            CASE WHEN record_id IS NULL OR NOT regexp_matches(record_id, '^CASE-[0-9]{6}$')
                THEN 'record_id_unusable' END,
            CASE WHEN created_date_raw IS NULL OR created_civil_date IS NULL OR created_at_utc IS NULL
                THEN 'created_date_unparseable' END,
            CASE WHEN is_deleted IS NULL
                THEN 'is_deleted_unparseable' END,
            CASE WHEN latitude_raw IS NOT NULL AND (latitude IS NULL OR latitude NOT BETWEEN -90 AND 90)
                THEN 'latitude_out_of_range' END,
            CASE WHEN longitude_raw IS NOT NULL AND (longitude IS NULL OR longitude NOT BETWEEN -180 AND 180)
                THEN 'longitude_out_of_range' END,
            CASE WHEN coordinates_appear_swapped THEN 'coordinates_appear_swapped' END,
            CASE WHEN longitude_missing_negative_sign THEN 'longitude_missing_negative_sign' END,
            CASE WHEN parcel_number LIKE '%,%' THEN 'multiple_parcel_numbers' END,
            CASE WHEN support_type IS NULL THEN 'support_type_missing' END,
            CASE WHEN encroachment_type IS NULL THEN 'encroachment_type_missing' END,
            CASE WHEN record_id_copies > 1 AND distinct_versions > 1 THEN 'duplicate_record_conflict' END,
            CASE WHEN record_id_copies > 1 AND distinct_versions = 1 THEN 'duplicate_record_exact' END
        ],
        lambda x: x IS NOT NULL
    ) AS dq_rule_ids,
    list_filter(
        [
            CASE WHEN record_id IS NULL OR NOT regexp_matches(record_id, '^CASE-[0-9]{6}$')
                THEN 'Expected record_id like CASE-000000, stored as text. The value was missing or did not match.' END,
            CASE WHEN created_date_raw IS NULL OR created_civil_date IS NULL OR created_at_utc IS NULL
                THEN 'Expected created_date as text with a UTC offset, for example 2024-03-13T10:51:00.000-07:00.' END,
            CASE WHEN is_deleted IS NULL
                THEN 'Expected is_deleted to be true or false.' END,
            CASE WHEN latitude_raw IS NOT NULL AND (latitude IS NULL OR latitude NOT BETWEEN -90 AND 90)
                THEN 'Expected latitude between -90 and 90. The source text was kept and was not swapped with longitude.' END,
            CASE WHEN longitude_raw IS NOT NULL AND (longitude IS NULL OR longitude NOT BETWEEN -180 AND 180)
                THEN 'Expected longitude between -180 and 180. The source text was kept.' END,
            CASE WHEN coordinates_appear_swapped
                THEN 'Expected latitude and longitude in that order. The values look swapped. They were not rewritten.' END,
            CASE WHEN longitude_missing_negative_sign
                THEN 'Expected a California longitude to be negative. The value looks like a missing minus sign and was not rewritten.' END,
            CASE WHEN parcel_number LIKE '%,%'
                THEN 'Expected one parcel number. Multiple parcels were kept in the original text and were not split in this table.' END,
            CASE WHEN support_type IS NULL
                THEN 'Expected a support_type. The source value was blank.' END,
            CASE WHEN encroachment_type IS NULL
                THEN 'Expected an encroachment type. The source value was blank. The row was not removed from the population for this alone.' END,
            CASE WHEN record_id_copies > 1 AND distinct_versions > 1
                THEN 'Expected one row per record_id, or exact copies. The copies disagree, so no survivor was chosen and every copy remains.' END,
            CASE WHEN record_id_copies > 1 AND distinct_versions = 1
                THEN 'Expected one row per record_id. The copies agree. Every copy remains, and the earliest source row is the keeper for a current view.' END
        ],
        lambda x: x IS NOT NULL
    ) AS dq_findings,
    record_fingerprint,
    unmapped_source_columns
FROM flagged;

-- case_clean_trusted and case_current may return fewer rows. case_clean may not.
CREATE OR REPLACE VIEW case_clean_trusted AS
SELECT *
FROM case_clean
WHERE len(list_filter(
    dq_rule_ids,
    lambda x: x IN ('record_id_unusable', 'created_date_unparseable', 'is_deleted_unparseable')
)) = 0;

CREATE OR REPLACE VIEW case_population AS
SELECT * FROM case_clean WHERE in_risk_population;

CREATE OR REPLACE VIEW case_current AS
SELECT * FROM case_clean WHERE duplicate_keeper;

CREATE OR REPLACE VIEW case_duplicate_review AS
SELECT * FROM case_clean WHERE record_id_copies > 1;

-- Build check. A returned row means a source row was dropped, added, or duplicated.
CREATE OR REPLACE VIEW assertion_case_clean_lossless AS
SELECT 'input_row_missing_from_clean' AS failure, s.source_row_id
FROM case_source s
ANTI JOIN case_clean c USING (source_row_id)
UNION ALL
SELECT 'clean_row_not_in_input', c.source_row_id
FROM case_clean c
ANTI JOIN case_source s USING (source_row_id)
UNION ALL
SELECT 'duplicate_source_row_id_in_clean', source_row_id
FROM case_clean
GROUP BY source_row_id
HAVING COUNT(*) > 1;
