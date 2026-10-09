"""Unit tests for bin/xlsx_to_stage.py (no database needed): python3 -m pytest tests"""
import csv
import datetime
import importlib.util
import os

import openpyxl
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("x2s", os.path.join(HERE, "..", "bin", "xlsx_to_stage.py"))
x2s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(x2s)


def workbook(path, sheets):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for r in rows:
            ws.append(r)
    wb.save(path)
    return str(path)


def run(tmp_path, src, *extra):
    out = tmp_path / "out"
    rc = x2s.main(["--batch-id", "7", "--out-dir", str(out), *extra, str(src)])
    read = lambda n: list(csv.DictReader(open(out / n))) if (out / n).exists() else None
    return rc, read("values.csv"), read("coa.csv")


def test_sheets_detected_by_header_not_name(tmp_path):
    src = workbook(tmp_path / "f.xlsx", {
        "Read me": [["Some instructions"], ["more text"]],
        "Sheet7": [["Mapping Set Name", "Target 1", "TARGET_2"], ["BS Map", "BS100", "Cash"]],
        "Sheet9": [["Title row"], [],
                   ["mapping set name", "Segment 1", "SEGMENT-2", "Account Description", "tgt1"],
                   ["BS Map", "01", 110000, "Cash at bank", "BS100"],
                   [None, None, None, None, None],
                   ["BS Map", "01", 120000.0, "  Trade\nreceivables ", "BS100"]],
    })
    rc, values, coa = run(tmp_path, src)
    assert rc == 0
    assert values == [dict(dict.fromkeys(x2s.VALUES_LAYOUT, ""), batch_id="7", line_no="2",
                           mapping_set_name="BS Map", target1="BS100", target2="Cash")]
    assert [(r["line_no"], r["segment2"], r["description"]) for r in coa] == [
        ("4", "110000", "Cash at bank"), ("6", "120000", "Trade receivables")]


def test_set_name_from_file_name_prefix(tmp_path):
    src = tmp_path / "P&L Mapping__2026-10.csv"
    src.write_text("Target 1,Target 2\nPL100,Revenue\n", encoding="utf-8")
    rc, values, coa = run(tmp_path, src)
    assert rc == 0 and coa == []
    assert values[0]["mapping_set_name"] == "P&L Mapping"


def test_missing_set_name_is_rejected(tmp_path):
    src = tmp_path / "plain.csv"
    src.write_text("Target 1\nX\n")
    assert run(tmp_path, src)[0] == 2


@pytest.mark.parametrize("header, msg", [
    (["Target 1", "Target 11"], "TARGET10"),
    (["Target 1", "TARGET1"], "twice"),
])
def test_bad_headers(tmp_path, capsys, header, msg):
    src = workbook(tmp_path / "M__x.xlsx", {"s": [header, ["a", "b"]]})
    assert run(tmp_path, src)[0] == 2
    assert msg in capsys.readouterr().err


def test_no_recognised_sheet(tmp_path):
    src = workbook(tmp_path / "M__x.xlsx", {"s": [["foo", "bar"], [1, 2]]})
    assert run(tmp_path, src)[0] == 2


def test_unsupported_extension(tmp_path):
    src = tmp_path / "old.xls"
    src.write_bytes(b"\xd0\xcf")
    assert run(tmp_path, src)[0] == 2


@pytest.mark.parametrize("value, expected", [
    (None, ""), (1000, "1000"), (1000.0, "1000"), (12.5, "12.5"), (True, "TRUE"),
    (datetime.datetime(2026, 10, 9), "2026-10-09"), ("  A  B \n", "A B"),
])
def test_cell_text(value, expected):
    assert x2s.cell_text(value) == expected
