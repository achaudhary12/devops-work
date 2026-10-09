#!/usr/bin/env python3
"""Convert a Finance mapping workbook (.xlsx) or CSV into the fixed-layout CSV
files loaded by SQL*Loader into the staging tables.

Sheets are recognised by their header row, not their name, so Finance can
call the tabs whatever they like and add instruction tabs:

  * a header with SEGMENT1 and SEGMENT2         -> COA_MAPPINGS rows
  * otherwise a header with TARGET<n> columns   -> MAPPING_SET_VALUES rows
  * anything else                               -> ignored

Headers are matched loosely ("Target 1", "TARGET_1", "target1" are the same).
MAPPING_SET_NAME comes from a column if there is one, else from the file name
prefix before a double underscore: "Balance Sheet Mapping__Oct26.xlsx".

Writes <out-dir>/values.csv and <out-dir>/coa.csv (header row always present)
and prints "VALUES_ROWS=<n> COA_ROWS=<n>" on success.

Exit codes: 0 ok, 2 the file is unusable (message on stderr), 1 unexpected error.
"""
import argparse
import csv
import datetime
import os
import re
import sys

MAX_TARGETS = 10
HEADER_SCAN_ROWS = 15

ALIASES = {
    "mapping_set_name": {"mappingsetname", "mappingset", "mappingname", "setname"},
    "segment1": {"segment1", "seg1"},
    "segment2": {"segment2", "seg2", "glaccount", "account"},
    "description": {"description", "desc", "accountdescription", "gldescription",
                    "segment2description", "accountdesc"},
}
TARGET_RE = re.compile(r"^(?:target|tgt)(\d+)$")

TARGET_COLS = [f"target{k}" for k in range(1, MAX_TARGETS + 1)]
VALUES_LAYOUT = ["batch_id", "line_no", "mapping_set_name"] + TARGET_COLS
COA_LAYOUT = ["batch_id", "line_no", "mapping_set_name", "segment1", "segment2",
              "description"] + TARGET_COLS


class InputError(Exception):
    """The input file cannot be used; message is shown to Finance."""


def norm_header(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def map_header(cells):
    """Return {column_index: field} for the recognised headers in a row."""
    mapping = {}
    for idx, cell in enumerate(cells):
        key = norm_header(cell)
        if not key:
            continue
        m = TARGET_RE.match(key)
        if m:
            n = int(m.group(1))
            if not 1 <= n <= MAX_TARGETS:
                raise InputError(f"column '{cell}': only TARGET1..TARGET{MAX_TARGETS} are supported")
            field = f"target{n}"
        else:
            field = next((f for f, names in ALIASES.items() if key in names), None)
        if field:
            if field in mapping.values():
                raise InputError(f"column '{cell}' appears twice")
            mapping[idx] = field
    return mapping


def cell_text(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).upper()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, datetime.datetime):
        return value.date().isoformat() if value.time() == datetime.time() else value.isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    text = str(value).replace(" ", " ")
    return " ".join(text.split())  # trims and folds newlines/tabs into single spaces


def classify(rows):
    """Find the header row; return (kind, header_line_no, header_map) or None."""
    for line_no, cells in rows[:HEADER_SCAN_ROWS]:
        mapping = map_header(cells)
        fields = set(mapping.values())
        if {"segment1", "segment2"} <= fields:
            return "coa", line_no, mapping
        if fields & set(TARGET_COLS):
            return "values", line_no, mapping
    return None


def default_set_name(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    return stem.split("__", 1)[0].strip() if "__" in stem else ""


def read_xlsx(path):
    try:
        import openpyxl
    except ImportError:
        raise InputError("python3 package 'openpyxl' is not installed; cannot read .xlsx "
                         "(pip install openpyxl, or send the sheets as CSV)")
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # corrupt / password protected / not really xlsx
        raise InputError(f"cannot open workbook: {exc}")
    sheets = []
    for ws in wb.worksheets:
        rows = [(i, list(r)) for i, r in enumerate(ws.iter_rows(values_only=True), start=1)]
        sheets.append((ws.title, rows))
    wb.close()
    return sheets


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = [(i, r) for i, r in enumerate(csv.reader(fh), start=1)]
    return [(os.path.basename(path), rows)]


def extract(sheets, set_name_default):
    out = {"values": [], "coa": []}
    for title, rows in sheets:
        found = classify(rows)
        if not found:
            print(f"info: sheet '{title}' has no recognised header - skipped", file=sys.stderr)
            continue
        kind, header_line, mapping = found
        if "mapping_set_name" not in mapping.values() and not set_name_default:
            raise InputError(f"sheet '{title}': no MAPPING_SET_NAME column and the file name has "
                             f"no 'Mapping Name__' prefix to take it from")
        for line_no, cells in rows:
            if line_no <= header_line:
                continue
            rec = {f: "" for f in COA_LAYOUT}
            for idx, field in mapping.items():
                if idx < len(cells):
                    rec[field] = cell_text(cells[idx])
            if not any(rec[f] for f in mapping.values()):
                continue  # blank line
            if "mapping_set_name" not in mapping.values():
                rec["mapping_set_name"] = set_name_default
            rec["line_no"] = line_no
            out[kind].append(rec)
        print(f"info: sheet '{title}' -> {kind} ({len(mapping)} columns recognised)", file=sys.stderr)
    return out


def write(path, layout, records, batch_id):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL, lineterminator="\n")
        w.writerow(layout)
        for rec in records:
            rec["batch_id"] = batch_id
            w.writerow([rec[c] for c in layout])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--batch-id", required=True, type=int)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--mapping-set-name", default="",
                    help="used when the sheet has no MAPPING_SET_NAME column")
    ap.add_argument("input")
    args = ap.parse_args(argv)

    try:
        ext = os.path.splitext(args.input)[1].lower()
        if ext in (".xlsx", ".xlsm"):
            sheets = read_xlsx(args.input)
        elif ext == ".csv":
            sheets = read_csv(args.input)
        else:
            raise InputError(f"unsupported file type '{ext}' - save the workbook as .xlsx")
        data = extract(sheets, args.mapping_set_name or default_set_name(args.input))
        if not data["values"] and not data["coa"]:
            raise InputError("no MAPPING_SET_VALUES or COA_MAPPINGS rows found "
                             "(check the header row: TARGET1.., SEGMENT1, SEGMENT2)")
    except InputError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    os.makedirs(args.out_dir, exist_ok=True)
    write(os.path.join(args.out_dir, "values.csv"), VALUES_LAYOUT, data["values"], args.batch_id)
    write(os.path.join(args.out_dir, "coa.csv"), COA_LAYOUT, data["coa"], args.batch_id)
    print(f"VALUES_ROWS={len(data['values'])} COA_ROWS={len(data['coa'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
