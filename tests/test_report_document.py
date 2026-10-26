import json
import unittest

from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError
from memoryking.domain.reporting import CHUNK_LIMIT, SOURCE_LIMIT
from memoryking.reporting.template import model, reader_pages


def phase_source(extra=""):
    return "# Synthetic phase\n\n" + "\n\n".join(
        f"## {i}. Section {i}\n\nMeasured result {i}." + (extra if i == 7 else "")
        for i in range(1, 18)
    )


class ReportDocumentTests(unittest.TestCase):
    def test_long_unicode_code_and_tables_survive_bounded_reader_fragments(self):
        code = "print('🦣')\n" * 10000
        source = phase_source(
            "\n\n```python\n"
            + code
            + "```\n\n| Name | Value |\n| --- | --- |\n| Kept | Entire value |"
        )
        document = model(source, "Synthetic phase")
        fragments = reader_pages(document)
        self.assertTrue(all(len(p) <= CHUNK_LIMIT for p in fragments))
        recovered = []
        for index in range(len(document["blocks"])):
            parts = [json.loads(p) for p in fragments if json.loads(p)["block"] == index]
            self.assertEqual([p["part"] for p in parts], list(range(parts[0]["parts"])))
            recovered.append(json.loads("".join(p["content"] for p in parts)))
        self.assertEqual(canonical_bytes(recovered), canonical_bytes(document["blocks"]))
        self.assertEqual(
            next(b["text"] for b in recovered if b["kind"] == "code"), code.rstrip("\n")
        )
        self.assertEqual(
            next(b["rows"] for b in recovered if b["kind"] == "table"), [["Kept", "Entire value"]]
        )

    def test_missing_duplicate_out_of_order_sections_and_identity_fail(self):
        complete = phase_source()
        for source in (
            complete.replace("## 17.", "## 18."),
            complete.replace("## 2.", "## 1."),
            complete.replace("## 1.", "## 0."),
            complete.replace("# Synthetic phase", "# Wrong phase", 1),
            complete + "\n\n# Second report\n",
            complete + "\x00",
            complete + "\ud800",
            complete + "x" * SOURCE_LIMIT,
            phase_source("\n\n```\nUnterminated"),
        ):
            with self.subTest(source=source[:60]), self.assertRaises(LocalError):
                model(source, "Synthetic phase")

    def test_figure_references_are_identifiers_not_paths_or_urls(self):
        for reference in (
            "../outside.png",
            "/secret.png",
            "nested/picture.png",
            "https://host/x.png",
            "file:///x.png",
            "evil.svg",
        ):
            with self.subTest(reference=reference), self.assertRaises(LocalError):
                model(phase_source(f"\n\n![Figure]({reference})\n"), "Synthetic phase")
        report = model(phase_source("\n\n![Useful figure](image-1.png)\n"), "Synthetic phase")
        self.assertEqual(
            next(b["reference"] for b in report["blocks"] if b["kind"] == "figure"), "image-1.png"
        )

    def test_raw_html_is_data_and_qa_is_not_fabricated(self):
        report = model(
            phase_source("\n\n<script>fetch('https://example.invalid')</script>\n"),
            "Synthetic phase",
        )
        self.assertTrue(any("<script>" in b.get("text", "") for b in report["blocks"]))
        self.assertNotIn("review_status", report)
        self.assertNotIn("qa", report)
