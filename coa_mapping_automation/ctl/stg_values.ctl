-- SQL*Loader control file for the CSV written by bin/xlsx_to_stage.py.
-- The data file is passed on the command line (data=...).
-- ERRORS=0: the converter writes clean CSV, so any reject is a real failure.
OPTIONS (SKIP=1, ERRORS=0)
LOAD DATA
CHARACTERSET AL32UTF8
APPEND
INTO TABLE xx_coa_map_stg_values
FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"'
TRAILING NULLCOLS
(
  batch_id,
  line_no,
  mapping_set_name CHAR(4000),
  target1 CHAR(4000),
  target2 CHAR(4000),
  target3 CHAR(4000),
  target4 CHAR(4000),
  target5 CHAR(4000),
  target6 CHAR(4000),
  target7 CHAR(4000),
  target8 CHAR(4000),
  target9 CHAR(4000),
  target10 CHAR(4000)
)
