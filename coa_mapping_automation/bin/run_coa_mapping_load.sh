#!/usr/bin/env bash
# =============================================================================
# run_coa_mapping_load.sh - end-to-end COA mapping load.
#
#   inbox/*.xlsx|*.csv
#     -> xlsx_to_stage.py          (sheet detection, header mapping, clean CSV)
#     -> SQL*Loader                (XX_COA_MAP_STG_VALUES / XX_COA_MAP_STG_COA)
#     -> xx_coa_map_loader_pkg     (validate, MAPPING_SET / _VALUES / COA_MAPPINGS)
#     -> archive/ or error/ + email with the error report
#
# Usage:
#   run_coa_mapping_load.sh              process every ready file in the inbox
#   run_coa_mapping_load.sh FILE...      process the given files only
#
# Exit status: 0 all files loaded (or nothing to do), 1 at least one failed.
# Schedule from cron, e.g.  */10 * * * *  /opt/coa_mapping/bin/run_coa_mapping_load.sh
# =============================================================================
set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
APP_DIR=$(dirname "$SCRIPT_DIR")
CONFIG_FILE=${COA_LOADER_CONFIG:-$APP_DIR/config/coa_loader.env}

if [[ ! -r $CONFIG_FILE ]]; then
  echo "config file not found: $CONFIG_FILE (copy config/coa_loader.env.example)" >&2
  exit 1
fi
# shellcheck source=/dev/null
source "$CONFIG_FILE"

: "${INBOX_DIR:?}" "${WORK_DIR:?}" "${ARCHIVE_DIR:?}" "${ERROR_DIR:?}" "${LOG_DIR:?}" "${DB_CONNECT:?}"
PYTHON_BIN=${PYTHON_BIN:-python3}
VALIDATE_TARGETS=${VALIDATE_TARGETS:-Y}
OVERWRITE_BLANKS=${OVERWRITE_BLANKS:-N}
CREATE_MAPPING_SETS=${CREATE_MAPPING_SETS:-Y}
FILE_MIN_AGE_MIN=${FILE_MIN_AGE_MIN:-1}
LOG_RETENTION_DAYS=${LOG_RETENTION_DAYS:-60}
STAGING_RETENTION_DAYS=${STAGING_RETENTION_DAYS:-90}

for f in VALIDATE_TARGETS OVERWRITE_BLANKS CREATE_MAPPING_SETS; do
  [[ ${!f} == Y || ${!f} == N ]] || { echo "$f must be Y or N" >&2; exit 1; }
done

mkdir -p "$INBOX_DIR" "$WORK_DIR" "$ARCHIVE_DIR" "$ERROR_DIR" "$LOG_DIR"
umask 077

RUN_TS=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$LOG_DIR/coa_mapping_load_$RUN_TS.log"

log() { printf '%s [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$2" | tee -a "$LOG_FILE" >&2; }

# ---- single instance --------------------------------------------------------
exec 9>"$WORK_DIR/.coa_mapping_load.lock"
if ! flock -n 9; then
  echo "another run is in progress - exiting" >&2
  exit 0
fi

# ---- database helpers -------------------------------------------------------
# Credentials go through stdin / a private parfile, never the command line.
run_sql() {
  {
    echo "connect $DB_CONNECT"
    echo "whenever sqlerror exit failure"
    echo "whenever oserror exit failure"
    echo "set heading off feedback off pagesize 0 verify off echo off trimspool on linesize 32767 serveroutput on"
    cat
    echo "exit"
  } | sqlplus -s -L /nolog
}

# Prints the value of the KEY=value line emitted by the SQL on stdin.
sql_value() {
  local key=$1 out
  out=$(run_sql) || { log ERROR "sqlplus failed: $out"; return 1; }
  out=$(grep -m1 "^$key=" <<<"$out") || { log ERROR "no $key in sqlplus output"; return 1; }
  echo "${out#*=}"
}

sql_quote() { printf "%s" "${1//\'/\'\'}"; }

load_stage() {   # load_stage <values|coa> <csv> <batch dir>
  local kind=$1 csv=$2 dir=$3 parfile rc
  parfile="$dir/sqlldr_$kind.par"
  printf 'userid=%s\n' "$DB_CONNECT" >"$parfile"
  sqlldr parfile="$parfile" control="$APP_DIR/ctl/stg_$kind.ctl" data="$csv" \
         log="$dir/sqlldr_$kind.log" bad="$dir/sqlldr_$kind.bad" silent=header,feedback >"$dir/sqlldr_$kind.out" 2>&1
  rc=$?
  rm -f "$parfile"
  return $rc
}

# ---- notification ------------------------------------------------------------
notify() {   # notify <subject> <body file> [attachment]
  [[ -n ${NOTIFY_TO:-} ]] || return 0
  command -v mailx >/dev/null || { log WARN "mailx not found - notification skipped"; return 0; }
  local subject="${MAIL_SUBJECT_PREFIX:-[COA Mapping Load]} $1" body=$2 attach=${3:-}
  if [[ -n $attach && -s $attach && -n ${MAIL_ATTACH_FLAG:-} ]]; then
    mailx -s "$subject" "$MAIL_ATTACH_FLAG" "$attach" "$NOTIFY_TO" <"$body"
  else
    mailx -s "$subject" "$NOTIFY_TO" <"$body"
  fi || log WARN "mail to $NOTIFY_TO failed"
}

# ---- one file ---------------------------------------------------------------
process_file() {
  local src=$1 name batch_id bdir claimed counts status message
  name=$(basename "$src")
  log INFO "---- $name ----"

  batch_id=$(sql_value BATCH_ID <<SQL
variable b number
exec :b := xx_coa_map_loader_pkg.create_batch('$(sql_quote "$name")')
select 'BATCH_ID=' || :b from dual;
SQL
) || { log ERROR "$name: could not open a batch - file left in inbox"; return 1; }

  bdir="$WORK_DIR/$batch_id"
  mkdir -p "$bdir"
  claimed="$bdir/$name"
  mv -- "$src" "$claimed" || { log ERROR "$name: cannot move into work dir"; return 1; }
  log INFO "$name: batch $batch_id"

  local fail_reason=""
  if ! counts=$("$PYTHON_BIN" "$SCRIPT_DIR/xlsx_to_stage.py" --batch-id "$batch_id" \
                --out-dir "$bdir" "$claimed" 2>"$bdir/convert.err"); then
    fail_reason="File could not be read: $(grep '^ERROR' "$bdir/convert.err" | head -1)"
  else
    log INFO "$name: $counts"
    local kind
    for kind in values coa; do
      if [[ $(wc -l <"$bdir/$kind.csv") -gt 1 ]] && ! load_stage "$kind" "$bdir/$kind.csv" "$bdir"; then
        fail_reason="SQL*Loader failed for the $kind sheet - see $bdir/sqlldr_$kind.log"
        break
      fi
    done
  fi
  cat "$bdir/convert.err" >>"$LOG_FILE" 2>/dev/null

  if [[ -n $fail_reason ]]; then
    run_sql >/dev/null <<SQL
exec xx_coa_map_loader_pkg.fail_batch($batch_id, '$(sql_quote "$fail_reason")')
SQL
    status=ERROR
    message=$fail_reason
  else
    status=$(sql_value BATCH_STATUS <<SQL
exec xx_coa_map_loader_pkg.process_batch($batch_id, '$VALIDATE_TARGETS', '$OVERWRITE_BLANKS', '$CREATE_MAPPING_SETS')
select 'BATCH_STATUS=' || status from xx_coa_map_batch where batch_id = $batch_id;
SQL
) || status=ERROR
    message=$(sql_value MESSAGE <<SQL
select 'MESSAGE=' || replace(message, chr(10), ' ') from xx_coa_map_batch where batch_id = $batch_id;
SQL
) || message="(could not read batch message)"
  fi
  log INFO "$name: $status - $message"

  local body="$bdir/mail.txt" report=""
  {
    echo "File      : $name"
    echo "Batch ID  : $batch_id"
    echo "Status    : $status"
    echo "Result    : $message"
    echo "Host      : $(hostname)"
  } >"$body"

  if [[ $status == SUCCESS ]]; then
    local dest
    dest="$ARCHIVE_DIR/$(date +%Y%m)"
    mkdir -p "$dest"
    mv -- "$claimed" "$dest/${batch_id}_$name"
    notify "SUCCESS - $name" "$body"
    return 0
  fi

  report="$ERROR_DIR/${batch_id}_${name%.*}.errors.csv"
  run_sql >"$report" <<SQL
set markup csv on quote on
set heading on
select sheet, line_no as excel_line, mapping_set_name, row_key, error_msg
  from xx_coa_map_errors_v
 where batch_id = $batch_id
 order by sheet, line_no;
SQL
  # heading-only output means there are no row errors to report
  [[ $(grep -c . "$report") -le 1 ]] && rm -f "$report"
  mv -- "$claimed" "$ERROR_DIR/${batch_id}_$name"
  if [[ -f $report ]]; then
    { echo; echo "First errors (full list attached / in $report):"; head -51 "$report"; } >>"$body"
  fi
  notify "FAILED - $name" "$body" "$report"
  return 1
}

# ---- main ---------------------------------------------------------------------
log INFO "run started (config $CONFIG_FILE)"

files=()
if (( $# > 0 )); then
  files=("$@")
else
  # oldest first; only files nobody has written to for FILE_MIN_AGE_MIN minutes
  while IFS= read -r -d '' entry; do
    files+=("${entry#* }")
  done < <(find "$INBOX_DIR" -maxdepth 1 -type f ! -name '.*' -mmin +"$FILE_MIN_AGE_MIN" \
             -printf '%T@ %p\0' | sort -z -n)
fi

failed=0
for f in "${files[@]}"; do
  case ${f,,} in
    *.xlsx|*.xlsm|*.csv) process_file "$f" || failed=$((failed + 1)) ;;
    *) log WARN "$(basename "$f"): not .xlsx/.csv - moved to error dir"
       mv -- "$f" "$ERROR_DIR/" ;;
  esac
done

if (( ${#files[@]} == 0 )); then
  log INFO "nothing to process"
fi

# housekeeping, once a day is plenty but it is cheap
find "$LOG_DIR" -name 'coa_mapping_load_*.log' -mtime +"$LOG_RETENTION_DAYS" -delete 2>/dev/null
find "$WORK_DIR" -mindepth 1 -maxdepth 1 -type d -mtime +"$LOG_RETENTION_DAYS" -exec rm -rf {} + 2>/dev/null
run_sql >/dev/null <<SQL || log WARN "staging purge failed"
exec xx_coa_map_loader_pkg.purge($STAGING_RETENTION_DAYS)
SQL

log INFO "run finished: ${#files[@]} file(s), $failed failed"
(( failed == 0 ))
