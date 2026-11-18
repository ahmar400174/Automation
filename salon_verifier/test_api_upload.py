"""Optional end-to-end smoke test. Run while the dashboard is on port 8770."""

import io
import json
import urllib.request
import zipfile
from pathlib import Path

from salon_verifier import app


BASE = "http://127.0.0.1:8770"


def request(path, body=None, headers=None):
    call = urllib.request.Request(BASE + path, data=body, headers=headers or {},
                                  method="POST" if body is not None else "GET")
    return json.load(urllib.request.urlopen(call, timeout=15))


def post_json(path, value):
    return request(path, json.dumps(value).encode(), {"Content-Type": "application/json"})


def sample_workbook():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Notes" sheetId="1" r:id="rId1"/><sheet name="Leads" sheetId="2" r:id="rId2"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
        archive.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Notes</t></is></c></row></sheetData></worksheet>')
        archive.writestr("xl/worksheets/sheet2.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Lead export</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>Business Name</t></is></c><c r="B2" t="inlineStr"><is><t>Website</t></is></c></row><row r="3"><c r="A3" t="inlineStr"><is><t>Sample Salon</t></is></c><c r="B3" t="inlineStr"><is><t>https://example.com</t></is></c></row></sheetData></worksheet>')
    return output.getvalue()


def main():
    original = request("/api/files")["selected"]
    uploaded = None
    try:
        result = request("/api/upload", sample_workbook(),
                         {"Content-Type": "application/octet-stream", "X-Filename": "verification_smoke.xlsx"})
        uploaded = result["selected"]
        files = request("/api/files")
        chosen = next(item for item in files["files"] if item["token"] == uploaded)
        assert chosen["sheets"] == ["Notes", "Leads"]
        selected = post_json("/api/select", {"file": uploaded, "sheet": "Leads", "header_row": 0})
        assert selected["rows"] == 1 and selected["fields"] == ["Business Name", "Website"]
        summary = request("/api/summary")
        assert summary["sheet_name"] == "Leads" and summary["columns"][0]["role"] == "companyname"
        post_json("/api/run", {"online": False})
        import time
        for _ in range(30):
            summary = request("/api/summary")
            if summary["job"]["state"] != "running":
                break
            time.sleep(0.1)
        assert summary["reviewed"] == 1 and summary["fields"] == 2
        print("XLSX upload, sheet selection, header detection, mapping, and verification passed")
    finally:
        if original:
            post_json("/api/select", {"file": original, "header_row": 0})
            post_json("/api/run", {"online": False})
        if uploaded:
            (app.DATA_ROOT / uploaded).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
