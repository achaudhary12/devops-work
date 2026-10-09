-- =============================================================================
-- 01_reference_tables.sql
--
-- REFERENCE / DEV-TEST ONLY.  Your three business tables already exist in the
-- target schema - do NOT run this there.  It documents the shape the loader
-- expects and lets you stand up a sandbox to test the automation.
--
-- The loader adapts to the real tables at run time (it reads USER_TAB_COLUMNS):
--   * TARGET1..TARGET10    - whichever of these exist are loaded
--   * DESCRIPTION          - optional on COA_MAPPINGS
--   * MAPPING_SET_ID       - optional on COA_MAPPINGS (key becomes SEGMENT1+2)
--   * CREATED_BY / CREATION_DATE / LAST_UPDATED_BY / LAST_UPDATE_DATE
--                          - optional audit columns, VARCHAR2 or NUMBER
-- Table and sequence NAMES are fixed in the package; see README if yours differ.
-- =============================================================================

-- Master: one row per mapping, e.g. 100 / 'Balance Sheet Mapping'
create sequence mapping_set_s start with 100 nocache;

create table mapping_set (
  mapping_set_id    number         not null,
  mapping_set_name  varchar2(240)  not null,
  created_by        varchar2(64),
  creation_date     date,
  last_updated_by   varchar2(64),
  last_update_date  date,
  constraint mapping_set_pk primary key (mapping_set_id)
);

-- Target values per mapping set. Natural key = MAPPING_SET_ID + TARGET1.
create sequence mapping_set_values_s nocache;

create table mapping_set_values (
  mapping_set_value_id  number        not null,
  mapping_set_id        number        not null,
  target1   varchar2(240),
  target2   varchar2(240),
  target3   varchar2(240),
  target4   varchar2(240),
  target5   varchar2(240),
  target6   varchar2(240),
  target7   varchar2(240),
  target8   varchar2(240),
  target9   varchar2(240),
  target10  varchar2(240),
  created_by        varchar2(64),
  creation_date     date,
  last_updated_by   varchar2(64),
  last_update_date  date,
  constraint mapping_set_values_pk primary key (mapping_set_value_id),
  constraint mapping_set_values_fk foreign key (mapping_set_id) references mapping_set
);

create index mapping_set_values_n1 on mapping_set_values (mapping_set_id, target1);

-- GL accounts and their targets. Natural key = MAPPING_SET_ID + SEGMENT1 + SEGMENT2.
create sequence coa_mappings_s nocache;

create table coa_mappings (
  map_id          number        not null,
  mapping_set_id  number        not null,
  segment1  varchar2(25),
  segment2  varchar2(25),
  segment3  varchar2(25),
  segment4  varchar2(25),
  segment5  varchar2(25),
  segment6  varchar2(25),
  segment7  varchar2(25),
  segment8  varchar2(25),
  segment9  varchar2(25),
  segment10 varchar2(25),
  description  varchar2(240),
  target1   varchar2(240),
  target2   varchar2(240),
  target3   varchar2(240),
  target4   varchar2(240),
  target5   varchar2(240),
  target6   varchar2(240),
  target7   varchar2(240),
  target8   varchar2(240),
  target9   varchar2(240),
  target10  varchar2(240),
  created_by        varchar2(64),
  creation_date     date,
  last_updated_by   varchar2(64),
  last_update_date  date,
  constraint coa_mappings_pk primary key (map_id),
  constraint coa_mappings_fk foreign key (mapping_set_id) references mapping_set
);

create index coa_mappings_n1 on coa_mappings (mapping_set_id, segment1, segment2);
