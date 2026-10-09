-- =============================================================================
-- 02_staging_objects.sql
--
-- Loader-owned objects: batch control, staging tables (filled by SQL*Loader or
-- OIC), and the error view used for the finance error report.
-- Safe to run in the target schema.
-- =============================================================================

create sequence xx_coa_map_batch_s nocache;

create table xx_coa_map_batch (
  batch_id          number         not null,
  file_name         varchar2(400),
  status            varchar2(20)   default 'NEW' not null,  -- NEW / RUNNING / SUCCESS / ERROR
  values_rows       number,
  coa_rows          number,
  error_rows        number,
  sets_created      number,
  values_inserted   number,
  values_updated    number,
  coa_inserted      number,
  coa_updated       number,
  message           varchar2(4000),
  created_on        timestamp      default systimestamp not null,
  started_on        timestamp,
  finished_on       timestamp,
  constraint xx_coa_map_batch_pk primary key (batch_id)
);

-- Columns are wide on purpose: the loader must never reject a row, so that
-- every problem is reported back with its Excel line number instead.
create table xx_coa_map_stg_values (
  batch_id          number         not null,
  line_no           number,
  mapping_set_name  varchar2(4000),
  target1   varchar2(4000),
  target2   varchar2(4000),
  target3   varchar2(4000),
  target4   varchar2(4000),
  target5   varchar2(4000),
  target6   varchar2(4000),
  target7   varchar2(4000),
  target8   varchar2(4000),
  target9   varchar2(4000),
  target10  varchar2(4000),
  mapping_set_id    number,
  status            varchar2(1)    default 'N',   -- N new / V valid / E error / P processed
  error_msg         varchar2(4000)
);

create index xx_coa_map_stg_values_n1 on xx_coa_map_stg_values (batch_id);

create table xx_coa_map_stg_coa (
  batch_id          number         not null,
  line_no           number,
  mapping_set_name  varchar2(4000),
  segment1          varchar2(4000),
  segment2          varchar2(4000),
  description       varchar2(4000),
  target1   varchar2(4000),
  target2   varchar2(4000),
  target3   varchar2(4000),
  target4   varchar2(4000),
  target5   varchar2(4000),
  target6   varchar2(4000),
  target7   varchar2(4000),
  target8   varchar2(4000),
  target9   varchar2(4000),
  target10  varchar2(4000),
  mapping_set_id    number,
  status            varchar2(1)    default 'N',
  error_msg         varchar2(4000)
);

create index xx_coa_map_stg_coa_n1 on xx_coa_map_stg_coa (batch_id);

create or replace view xx_coa_map_errors_v as
select batch_id,
       'MAPPING_SET_VALUES' as sheet,
       line_no,
       mapping_set_name,
       'TARGET1=' || target1 as row_key,
       error_msg
  from xx_coa_map_stg_values
 where status = 'E'
union all
select batch_id,
       'COA_MAPPINGS',
       line_no,
       mapping_set_name,
       'SEGMENT1=' || segment1 || ' SEGMENT2=' || segment2,
       error_msg
  from xx_coa_map_stg_coa
 where status = 'E';
