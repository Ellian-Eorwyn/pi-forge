import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import eml_parser


class EmlParserTests(unittest.TestCase):
    def write_message(self, directory, message, name="message.eml"):
        path = Path(directory) / name
        path.write_bytes(message.as_bytes())
        return path

    def test_plain_email_preserves_headers_body_threading_and_attachment_hashes(self):
        message = EmailMessage()
        message["Subject"] = "Project update"
        message["From"] = "Alex <alex@example.com>"
        message["To"] = "Sam <sam@example.com>"
        message["Date"] = "Mon, 27 Jul 2026 10:00:00 -0700"
        message["Message-ID"] = "<child@example.com>"
        message["In-Reply-To"] = "<parent@example.com>"
        message["References"] = "<root@example.com> <parent@example.com>"
        message.set_content("The final report is due Friday.\n\nPlease send the revised budget.")
        message.add_attachment(b"budget data", maintype="application", subtype="octet-stream", filename="../budget.csv")

        with tempfile.TemporaryDirectory() as temporary:
            source = self.write_message(temporary, message)
            attachments = Path(temporary) / "attachments"
            result = eml_parser.parse_eml(source, attachments, "attachments")

            self.assertIn("# Project update", result["markdown"])
            self.assertIn("The final report is due Friday.", result["markdown"])
            self.assertEqual(result["email"]["selectedHeaders"]["inReplyToIds"], ["<parent@example.com>"])
            self.assertEqual(result["email"]["selectedHeaders"]["referencesIds"], ["<root@example.com>", "<parent@example.com>"])
            attachment = result["email"]["attachments"][0]
            self.assertEqual(attachment["filename"], "budget.csv")
            self.assertEqual((attachments / "budget.csv").read_bytes(), b"budget data")
            self.assertEqual(attachment["sha256"], eml_parser.sha256_bytes(b"budget data"))
            body_entry = next(entry for entry in result["sourceMapEntries"] if entry["sourceLocator"]["type"] == "email-mime-part" and entry["sourceLocator"].get("mimeType") == "text/plain")
            cited = result["markdown"].splitlines()[body_entry["markdownStartLine"] - 1 : body_entry["markdownEndLine"]]
            self.assertIn("The final report is due Friday.", "\n".join(cited))

    def test_html_fallback_is_conservative_and_never_fetches_remote_content(self):
        message = EmailMessage()
        message["Subject"] = "HTML only"
        message["From"] = "sender@example.com"
        message.set_content("<p>Read the <strong>attached memo</strong>.</p><img src='https://example.com/track.png' alt='tracker'>", subtype="html")

        with tempfile.TemporaryDirectory() as temporary:
            source = self.write_message(temporary, message)
            result = eml_parser.parse_eml(source, Path(temporary) / "attachments", "attachments")

            self.assertIn("Read the **attached memo**.", result["markdown"])
            self.assertIn("[Image: tracker]", result["markdown"])
            self.assertNotIn("https://example.com/track.png", result["markdown"])
            self.assertTrue(any("no usable text/plain" in warning for warning in result["warnings"]))

    def test_duplicate_attachment_names_receive_collision_safe_paths(self):
        message = EmailMessage()
        message["Subject"] = "Duplicates"
        message.set_content("Two files are attached.")
        message.add_attachment(b"one", maintype="text", subtype="plain", filename="notes.txt")
        message.add_attachment(b"two", maintype="text", subtype="plain", filename="notes.txt")

        with tempfile.TemporaryDirectory() as temporary:
            source = self.write_message(temporary, message)
            result = eml_parser.parse_eml(source, Path(temporary) / "attachments", "attachments")

            self.assertEqual([item["filename"] for item in result["email"]["attachments"]], ["notes.txt", "notes-2.txt"])


if __name__ == "__main__":
    unittest.main()
