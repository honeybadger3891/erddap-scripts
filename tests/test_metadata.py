import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from erddap_metadata import PlanError, apply_edits, build_plan, replace_phrase


OLD, NEW = "Lake Ontario", "Lake of America"


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # macOS's default /var temp directory itself may be a symlink.
        self.directory = Path(self.temp.name).resolve()
        self.root = self.directory / "datasets.xml"

    def tearDown(self):
        self.temp.cleanup()

    def write(self, text, name="datasets.xml"):
        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
        return path

    def plan(self, **kwargs):
        return build_plan([str(self.root)], OLD, NEW, **kwargs)

    def output(self, plan, path=None):
        path = str(path or self.root)
        item = next(item for item in plan["files"] if item["path"] == path)
        return apply_edits(Path(path).read_bytes(), item["edits"])

    def test_only_selected_global_and_variable_strings_change(self):
        source = '''<erddapDatasets>
<!-- Lake Ontario -->
<dataset type="EDDTableFromFiles" datasetID="id_Lake Ontario">
<fileDir>/data/Lake Ontario/</fileDir>
<sourceAttributes><att name="summary">Lake Ontario</att></sourceAttributes>
<addAttributes>
<att name="title">Lake Ontario surveys</att>
<att name="history">Lake Ontario</att>
<att name="comment" type="int">Lake Ontario</att>
<att name="summary">Ontario; Lake OntarioEast; EastLake Ontario; Lake Ontario.</att>
</addAttributes>
<dataVariable><sourceName>Lake Ontario</sourceName><destinationName>Lake Ontario</destinationName>
<addAttributes><att name="long_name" type="String">LAKE ONTARIO temperatures</att></addAttributes>
</dataVariable></dataset></erddapDatasets>'''
        self.write(source)
        plan = self.plan()
        actual = self.output(plan).decode()
        expected = source.replace("Lake Ontario surveys", "Lake of America surveys").replace(
            "Lake Ontario.</att>", "Lake of America.</att>").replace("LAKE ONTARIO temperatures", "LAKE OF AMERICA temperatures")
        self.assertEqual(actual, expected)
        self.assertEqual(len(plan["changes"]), 3)
        self.assertEqual(plan["changes"][-1]["variable"], "Lake Ontario")
        reasons = {item["reason"] for item in plan["manual_review"]}
        self.assertTrue({"variable_identifier", "source_attribute_requires_override",
                         "attribute_not_selected", "non_string_attribute"} <= reasons)
        self.assertEqual(self.root.read_text(), source)

    def test_entities_cdata_unicode_crlf_and_unrelated_bytes_preserved(self):
        source = (b'\xef\xbb\xbf<?xml version="1.0" encoding="UTF-8"?>\r\n<erddapDatasets>\r\n'
                  b'<dataset datasetID="a" type="EDDTableFromFiles"><addAttributes>'
                  b'<att name="title" note="a&gt;b">Pr\xc3\xa9 Lake &#79;ntario &amp; lake ontario.</att>\r\n'
                  b'<att name="summary"><![CDATA[LAKE ONTARIO & text]]></att>'
                  b'</addAttributes></dataset></erddapDatasets>\r\n')
        self.write(source)
        plan = self.plan()
        self.assertEqual(self.output(plan), source.replace(b"Lake &#79;ntario", b"Lake of America").replace(
            b"lake ontario", b"lake of america").replace(b"LAKE ONTARIO", b"LAKE OF AMERICA"))
        self.assertEqual(plan, self.plan())
        self.assertEqual(plan["files"][0]["before_sha256"], hashlib.sha256(source).hexdigest())

    def test_cdata_terminator_and_xml_metacharacters_are_escaped(self):
        self.write('<erddapDatasets><dataset datasetID="a"><addAttributes>'
                   '<att name="title"><![CDATA[Lake Ontario]]></att>'
                   '<att name="summary">Lake Ontario</att></addAttributes></dataset></erddapDatasets>')
        plan = build_plan([str(self.root)], OLD, "Lake ]]>& America")
        actual = self.output(plan)
        self.assertIn(b"<![CDATA[Lake ]]]]><![CDATA[>& America]]>", actual)
        self.assertIn(b"Lake ]]&gt;&amp; America", actual)

    def test_shared_includes_nested_inventory_and_unchanged_dependencies(self):
        self.write('''<erddapDatasets xmlns:xi="http://www.w3.org/2001/XInclude">
<xi:include href="datasets/outer.xml"/><xi:include href="unchanged.xml"/>
<dataset datasetID="another"><dataVariable><sourceName>temp</sourceName><xi:include href="shared.xml"/></dataVariable></dataset>
</erddapDatasets>''')
        self.write('''<dataset datasetID="outer" type="EDDTableFromEDDGrid" xmlns:xi="http://www.w3.org/2001/XInclude">
<dataset datasetID="inner" type="EDDGridFromFiles"><dataVariable><sourceName>t</sourceName>
<xi:include href="../shared.xml"/></dataVariable></dataset></dataset>''', "datasets/outer.xml")
        shared = self.write('<addAttributes><att name="long_name">Lake Ontario temperature</att></addAttributes>', "shared.xml")
        self.write('<dataset datasetID="unchanged"><addAttributes><att name="title">Atlantic</att></addAttributes></dataset>', "unchanged.xml")
        plan = self.plan()
        self.assertEqual(len(plan["files"]), 4)
        self.assertEqual(len(plan["changes"]), 2)
        self.assertEqual(plan["reload_ids"], ["another", "outer"])
        self.assertEqual([item["dataset_id"] for item in plan["datasets"]], ["outer", "inner", "unchanged", "another"])
        shared_record = next(item for item in plan["files"] if item["path"] == str(shared))
        self.assertEqual(len(shared_record["edits"]), 1)
        self.assertIn(b"Lake of America", self.output(plan, shared))
        self.assertEqual(plan["datasets"][1]["parent_dataset_id"], "outer")

    def test_remote_datasets_and_shared_noneditable_context_block_updates(self):
        self.write('''<erddapDatasets xmlns:xi="http://www.w3.org/2001/XInclude">
<dataset datasetID="remote" type="EDDTableFromErddap"><xi:include href="shared.xml"/></dataset>
<dataset datasetID="local" type="EDDTableFromFiles"><xi:include href="shared.xml"/></dataset>
<dataset datasetID="grid" type="EDDGridFromErddap"><addAttributes><att name="summary">Lake Ontario</att></addAttributes></dataset>
</erddapDatasets>''')
        self.write('<addAttributes><att name="title">Lake Ontario</att></addAttributes>', "shared.xml")
        plan = self.plan()
        self.assertFalse(plan["changes"])
        self.assertFalse(plan["reload_ids"])
        self.assertEqual({item["reason"] for item in plan["manual_review"]},
                         {"upstream_required", "shared_include_has_noneditable_context"})

    def test_urls_and_paths_in_metadata_preserved_and_reported(self):
        self.write('''<erddapDatasets><dataset datasetID="a"><addAttributes>
<att name="summary">Lake Ontario; https://example.test/Lake Ontario; /data/Lake Ontario; Lake Ontario.</att>
</addAttributes></dataset></erddapDatasets>''')
        plan = self.plan()
        self.assertEqual(plan["changes"][0]["after"], "Lake of America; https://example.test/Lake Ontario; /data/Lake Ontario; Lake of America.")
        self.assertEqual(plan["manual_review"][0]["reason"], "url_or_path_reference")

    def test_comments_and_cdata_boundaries_are_not_removed(self):
        self.write('''<erddapDatasets><dataset datasetID="a"><addAttributes>
<att name="title">Lake <!--keep-->Ontario</att>
<att name="summary">Lake <![CDATA[Ontario]]></att>
<att name="comment">Lake Ontario<!--Lake Ontario--></att>
</addAttributes></dataset></erddapDatasets>''')
        plan = self.plan()
        output = self.output(plan)
        self.assertIn(b"Lake <!--keep-->Ontario", output)
        self.assertIn(b"Lake <![CDATA[Ontario]]>", output)
        self.assertIn(b"Lake of America<!--Lake Ontario-->", output)
        self.assertEqual(len(plan["changes"]), 1)
        self.assertEqual(len(plan["manual_review"]), 2)

    def test_allowlist_extension(self):
        self.write('<erddapDatasets><dataset datasetID="a"><addAttributes><att name="history">Lake Ontario</att></addAttributes></dataset></erddapDatasets>')
        self.assertFalse(self.plan()["changes"])
        plan = self.plan(attributes=["history"])
        self.assertEqual(len(plan["changes"]), 1)
        self.assertEqual(plan, self.plan(attributes=plan["attributes"]))
        for attr in ["sourceName", "sourceUrl", "filePath", "*", ""]:
            with self.subTest(attr=attr), self.assertRaises(PlanError):
                self.plan(attributes=[attr])

    def test_inactive_dataset_is_planned_but_not_reloaded(self):
        self.write('<erddapDatasets><dataset datasetID="inactive" active="false"><addAttributes>'
                   '<att name="title">Lake Ontario</att></addAttributes></dataset></erddapDatasets>')
        plan = self.plan()
        self.assertEqual(len(plan["changes"]), 1)
        self.assertFalse(plan["datasets"][0]["active"])
        self.assertFalse(plan["reload_ids"])

    def test_add_attributes_in_unrecognized_variable_context_are_not_edited(self):
        self.write('<erddapDatasets><dataset datasetID="a"><unexpected><dataVariable><addAttributes>'
                   '<att name="title">Lake Ontario</att></addAttributes></dataVariable></unexpected></dataset></erddapDatasets>')
        plan = self.plan()
        self.assertFalse(plan["changes"])
        self.assertEqual(plan["manual_review"][0]["reason"], "outside_metadata_addAttributes")

    def test_case_sensitive_and_phrase_boundaries(self):
        self.assertEqual(replace_phrase("Lake Ontario LAKE ONTARIO lake ontario lAkE oNtArIo", OLD, NEW),
                         "Lake of America LAKE OF AMERICA lake of america Lake of America")
        self.assertEqual(replace_phrase("Lake Ontario LAKE ONTARIO lake ontario", OLD, NEW, True),
                         "Lake of America LAKE ONTARIO lake ontario")
        self.assertEqual(replace_phrase("Lake Ontario2 EastLake Ontario Lake Ontario's", OLD, NEW),
                         "Lake Ontario2 EastLake Ontario Lake of America's")

    def test_rejects_unsafe_and_unsupported_xml(self):
        examples = [
            '<!DOCTYPE erddapDatasets [<!ENTITY x "Lake Ontario">]><erddapDatasets/>',
            '<!DOCTYPE erddapDatasets SYSTEM "https://example.test/x"><erddapDatasets/>',
            '<?xml version="1.0" encoding="ISO-8859-1"?><erddapDatasets/>',
            '<?xml version="1.1"?><erddapDatasets/>',
            '<erddapDatasets xmlns="https://example.test/ns"/>',
            '<erddapDatasets xml:base="/tmp/"/>',
            '<erddapDatasets><dataset></erddapDatasets>',
            '<dataset datasetID="a"/>',
            '<erddapDatasets><dataset/></erddapDatasets>',
            '<erddapDatasets><dataset datasetID="a"/><dataset datasetID="a"/></erddapDatasets>',
        ]
        for source in examples:
            with self.subTest(source=source), self.assertRaises(PlanError):
                self.write(source)
                self.plan()

    def test_rejects_unsupported_and_missing_includes(self):
        for include in [
            '<xi:include href="https://example.test/a.xml"/>',
            '<xi:include href="../outside.xml"/>',
            '<xi:include href="/absolute.xml"/>',
            '<xi:include href="missing.xml"/>',
            '<xi:include href="datasets.xml"/>',
            '<xi:include href="a.xml#part"/>',
            '<xi:include href="a.xml" xpointer="part"/>',
            '<xi:include href="a.xml" parse="text"/>',
            '<xi:include href="a.xml"><xi:fallback/></xi:include>',
        ]:
            with self.subTest(include=include), self.assertRaises(PlanError):
                self.write('<erddapDatasets xmlns:xi="http://www.w3.org/2001/XInclude">' + include + '</erddapDatasets>')
                self.plan()

    def test_rejects_symlinks_parent_symlinks_and_hardlinks(self):
        real = self.write('<erddapDatasets/>', "real.xml")
        self.root.symlink_to(real)
        with self.assertRaisesRegex(PlanError, "symlink"):
            self.plan()
        self.root.unlink()
        os.link(real, self.root)
        with self.assertRaisesRegex(PlanError, "hard link"):
            self.plan()
        self.root.unlink()
        alias = self.directory / "alias"
        alias.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaisesRegex(PlanError, "symlink"):
            build_plan([str(alias / "real.xml")], OLD, NEW)
        with self.assertRaisesRegex(PlanError, "symlink"):
            build_plan([str(alias / ".." / self.directory.name / "real.xml")], OLD, NEW)

    def test_invalid_names_and_edits_fail_closed(self):
        for old, new in [("", NEW), (OLD, ""), (OLD, OLD), (OLD, OLD.lower()), (OLD, "bad\x00"), (" " + OLD, NEW)]:
            with self.subTest(old=old, new=new), self.assertRaises(PlanError):
                build_plan([str(self.root)], old, new)
        for edits in [
            [{"start": 0, "end": 1, "before": "x", "after": "z"}],
            [{"start": -1, "end": 1, "before": "a", "after": "z"}],
            [{"start": 0, "end": 2, "before": "ab", "after": "z"}, {"start": 1, "end": 2, "before": "b", "after": "z"}],
        ]:
            with self.subTest(edits=edits), self.assertRaises(PlanError):
                apply_edits(b"abc", edits)


if __name__ == "__main__":
    unittest.main()
