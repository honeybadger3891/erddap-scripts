import contextlib
import io
import json
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

import erddap_catalog
import erddap_search


SERVER = "https://example.test/erddap"


def table(names, rows):
    return json.dumps({"table": {"columnNames": names, "rows": rows}}).encode()


def index(rows):
    # Intentionally shuffled and extra columns: fixed-offset parsers fail.
    return table(["Dataset ID", "Summary", "Title"], [
        [dataset_id, "catalog summary", title] for dataset_id, title in rows
    ])


def metadata(value="A Lake Ontario survey", attribute="summary", variable="NC_GLOBAL"):
    return table(["Value", "Attribute Name", "Row Type", "Data Type", "Variable Name"], [
        ["", "", "variable", "float", "temperature"],
        [value, attribute, "attribute", "String", variable],
    ])


def http_error(url, code=404, body="Not Found"):
    return urllib.error.HTTPError(url, code, "test response", {}, io.BytesIO(body.encode()))


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.responses = {}
        self.sleep = patch("erddap_catalog.time.sleep").start()
        self.page_size = patch("erddap_catalog.PAGE_SIZE", 2).start()
        self.urlopen = patch("erddap_catalog.urllib.request.urlopen", side_effect=self.open).start()
        self.addCleanup(patch.stopall)

    def open(self, request, timeout):
        url = request.full_url
        self.calls.append(url)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(timeout, erddap_catalog.TIMEOUT_SECONDS)
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path.endswith("/info/index.json"):
            self.assertNotIn("searchFor", query)
        key = (parsed.path, int(query.get("page", [0])[0]))
        if key not in self.responses:
            raise AssertionError("unexpected request: " + url)
        response = self.responses[key]
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(response)

    def add_page(self, page, rows, endpoint="info"):
        self.responses[(f"/erddap/{endpoint}/index.json", page)] = index(rows)

    def add_metadata(self, dataset_id, body=None):
        self.responses[(f"/erddap/info/{urllib.parse.quote(dataset_id, safe='')}/index.json", 0)] = body or metadata()

    def audit(self):
        return erddap_catalog.audit_catalog(SERVER, "Lake Ontario", "Lake of America")

    def test_every_page_and_attribute_are_inspected_even_when_title_does_not_match(self):
        self.add_page(1, [("allDatasets", "Synthetic"), ("neutral", "Temperature")])
        self.add_page(2, [("second", "Weather")])
        self.add_metadata("neutral", metadata())
        self.add_metadata("second", metadata("lake ontario station", "long_name", "station"))
        result = self.audit()
        self.assertTrue(result["complete"])
        self.assertEqual(result["dataset_count"], 2)
        self.assertEqual(result["metadata_checked_count"], 2)
        self.assertEqual(len(result["matches"]), 2)
        self.assertEqual(result["matches"][0]["after"], "A Lake of America survey")
        self.assertEqual(result["matches"][1]["variable"], "station")
        self.assertEqual(result["matches"][1]["after"], "lake of america station")
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.sleep.call_count, 4)
        self.assertTrue(result["limitations"])

    def test_exact_full_last_page_accepts_only_specific_next_page_error(self):
        self.add_page(1, [("one", "One"), ("two", "Two")])
        self.responses[("/erddap/info/index.json", 2)] = http_error(SERVER, body='Error { code=404; message="Not Found: Resource not found: You requested results page=2, but the last page is 1. Please request a page from 1 through 1."; }')
        self.add_metadata("one")
        self.add_metadata("two")
        result = self.audit()
        self.assertTrue(result["complete"])
        self.assertEqual(result["dataset_count"], 2)

    def test_repeated_page_is_incomplete_and_stops(self):
        rows = [("one", "One"), ("two", "Two")]
        self.add_page(1, rows)
        self.add_page(2, rows)
        self.add_metadata("one")
        self.add_metadata("two")
        result = self.audit()
        self.assertFalse(result["complete"])
        self.assertEqual(result["dataset_count"], 2)
        self.assertIn("duplicate dataset ID", result["errors"][0]["message"])
        self.assertEqual(len(self.calls), 4)

    def test_metadata_failure_is_reported_and_other_datasets_continue(self):
        self.add_page(1, [("one", "One"), ("two", "Two")])
        self.add_page(2, [])
        self.add_metadata("one", http_error(SERVER, code=503))
        self.add_metadata("two")
        result = self.audit()
        self.assertFalse(result["complete"])
        self.assertEqual(result["metadata_checked_count"], 1)
        self.assertEqual(result["errors"][0]["dataset_id"], "one")
        self.assertEqual(result["matches"][0]["dataset_id"], "two")

    def test_arbitrary_404_does_not_mean_empty_catalog(self):
        self.responses[("/erddap/info/index.json", 1)] = http_error(SERVER, body="<html>Not Found</html>")
        result = self.audit()
        self.assertFalse(result["complete"])
        self.assertEqual(result["dataset_count"], 0)
        self.assertIn("HTTP 404", result["errors"][0]["message"])

    def test_html_404_quoting_terminal_text_is_still_a_failure(self):
        self.add_page(1, [("one", "One"), ("two", "Two")])
        self.responses[("/erddap/info/index.json", 2)] = http_error(SERVER, body='<html>You requested results page=2, but the last page is 1.</html>')
        self.add_metadata("one")
        self.add_metadata("two")
        self.assertFalse(self.audit()["complete"])

    def test_catalog_shrink_during_scan_is_incomplete(self):
        self.add_page(1, [("one", "One"), ("two", "Two")])
        self.add_page(2, [("three", "Three"), ("four", "Four")])
        self.responses[("/erddap/info/index.json", 3)] = http_error(SERVER, body='Error { code=404; message="Not Found: Resource not found: You requested results page=3, but the last page is 1. Please request a page from 1 through 1."; }')
        for dataset_id in ("one", "two", "three", "four"):
            self.add_metadata(dataset_id)
        self.assertFalse(self.audit()["complete"])

    def test_known_erddap_no_results_is_empty(self):
        self.responses[("/erddap/info/index.json", 1)] = http_error(SERVER, body='Error { code=404; message="Not Found: Resource not found: Your query produced no matching results. Check the spelling of the word(s) you searched for."; }')
        result = self.audit()
        self.assertTrue(result["complete"])
        self.assertEqual(result["datasets"], [])

    def test_html_in_success_response_is_incomplete(self):
        self.responses[("/erddap/info/index.json", 1)] = b"<html>Login required</html>"
        self.assertFalse(self.audit()["complete"])

    def test_wrong_column_name_is_not_silently_accepted(self):
        self.responses[("/erddap/info/index.json", 1)] = table(["datasetID", "Title"], [["one", "One"]])
        result = self.audit()
        self.assertFalse(result["complete"])
        self.assertIn("Dataset ID", result["errors"][0]["message"])

    def test_malformed_metadata_schema_is_incomplete(self):
        self.add_page(1, [("one", "One")])
        self.add_metadata("one", table(["Value"], [["Lake Ontario"]]))
        self.assertFalse(self.audit()["complete"])

    def test_malformed_row_width_is_incomplete(self):
        self.responses[("/erddap/info/index.json", 1)] = table(["Dataset ID", "Title"], [["one"]])
        self.assertFalse(self.audit()["complete"])

    def test_dataset_ids_are_quoted_and_catalog_urls_are_not_followed(self):
        self.add_page(1, [("strange/id?#", "A title")])
        self.add_metadata("strange/id?#")
        result = self.audit()
        self.assertTrue(result["complete"])
        self.assertIn("/info/strange%2Fid%3F%23/index.json", self.calls[1])

    def test_timeout_does_not_become_success(self):
        self.responses[("/erddap/info/index.json", 1)] = TimeoutError("timeout")
        self.assertFalse(self.audit()["complete"])

    def test_case_sensitive_option(self):
        self.add_page(1, [("one", "One")])
        self.add_metadata("one", metadata("lake ontario"))
        result = erddap_catalog.audit_catalog(SERVER, "Lake Ontario", "Lake of America", case_sensitive=True)
        self.assertTrue(result["complete"])
        self.assertEqual(result["matches"], [])

    def test_invalid_servers_fail_before_network_access(self):
        for server in ["file:///etc/passwd", "https://u:p@example.test/erddap", "example.test/erddap", "https://example.test/?x=1", "http://example.test:bad", "https://example.test/a\nb"]:
            with self.subTest(server=server), self.assertRaises(ValueError):
                erddap_catalog.audit_catalog(server, "Lake Ontario", "Lake of America")
        self.assertEqual(self.calls, [])

    def test_search_uses_all_json_pages(self):
        self.add_page(1, [("one", "One"), ("two", "Two")], endpoint="search")
        self.add_page(2, [("three", "Three")], endpoint="search")
        result = erddap_search.search("test", SERVER, "lake ontario")
        self.assertEqual(len(result), 3)
        for url in self.calls:
            self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["searchFor"], ["lake ontario"])

    def test_search_failure_returns_nonzero_exit(self):
        self.responses[("/erddap/search/index.json", 1)] = b"<html>Error</html>"
        with patch("sys.argv", ["erddap_search.py", "ontario", "--servers", SERVER]), contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(erddap_search.main(), 1)

    def test_search_known_no_results_is_success(self):
        self.responses[("/erddap/search/index.json", 1)] = http_error(SERVER, body='Error { code=404; message="Not Found: Resource not found: Your query produced no matching results. Check the spelling of the word(s) you searched for."; }')
        self.assertEqual(erddap_search.search("test", SERVER, "no match"), [])


if __name__ == "__main__":
    unittest.main()
