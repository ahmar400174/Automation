"""Read CSV and modern Excel worksheets with only the Python standard library."""

from __future__ import annotations

import csv
import io
import posixpath
import re
import zipfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET


MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
DATE_FORMATS = {14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47}


def file_kind(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix == ".csv":
        return "csv"
    if suffix in (".xlsx", ".xlsm"):
        return "excel"
    raise ValueError(f"Unsupported file type: {suffix}. Use CSV, XLSX, or XLSM.")


def workbook_sheets(path: Path) -> list[tuple[str, str]]:
    file_kind(path)
    if path.suffix.casefold() == ".csv":
        return [("CSV", "")]
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {item.attrib["Id"]: item.attrib["Target"] for item in relationships.findall(f"{{{PACKAGE_REL}}}Relationship")}
        sheets = []
        for item in workbook.findall(f".//{{{MAIN}}}sheet"):
            target = targets[item.attrib[f"{{{REL}}}id"]]
            member = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
            sheets.append((item.attrib["name"], member))
        return sheets


def unique_headers(values: list[str]) -> list[str]:
    seen = Counter()
    result = []
    for index, value in enumerate(values, 1):
        label = value.strip() or f"Column {index}"
        seen[label.casefold()] += 1
        count = seen[label.casefold()]
        result.append(label if count == 1 else f"{label} ({count})")
    return result


def select_header(rows: list[list[str]], header_row: int = 0) -> tuple[int, list[str]]:
    if not rows:
        return 0, []
    if header_row == -1:
        width = max(map(len, rows))
        return -1, [f"Column {index}" for index in range(1, width + 1)]
    if header_row:
        if header_row < 1 or header_row > len(rows):
            raise ValueError("Header row is outside the worksheet")
        index = header_row - 1
    else:
        index = next((i for i, row in enumerate(rows[:30]) if sum(bool(v.strip()) for v in row) >= 2),
                     next((i for i, row in enumerate(rows) if any(v.strip() for v in row)), 0))
    width = max((len(row) for row in rows[index:]), default=0)
    labels = rows[index] + [""] * (width - len(rows[index]))
    return index, unique_headers(labels)


def _csv_matrix(path: Path) -> list[list[str]]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return list(csv.reader(io.StringIO(text), dialect))


def original_csv_headers(path: Path, header_row: int = 0) -> list[str]:
    matrix = _csv_matrix(path)
    index, headers = select_header(matrix, header_row)
    if index < 0:
        return headers
    return (matrix[index] + [""] * (len(headers) - len(matrix[index])))[:len(headers)]


def read_csv(path: Path, header_row: int = 0) -> tuple[list[str], list[dict], int]:
    matrix = _csv_matrix(path)
    index, headers = select_header(matrix, header_row)
    rows = [dict(zip(headers, values + [""] * (len(headers) - len(values))))
            for values in matrix[index + 1:] if any(v.strip() for v in values)]
    return headers, rows, index + 1


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return ["".join(part.text or "" for part in node.findall(f".//{{{MAIN}}}t"))
            for node in root.findall(f"{{{MAIN}}}si")]


def _date_styles(archive: zipfile.ZipFile) -> set[int]:
    if "xl/styles.xml" not in archive.namelist():
        return set()
    root = ET.fromstring(archive.read("xl/styles.xml"))
    custom = {int(node.attrib["numFmtId"]): node.attrib.get("formatCode", "")
              for node in root.findall(f".//{{{MAIN}}}numFmt")}
    cell_xfs = root.find(f"{{{MAIN}}}cellXfs")
    if cell_xfs is None:
        return set()
    result = set()
    for index, node in enumerate(cell_xfs):
        fmt = int(node.attrib.get("numFmtId", "0"))
        code = re.sub(r'"[^"]*"|\\.', "", custom.get(fmt, "")).lower()
        if fmt in DATE_FORMATS or (fmt in custom and bool(re.search(r"[ymd]", code))):
            result.add(index)
    return result


def _cell_text(cell: ET.Element, strings: list[str], date_styles: set[int], date1904: bool) -> str:
    kind = cell.attrib.get("t", "")
    value = cell.find(f"{{{MAIN}}}v")
    raw = value.text if value is not None and value.text is not None else ""
    if kind == "inlineStr":
        inline = cell.find(f"{{{MAIN}}}is")
        return "".join(inline.itertext()) if inline is not None else ""
    if kind == "s":
        return strings[int(raw)] if raw else ""
    if kind == "b":
        return "TRUE" if raw == "1" else "FALSE"
    if kind == "e":
        return raw
    if raw and int(cell.attrib.get("s", "0")) in date_styles:
        try:
            origin = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)
            date = origin + timedelta(days=float(raw))
            return date.strftime("%Y-%m-%d %H:%M:%S") if date.time() != datetime.min.time() else date.date().isoformat()
        except ValueError:
            pass
    return raw


def read_excel(path: Path, sheet: str | None = None, header_row: int = 0) -> tuple[str, list[str], list[dict], int]:
    sheets = workbook_sheets(path)
    if not sheets:
        raise ValueError("Workbook has no worksheets")
    selected = next(((name, member) for name, member in sheets if name == sheet), None) if sheet else sheets[0]
    if selected is None:
        raise ValueError(f"Worksheet not found: {sheet}")
    with zipfile.ZipFile(path) as archive:
        strings = _shared_strings(archive)
        styles = _date_styles(archive)
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        properties = workbook.find(f"{{{MAIN}}}workbookPr")
        date1904 = properties is not None and properties.attrib.get("date1904") in ("1", "true")
        matrix = []
        with archive.open(selected[1]) as handle:
            for _, element in ET.iterparse(handle, events=("end",)):
                if element.tag != f"{{{MAIN}}}row":
                    continue
                cells = {}
                for cell in element.findall(f"{{{MAIN}}}c"):
                    address = cell.attrib.get("r", "")
                    letters = re.match(r"[A-Z]+", address)
                    if not letters:
                        continue
                    index = 0
                    for char in letters.group():
                        index = index * 26 + ord(char) - 64
                    cells[index - 1] = _cell_text(cell, strings, styles, date1904)
                width = max(cells, default=-1) + 1
                try:
                    row_number = int(element.attrib.get("r", str(len(matrix) + 1)))
                    while len(matrix) < row_number - 1:
                        matrix.append([])
                except ValueError:
                    pass
                matrix.append([cells.get(i, "") for i in range(width)])
                element.clear()
    index, headers = select_header(matrix, header_row)
    rows = [dict(zip(headers, values + [""] * (len(headers) - len(values))))
            for values in matrix[index + 1:] if any(v.strip() for v in values)]
    return selected[0], headers, rows, index + 1


def read_table(path: Path, sheet: str | None = None, header_row: int = 0) -> tuple[str, list[str], list[dict], int]:
    if file_kind(path) == "csv":
        headers, rows, actual = read_csv(path, header_row)
        return "CSV", headers, rows, actual
    return read_excel(path, sheet, header_row)
