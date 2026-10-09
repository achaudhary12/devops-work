# Option B: Oracle Integration Cloud (OIC) implementation

Use this design if the drop location is OIC's File Server or an SFTP server and you
don't want to run a Unix job. The OIC flow only moves files into the database. The
same `xx_coa_map_loader_pkg` runs all the validation and the inserts and updates,
so both options behave the same and produce the same error report.

```
SFTP / OIC File Server             OIC (scheduled integration)                       Oracle DB / ATP
  /coa_mapping/inbox   ──list──▶  for-each file                                      
                                   ├─ invoke create_batch(file)  ─────────────────▶ XX_COA_MAP_BATCH
                                   ├─ download + Stage File "Read in segments"       
                                   ├─ DB adapter: insert rows (batch_id, line_no) ─▶ XX_COA_MAP_STG_*
                                   ├─ invoke process_batch(batch_id, 'Y','N','Y') ─▶ MAPPING_SET / _VALUES / COA_MAPPINGS
                                   ├─ select status/message, XX_COA_MAP_ERRORS_V ◀─
                                   ├─ Notification action (success / error lines)
                                   └─ FTP move ─▶ /archive or /error
```

## The .xlsx limitation

OIC cannot parse `.xlsx` natively: Stage File reads delimited, fixed-length, XML and
JSON files only. Choose one of these:

1. **Finance saves each tab as CSV** (`<Mapping Name>__VALUES.csv`,
   `<Mapping Name>__COA.csv`). This is the simplest option. The header row must match the
   staging layout below.
2. **Keep .xlsx.** Run `bin/xlsx_to_stage.py` as an OCI Function (or on the
   connectivity-agent host) and call it from OIC over REST. The function returns
   `values.csv` and `coa.csv` in the same fixed layout the Unix path uses.
3. **Use the Unix option for conversion only.** A small cron job runs the converter and
   drops the two CSVs where OIC picks them up.

## Connections

| Connection | Adapter | Notes |
|---|---|---|
| `COA_SFTP` | FTP (SFTP) | Inbox, archive and error folders. Or use the OIC File Server. |
| `COA_DB` | Oracle Database (via connectivity agent) or Oracle ATP | Schema that owns the package. Grant only `EXECUTE` on the package plus `INSERT` on the two staging tables. |
| Notification | built-in | Distribution list held in a lookup `COA_MAP_CONFIG`. |

## Integration: `COA_MAPPING_LOAD` (Scheduled Orchestration)

Schedule it every 15 minutes, and allow only one instance at a time (*Schedule → Advanced → Allow only
one instance*). That way two runs never process the same file.

| # | Action | Detail |
|---|---|---|
| 1 | Assign | `inDir`, `archiveDir`, `errorDir`, flags from lookup `COA_MAP_CONFIG` (`VALIDATE_TARGETS`, `OVERWRITE_BLANKS`, `CREATE_MAPPING_SETS`). |
| 2 | FTP **List Files** | `inDir`, pattern `*.csv`, ordered by last modified. |
| 3 | **For-each** file | Scope with a fault handler (step 11). |
| 4 | DB **Invoke stored procedure** | Wrap `xx_coa_map_loader_pkg.create_batch(p_file_name)` in a one-line procedure with an OUT param if your adapter version cannot call functions. Store `batchId`. |
| 5 | FTP **Download** to stage | Download the file into the OIC staging area. |
| 6 | **Stage File → Read File in Segments** | Use a CSV schema matching the header (see below). Read 200-row segments to keep memory flat. |
| 7 | Map → DB **Insert** | Target `XX_COA_MAP_STG_VALUES` or `XX_COA_MAP_STG_COA` (choose by file name suffix `__VALUES` / `__COA`). Map `batch_id = batchId` and `line_no = position() + 1` (the Excel row). If the file has no set-name column, map `mapping_set_name` = substring-before(fileName, `__`). No trimming is needed, because the package trims. |
| 8 | DB **Invoke stored procedure** | `xx_coa_map_loader_pkg.process_batch(batchId, VALIDATE_TARGETS, OVERWRITE_BLANKS, CREATE_MAPPING_SETS)`. It never raises an exception. |
| 9 | DB **Run SQL** | `select status, message from xx_coa_map_batch where batch_id = #batchId` and `select sheet, line_no, mapping_set_name, row_key, error_msg from xx_coa_map_errors_v where batch_id = #batchId order by sheet, line_no`. |
| 10 | **Switch** on status | SUCCESS: FTP move the file to `archiveDir/<batchId>_<file>`, then send a success notification. ERROR: write the error rows as CSV (Stage File → Write), move the source file to `errorDir`, then send a notification with the error CSV attached. |
| 11 | Fault handler | Call `xx_coa_map_loader_pkg.fail_batch(batchId, $fault_message)`, move the file to `errorDir`, notify, and continue with the next file. |

### CSV schemas for Stage File

`__VALUES.csv`:
```
MAPPING_SET_NAME,TARGET1,TARGET2,TARGET3,TARGET4,TARGET5,TARGET6,TARGET7,TARGET8,TARGET9,TARGET10
```
`__COA.csv`:
```
MAPPING_SET_NAME,SEGMENT1,SEGMENT2,DESCRIPTION,TARGET1,TARGET2,TARGET3,TARGET4,TARGET5,TARGET6,TARGET7,TARGET8,TARGET9,TARGET10
```
Columns that are not used can be left out of the file, as long as you define the
schema with only the columns that are present.

### Ordering when target validation is on

When `VALIDATE_TARGETS=Y`, each COA target must already exist in
`MAPPING_SET_VALUES` or be in the same batch. With separate CSVs, process
`__VALUES` before `__COA`: sort the file list by name, or give the values file an
earlier timestamp. The Unix option has no ordering issue, because one workbook is one
batch.

## Comparing the two options

| | Unix script (Option A) | OIC (Option B) |
|---|---|---|
| Reads .xlsx directly | yes | no (CSV, or a converter function) |
| Infrastructure | Oracle client + Python 3 on a Linux host | OIC subscription, connectivity agent for an on-prem DB |
| Monitoring | log files, email, exit code for the scheduler | OIC activity stream and tracking by file name |
| Build effort | ready in this repo | about 1–2 days to build the integration in the OIC designer |
| Business rules | `xx_coa_map_loader_pkg` | `xx_coa_map_loader_pkg` (same code) |
