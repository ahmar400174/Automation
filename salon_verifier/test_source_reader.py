import tempfile
import unittest
import zipfile
from pathlib import Path

from salon_verifier import source_reader


class SourceReaderTests(unittest.TestCase):
    def test_csv_duplicate_headers_and_semicolon(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "example.csv"
            path.write_text("Company Name;Phone;Phone\nSalon One;111;222\n", encoding="utf-8")
            headers, rows, header_row = source_reader.read_csv(path)
        self.assertEqual(headers, ["Company Name", "Phone", "Phone (2)"])
        self.assertEqual(rows[0]["Phone (2)"], "222")
        self.assertEqual(header_row, 1)

    def test_sheet_without_header_keeps_first_data_row(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "no_header.csv"
            path.write_text("Salon One,2125864343\nSalon Two,2125864344\n", encoding="utf-8")
            headers, rows, header_row = source_reader.read_csv(path, -1)
        self.assertEqual(headers, ["Column 1", "Column 2"])
        self.assertEqual(header_row, 0)
        self.assertEqual(len(rows), 2)

    def test_excel_sheet_choice_header_detection_and_date(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "example.xlsx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Notes" sheetId="1" r:id="rId1"/><sheet name="Leads" sheetId="2" r:id="rId2"/></sheets></workbook>')
                archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
                archive.writestr("xl/sharedStrings.xml", '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>Company</t></si><si><t>Phone</t></si><si><t>Export title</t></si><si><t>Salon One</t></si></sst>')
                archive.writestr("xl/styles.xml", '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>')
                archive.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Notes</t></is></c></row></sheetData></worksheet>')
                archive.writestr("xl/worksheets/sheet2.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="s"><v>2</v></c></row><row r="2"><c r="A2" t="s"><v>0</v></c><c r="B2" t="s"><v>1</v></c><c r="C2" t="inlineStr"><is><t>Date</t></is></c></row><row r="3"><c r="A3" t="s"><v>3</v></c><c r="B3"><v>2125864343</v></c><c r="C3" s="1"><v>45969</v></c></row></sheetData></worksheet>')
            sheet, headers, rows, header_row = source_reader.read_excel(path, "Leads")
        self.assertEqual(sheet, "Leads")
        self.assertEqual(header_row, 2)
        self.assertEqual(headers, ["Company", "Phone", "Date"])
        self.assertEqual(rows[0]["Company"], "Salon One")
        self.assertRegex(rows[0]["Date"], r"^\d{4}-\d{2}-\d{2}$")


if __name__ == "__main__":
    unittest.main()
