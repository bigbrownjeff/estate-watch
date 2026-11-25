"""Tests for local-tools/codex-memory-import note_url(): the address of the vault
note from `notes_vault log` stdout, on the routed (outbox) path. Pure function,
no vault, no network."""
import importlib.machinery
import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "local-tools" / "codex-memory-import"
LOADER = importlib.machinery.SourceFileLoader("codex_memory_import", str(SCRIPT))
SPEC = importlib.util.spec_from_loader("codex_memory_import", LOADER)
cmi = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(cmi)

BASE = "https://notes.jeffpinto.com"
NID = "codex-memory-candidates-2026-11-25"


class NoteUrlTest(unittest.TestCase):
    def test_applied_note_gives_the_id_address(self):
        out = "logged note #4711 (log/%s)\n" % NID
        self.assertEqual(cmi.note_url(out, NID), BASE + "/#id=4711")

    def test_queued_output_gives_the_address_line(self):
        out = ("QUEUED log/%s\n  event: ev-1\n"
               "  address: https://notes.jeffpinto.com/#sid=log/%s\n" % (NID, NID))
        self.assertEqual(cmi.note_url(out, NID), BASE + "/#sid=log/" + NID)

    def test_queued_address_line_is_taken_verbatim_not_rebuilt(self):
        out = "QUEUED\n  address: https://example.invalid/#sid=elsewhere\n"
        self.assertEqual(cmi.note_url(out, NID), "https://example.invalid/#sid=elsewhere")

    def test_queued_without_an_address_falls_back_to_the_sid_address(self):
        self.assertEqual(cmi.note_url("QUEUED log/x\n", NID), "%s/#sid=log/%s" % (BASE, NID))

    def test_applied_id_wins_over_a_queued_word_and_an_address(self):
        out = "logged note #9\nQUEUED earlier\n  address: https://x.invalid/\n"
        self.assertEqual(cmi.note_url(out, NID), BASE + "/#id=9")

    def test_multi_digit_ids_are_read_whole(self):
        self.assertEqual(cmi.note_url("logged note #123456\n", NID), BASE + "/#id=123456")

    def test_output_with_neither_marker_raises_loudly(self):
        # the code lane's pin said "neither raises"; the code raises RuntimeError here,
        # and main() turns that into "vault log FAILED" and rc 1
        with self.assertRaises(RuntimeError) as ctx:
            cmi.note_url("something unexpected\n", NID)
        self.assertIn("something unexpected", str(ctx.exception))

    def test_empty_output_raises(self):
        with self.assertRaises(RuntimeError):
            cmi.note_url("", NID)


if __name__ == "__main__":
    unittest.main()
