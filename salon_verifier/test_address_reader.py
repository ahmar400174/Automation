import unittest

from salon_verifier.address_reader import addresses
from salon_verifier import app


class AddressTests(unittest.TestCase):
    def test_visible_address_with_suite_and_locality(self):
        text = "Contact us\n123 Main Street\nSuite 4\nBoston, MA 02110\nHours Monday-Friday"
        self.assertEqual(addresses(text), ["123 Main Street Suite 4 Boston, MA 02110"])

    def test_map_query_and_encoded_place_path(self):
        links = [{"url": "https://www.google.com/maps?q=123+Main+St,+Boston,+MA+02110"}]
        self.assertEqual(addresses("", links), ["123 Main St, Boston, MA 02110"])
        links = [{"url": "https://www.google.com/maps/place/123+Main+St,+Boston,+MA+02110/"}]
        self.assertEqual(addresses("", links), ["123 Main St, Boston, MA 02110"])

    def test_ambiguous_branches_are_blank_and_city_selects_branch(self):
        site = {"pages": [{"url": "https://sample.example/contact", "text":
                          "10 Main St Boston MA 02110 | 20 Main St Cambridge MA 02139"}], "links": []}
        self.assertNotIn("companyaddress", app.website_fills({"Company name": "Sample Salon"}, site))
        fills = app.website_fills({"Company name": "Sample Salon", "city": "Boston"}, site)
        self.assertEqual(fills["companyaddress"][0], "10 Main St Boston MA 02110")

    def test_live_crawl_keeps_map_query_and_fills_address(self):
        def loader(url):
            return ("Contact us", "https://sample.example/", [
                ("https://maps.google.com/?q=10+Main+St,+Boston,+MA+02110", "Directions")], [], "")
        site = app.fetch_website("https://sample.example/", loader)
        fills = app.website_fills({"Company name": "Sample Salon"}, site)
        self.assertEqual(fills["companyaddress"], ("10 Main St, Boston, MA 02110", "https://sample.example/"))

    def test_phone_and_hours_are_not_addresses(self):
        self.assertEqual(addresses("Phone 212-555-0123 Open Monday 9-5"), [])

    def test_same_street_in_different_cities_is_ambiguous(self):
        site = {"pages": [{"url": "https://sample.example", "text":
                          "10 Main St Boston MA 02110 | 10 Main St Cambridge MA 02139"}], "links": []}
        self.assertNotIn("companyaddress", app.website_fills({}, site))


if __name__ == "__main__":
    unittest.main()
