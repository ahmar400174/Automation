import csv
import io
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from salon_verifier import app


class UpdatedExportTests(unittest.TestCase):
    def test_download_contains_source_rows_and_checks_and_rejects_stale_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "leads.csv"
            source.write_text("Company Name,Company Email,Company Email\nSalon One,hello@example.com,backup@example.com\nSalon Two,bad-email,backup@example.com\nSalon Three,Not Available,backup@example.com\n", encoding="utf-8")
            patches = [
                patch.object(app, "SOURCE", source),
                patch.object(app, "DB", root / "checks.sqlite3"),
                patch.object(app, "REFERENCE", root / "reference.csv"),
                patch.object(app, "TABLE_CACHE", None),
                patch.object(app, "SELECTED_SHEET", None),
                patch.object(app, "HEADER_ROW", 0),
                patch.object(app, "FIELD_OVERRIDES", {}),
                patch.object(app, "FIELDS", ["Company Name", "Company Email", "Company Email (2)"]),
                patch.object(app, "choose_source", return_value=source),
            ]
            for item in patches:
                item.start()
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}/api/export-updated"
            try:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(url)
                self.assertEqual(error.exception.code, 409)

                app.run_verification(False)
                self.assertEqual(app.JOB["state"], "complete")
                with urllib.request.urlopen(url) as response:
                    self.assertIn("leads_updated.csv", response.headers["Content-Disposition"])
                    rows = list(csv.reader(io.StringIO(response.read().decode("utf-8-sig"))))
                self.assertEqual(rows[0][:3], ["Company Name", "Company Email", "Company Email"])
                self.assertEqual(rows[0][3:], ["system_verification_status", "system_verification_flags",
                                               "system_verification_checks", "system_verification_evidence",
                                               "system_verified_at"])
                self.assertTrue(all(len(row) == len(rows[0]) for row in rows))
                self.assertEqual(rows[1][:3], ["Salon One", "hello@example.com", "backup@example.com"])
                self.assertEqual(rows[1][3], "Checked")
                self.assertIn("Company Email: format_valid", rows[1][5])
                self.assertEqual(rows[2][3], "Review needed")
                self.assertIn("Company Email: invalid", rows[2][4])
                self.assertEqual(rows[3][1], "")
                self.assertIn("Company Email: missing", rows[3][4])

                source.write_text("Company Name,Company Email\nSalon Two,hello@example.com\n", encoding="utf-8")
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(url)
                self.assertEqual(error.exception.code, 409)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                for item in reversed(patches):
                    item.stop()


if __name__ == "__main__":
    unittest.main()
