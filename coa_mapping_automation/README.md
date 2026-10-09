# COA mapping loader

This loader automates the Finance mapping upload. Finance drops the Excel workbook
into a folder. The job loads it into `MAPPING_SET`, `MAPPING_SET_VALUES` and
`COA_MAPPINGS`, then emails the result. Nobody writes INSERT statements by hand.

```
data/inbox/*.xlsx ─▶ xlsx_to_stage.py ─▶ SQL*Loader ─▶ XX_COA_MAP_STG_*  ─▶ xx_coa_map_loader_pkg ─▶ MAPPING_SET
   (Finance)        sheet + header       (staging)                          validate, then MERGE      MAPPING_SET_VALUES
                    detection                                                (all or nothing)           COA_MAPPINGS
                                                                                    │
                                     data/archive/  or  data/error/ + <file>.errors.csv + email ◀───┘
```

There are two ways to run it. Both use the same database package:

* **Option A, Unix script** (`bin/`): the scheduler runs it every N minutes. It is ready to use.
* **Option B, Oracle Integration Cloud**: see [`oic/OIC_DESIGN.md`](oic/OIC_DESIGN.md).

## What the load does

| Table | Matched on | Existing row | New row |
|---|---|---|---|
| `MAPPING_SET` | `MAPPING_SET_NAME` (case-insensitive, trimmed) | `MAPPING_SET_ID` reused, e.g. 100 stays 100 | created with `MAPPING_SET_S.NEXTVAL` (switch `CREATE_MAPPING_SETS`) |
| `MAPPING_SET_VALUES` | `MAPPING_SET_ID` + `TARGET1` | `MAPPING_SET_VALUE_ID` and `MAPPING_SET_ID` kept; `TARGET2..n` updated from Excel | inserted with `MAPPING_SET_VALUES_S.NEXTVAL` |
| `COA_MAPPINGS` | `MAPPING_SET_ID` + `SEGMENT1` + `SEGMENT2` | `MAP_ID` kept; `DESCRIPTION` and `TARGET1..n` updated | inserted with `COA_MAPPINGS_S.NEXTVAL`; only `SEGMENT1`/`SEGMENT2` are populated |

The load also follows these rules:

* **All or nothing.** If any row in a file fails validation, nothing from that file is
  loaded. The error report lists each problem with its sheet and Excel line number.
* **Blank cells keep the current value** (`OVERWRITE_BLANKS=N`). Set it to `Y` if a blank
  cell should clear the value instead.
* **Targets are validated** (`VALIDATE_TARGETS=Y`). Each `TARGETn` on a GL row must
  already exist in `MAPPING_SET_VALUES.TARGETn` for that mapping set, or be on the
  values tab of the same file.
* **Other checks:** required fields, duplicate keys within the file, values longer
  than the column (the length is read from the real table), a target column the table
  doesn't have, and a mapping-set name that matches two master rows.
* **Re-running a file is safe.** Matching rows are updated rather than duplicated.

## The input workbook

`samples/COA_Mapping_Template.xlsx` is the template to give Finance. Regenerate it with
`samples/make_sample_workbook.py`.

* **Tab names don't matter.** Each tab is identified by its header row, which can be in
  any of the first 15 rows. Tabs without a recognised header, such as an instructions
  tab, are skipped.
  * A tab with `SEGMENT1` and `SEGMENT2` headers loads into `COA_MAPPINGS`.
  * A tab with `TARGETn` headers but no segments loads into `MAPPING_SET_VALUES`.
* **Headers are matched loosely.** `Target 1`, `TARGET_1`, `target1` and `Tgt1` are all the same column.
  `Mapping Set Name`, `Segment 1`, `Segment 2`, `Description` / `Account Description`
  and `Target 1`..`Target 10` are recognised. Other columns are ignored.
* **The mapping-set name** comes from a `Mapping Set Name` column. If there is no such
  column, it is taken from the file name prefix before a double underscore:
  `Balance Sheet Mapping__Oct26.xlsx` gives "Balance Sheet Mapping".
* **One workbook can contain several mapping sets**, and either tab can be left out.
* **CSV files work too.** Each CSV is treated as one tab. Old `.xls` files are rejected
  with a message asking for `.xlsx`.
* **Format the segment columns as Text in Excel.** Otherwise Excel stores `01` as the
  number 1, and the leading zero is gone before the file reaches the loader.

## Installation (Option A)

1. **Database objects.** Install these in the schema that owns the three tables:
   ```sql
   @sql/02_staging_objects.sql
   @sql/03_xx_coa_map_loader_pkg.sql
   ```
   `sql/01_reference_tables.sql` is only for a sandbox. It shows the expected shape of
   the three tables. **Don't run it where they already exist.**

   The package adapts to the real tables at run time. Whichever `TARGET1..TARGET10`
   columns exist are loaded. `DESCRIPTION` and `COA_MAPPINGS.MAPPING_SET_ID` are
   optional (without the latter, the COA key is `SEGMENT1+SEGMENT2`). The audit columns
   `CREATED_BY`, `CREATION_DATE`, `LAST_UPDATED_BY` and `LAST_UPDATE_DATE` are filled
   if present (`'COA_MAP_LOADER'`, or `-1` for NUMBER columns). The table and sequence
   **names** are fixed. If yours differ, search and replace `mapping_set`,
   `mapping_set_values`, `coa_mappings` and their `_s` sequences in `03_...pkg.sql`.

2. **Unix host.** You need an Oracle client with `sqlplus` and `sqlldr`, Python 3, and
   `pip install openpyxl`. `mailx` is needed for email.
   ```bash
   sudo mkdir -p /opt/coa_mapping && sudo chown batchuser /opt/coa_mapping
   cp -r bin ctl config /opt/coa_mapping/
   cp /opt/coa_mapping/config/coa_loader.env.example /opt/coa_mapping/config/coa_loader.env
   chmod 600 /opt/coa_mapping/config/coa_loader.env      # then edit it
   ```

3. **Credentials.** Use a wallet so that no password sits in the config file:
   ```bash
   mkstore -wrl $TNS_ADMIN/wallet -create
   mkstore -wrl $TNS_ADMIN/wallet -createCredential COAMAP coa_owner
   # sqlnet.ora: WALLET_LOCATION=(SOURCE=(METHOD=FILE)(METHOD_DATA=(DIRECTORY=<that dir>)))
   #             SQLNET.WALLET_OVERRIDE=TRUE
   ```
   Then set `DB_CONNECT="/@COAMAP"`. The script never puts the connect string on a
   command line, so it never shows up in `ps`.

4. **Schedule.** Use cron or Control-M/Autosys. The script holds a lock, so overlapping
   runs exit straight away.
   ```
   */10 * * * * /opt/coa_mapping/bin/run_coa_mapping_load.sh >/dev/null 2>&1
   ```
   Exit code 0 means everything loaded (or there was nothing to do). Exit code 1 means
   at least one file failed.

5. **Give Finance write access** to `data/inbox` only. Use a Samba/NFS share, SFTP, or
   an MFT job that copies from SharePoint. A file is picked up once it has been unchanged
   for `FILE_MIN_AGE_MIN` minutes, so a half-copied upload is never read.

## Operations

| Task | How |
|---|---|
| Run now, or run one file | `bin/run_coa_mapping_load.sh` or `bin/run_coa_mapping_load.sh /path/file.xlsx` |
| Find out why a file failed | Read the email, or `data/error/<batch>_<file>.errors.csv`. For file or loader problems: `logs/coa_mapping_load_*.log` and `data/work/<batch>/sqlldr_*.log` |
| Fix and reload | Correct the workbook and drop it in the inbox again. Matching rows are updated, so there are no duplicates. |
| Audit history | `select * from xx_coa_map_batch order by batch_id desc;` |
| Look at the errors in SQL | `select * from xx_coa_map_errors_v where batch_id = :b;` |
| Re-process staged data after fixing it in SQL | `exec xx_coa_map_loader_pkg.process_batch(:b)` |
| Housekeeping | Automatic: logs and work directories older than `LOG_RETENTION_DAYS`, and staging rows older than `STAGING_RETENTION_DAYS` |

Sequence values can have gaps. `MERGE` draws a `NEXTVAL` for matched rows as well.
That is normal Oracle behaviour and harmless for surrogate keys.

## Tests

* `python3 -m pytest tests` runs the converter unit tests. No database is needed.
* For an end-to-end test in a sandbox, run `sql/01..03`, copy the sample workbook into
  the inbox, and run the script. During development this was done against Oracle 23ai
  Free (`gvenzl/oracle-free`). The checks covered: existing IDs kept, new sets created,
  blank cells preserving values, a re-run being idempotent, every validation error,
  CSV input, and an alternate table shape (5 targets, NUMBER audit columns, no
  `DESCRIPTION` or `MAPPING_SET_ID` on COA).

## Assumptions to confirm with Finance

1. `TARGET1` identifies a row in `MAPPING_SET_VALUES`. Sending the same `TARGET1` again
   updates `TARGET2..n` rather than adding a row. If the key should be something else,
   change the `on (...)` clause in `merge_values` and the duplicate check in `validate`.
2. A GL account (`SEGMENT1`+`SEGMENT2`) belongs to one mapping set. In other words,
   `COA_MAPPINGS` has `MAPPING_SET_ID`.
3. A whole file is rejected if any row is bad, rather than loading the good rows.
