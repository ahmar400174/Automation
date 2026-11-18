import unittest
from unittest.mock import patch

from salon_verifier import app


class CrawlerTests(unittest.TestCase):
    def test_follows_relevant_pages_and_records_exact_evidence(self):
        pages = {
            "https://sample-salon.example/": (
                "Sample Salon", "https://sample-salon.example/",
                [("/contact", "Contact"), ("/gallery", "Gallery"),
                 ("https://booksy.com/sample", "Book Now")], [], ""),
            "https://sample-salon.example/contact": (
                "Call (212) 837-2774 or visit 223 Grand St New York",
                "https://sample-salon.example/contact", [], [], ""),
        }
        calls = []

        def loader(url):
            calls.append(url)
            return pages.get(url, ("", "", [], [], "unavailable"))

        site = app.fetch_website("https://sample-salon.example/", loader=loader)
        self.assertEqual(len(site["pages"]), 2)
        self.assertNotIn("https://sample-salon.example/gallery", calls)
        row = {"companyname": "Sample Salon", "Company Number": "(212) 837-2774",
               "Pain areas": "No online booking system"}
        self.assertEqual(app.evidence_for("Company Number", row["Company Number"], row, site)[0], "public_mention")
        self.assertEqual(app.evidence_for("Pain areas", row["Pain areas"], row, site)[0], "needs_review")

    def test_entry_date_is_not_claimed_as_publicly_verified(self):
        result = app.format_result("Date", "28/10/2026", {})
        self.assertEqual(result[0], "format_valid")
        self.assertIn("entry log", result[1])

    def test_structured_location_mismatch_is_flagged_with_page(self):
        site = {"pages": [], "links": [], "error": "", "structured": [{
            "url": "https://sample-salon.example/locations",
            "data": {"@type": "HairSalon", "name": "Sample Salon",
                     "address": {"streetAddress": "4340 E Indian School Rd",
                                 "addressLocality": "Phoenix", "postalCode": "85018"}},
        }]}
        row = {"companyname": "Sample Salon", "city": "Phoenix"}
        result = app.structured_evidence("Company Address", "4339 E Indian School Rd", row, site)
        self.assertEqual(result[0], "needs_review")
        self.assertEqual(result[2], "https://sample-salon.example/locations")
        self.assertEqual(app.structured_evidence("zipcode", "85018", row, site)[0], "structured_match")

    def test_fills_only_clear_missing_values_with_source(self):
        site = {"pages": [{"url": "https://sample.example/contact", "text": "Call us"}],
                "links": [], "contact_links": [
                    {"url": "mailto:hello@sample.example", "label": "Email", "page": "https://sample.example/contact"},
                    {"url": "tel:+12125550123", "label": "Call", "page": "https://sample.example/contact"}],
                "structured": [{"url": "https://sample.example/contact", "data": {
                    "@type": "HairSalon", "name": "Sample Salon",
                    "address": {"streetAddress": "10 Main St", "addressLocality": "Boston"}}}],
                "error": ""}
        row = {"Company Name": "Sample Salon", "Company Website": "https://sample.example",
               "Company Email": "Not Available", "Company Phone": "", "Company Address": "",
               "Owner Email": "", "Remarks": ""}
        fields = list(row)
        with patch.object(app, "FIELDS", fields):
            checks = app.verify_row(1, row, {}, site)
        by_field = {check[1]: check for check in checks}
        self.assertEqual(by_field["Company Email"][2:4], ("hello@sample.example", "website_filled"))
        self.assertEqual(by_field["Company Phone"][2:4], ("+12125550123", "website_filled"))
        self.assertEqual(by_field["Company Address"][2:4], ("10 Main St", "website_filled"))
        self.assertEqual(by_field["Company Address"][5], "https://sample.example/contact")
        self.assertEqual(by_field["Owner Email"][2:4], ("", "missing"))
        self.assertEqual(by_field["Remarks"][2:4], ("", "missing"))

    def test_ambiguous_contact_details_are_not_filled(self):
        site = {"pages": [], "links": [], "contact_links": [
            {"url": "mailto:first@sample.example", "label": "Email", "page": "https://sample.example"},
            {"url": "mailto:second@sample.example", "label": "Email", "page": "https://sample.example"}],
            "structured": [], "error": ""}
        self.assertNotIn("companyemail", app.website_fills({"Company name": "Sample Salon"}, site))


if __name__ == "__main__":
    unittest.main()
