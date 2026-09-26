import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from audioletter_text import render_audioletter_text
from db import connect, init_db, transaction, utcnow
from season1_audioletter_cleanup import (
    CleanupValidationError,
    PRESERVED_MARKDOWN,
    REVIEW_REQUIRED,
    SAFE_AUTO_FIX,
    apply_cleanup,
    scan_cleanup,
)


ROOT = Path(__file__).resolve().parents[1]


class Season1AudioletterCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "portal.db")
        init_db(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def _episode(self, sequence=1, *, season=1, title="Episode"):
        with transaction(self.db_path) as conn:
            now = utcnow()
            return conn.execute(
                """INSERT INTO audioletter_episodes
                   (sequence,season,season_episode,title,audio_storage_key,transcript,is_published,created_at,updated_at)
                   VALUES(?,?,?,?,NULL,'',0,?,?)""",
                (sequence, season, sequence, title, now, now),
            ).lastrowid

    def _block(self, episode_id, order, kind, *, title="", transcript="", body="", key=None):
        with transaction(self.db_path) as conn:
            now = utcnow()
            return conn.execute(
                """INSERT INTO audioletter_blocks
                   (episode_id,sort_order,block_type,title,body,audio_storage_key,transcript,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (episode_id, order, kind, title, body, key, transcript, now, now),
            ).lastrowid

    def _field(self, block_id, field):
        conn = connect(self.db_path)
        value = conn.execute(f"SELECT {field} FROM audioletter_blocks WHERE id=?", (block_id,)).fetchone()[0]
        conn.close()
        return value

    def test_scan_is_read_only_and_reports_br_empty_block_and_wrapper_cleanup(self):
        episode_id = self._episode()
        block_id = self._block(
            episode_id, 1, "audio", key="audioletters/season1/s1-01-01.mp3",
            transcript="첫 줄&lt;br&gt;둘째 줄<br />셋째 줄\n<empty-block>\n<synced_block id=\"a\">\n마지막 줄\n</synced_block>",
        )
        before = Path(self.db_path).read_bytes()
        report = scan_cleanup(self.db_path)
        self.assertEqual(Path(self.db_path).read_bytes(), before)
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(report["scanned episodes"], 1)
        self.assertEqual(report["scanned blocks"], 1)
        self.assertEqual(report["SAFE_AUTO_FIX"], 1)
        finding = report["findings"][0]
        self.assertEqual(finding["episode"], "S1-01")
        self.assertEqual(finding["sort_order"], 1)
        self.assertEqual(finding["field"], "transcript")
        self.assertEqual(finding["classification"], SAFE_AUTO_FIX)
        for pattern in ("ENCODED_BR", "LITERAL_BR", "LITERAL_EMPTY_BLOCK", "NOTION_SYNCED_BLOCK_WRAPPER"):
            self.assertIn(pattern, finding["detected_patterns"])
        self.assertEqual(self._field(block_id, "transcript"),
                         "첫 줄&lt;br&gt;둘째 줄<br />셋째 줄\n<empty-block>\n<synced_block id=\"a\">\n마지막 줄\n</synced_block>")

    def test_apply_normalizes_br_empty_block_wrapper_and_is_idempotent(self):
        episode_id = self._episode()
        block_id = self._block(
            episode_id, 1, "audio", key="audioletters/season1/s1-01-01.mp3",
            transcript="가&lt;br/&gt;나\n&lt;empty-block&gt;\n<synced_block>\n다\n</synced_block>",
        )
        first = apply_cleanup(self.db_path)
        self.assertEqual(first["updated fields"], 1)
        self.assertEqual(self._field(block_id, "transcript"), "가\n나\n\n다\n")
        second = apply_cleanup(self.db_path)
        self.assertEqual(second["updated fields"], 0)
        self.assertEqual(second["SAFE_AUTO_FIX"], 0)

    def test_shared_tab_and_four_space_prose_indentation_are_safe(self):
        episode_id = self._episode()
        tab_block = self._block(episode_id, 1, "audio", key="audioletters/season1/s1-01-01.mp3",
                                transcript="\t첫 문장\n\t둘째 문장")
        space_block = self._block(episode_id, 2, "info", body="    첫 안내\n    둘째 안내")
        report = scan_cleanup(self.db_path)
        self.assertEqual(report["SAFE_AUTO_FIX"], 2)
        self.assertEqual(apply_cleanup(self.db_path)["updated fields"], 2)
        self.assertEqual(self._field(tab_block, "transcript"), "첫 문장\n둘째 문장")
        self.assertEqual(self._field(space_block, "body"), "첫 안내\n둘째 안내")

    def test_repeated_prose_indentation_is_safe_even_when_other_paragraphs_are_not_indented(self):
        episode_id = self._episode()
        block_id = self._block(
            episode_id, 1, "audio", key="audioletters/season1/s1-01-01.mp3",
            transcript="앞 문단\n\t첫 번째 들여쓰기 문장\n\t둘째 들여쓰기 문장\n마지막 문단",
        )
        report = scan_cleanup(self.db_path)
        self.assertEqual(report["SAFE_AUTO_FIX"], 1)
        self.assertIn("TAB_INDENTATION", report["findings"][0]["safe_patterns"])
        apply_cleanup(self.db_path)
        self.assertEqual(self._field(block_id, "transcript"),
                         "앞 문단\n첫 번째 들여쓰기 문장\n둘째 들여쓰기 문장\n마지막 문단")

    def test_normal_markdown_is_preserved_and_not_an_apply_blocker(self):
        episode_id = self._episode()
        self._block(episode_id, 1, "audio", title="**강조 제목**", transcript="***National Research Council (NRC)***",
                    key="audioletters/season1/s1-01-01.mp3")
        report = scan_cleanup(self.db_path)
        self.assertEqual(report["SAFE_AUTO_FIX"], 0)
        self.assertEqual(report["REVIEW_REQUIRED"], 0)
        self.assertEqual(report["PRESERVED_MARKDOWN"], 2)
        self.assertTrue(all(item["classification"] == PRESERVED_MARKDOWN for item in report["findings"]))
        self.assertEqual(apply_cleanup(self.db_path)["updated fields"], 0)

    def test_ambiguous_indentation_inline_empty_block_and_unknown_tag_require_review(self):
        episode_id = self._episode()
        self._block(episode_id, 1, "audio", key="audioletters/season1/s1-01-01.mp3",
                    transcript="\t한 줄뿐인 들여쓰기")
        self._block(episode_id, 2, "info", body="문장<empty-block>다음 문장")
        self._block(episode_id, 3, "info", body="<unknown-export>원문</unknown-export>")
        report = scan_cleanup(self.db_path)
        self.assertEqual(report["REVIEW_REQUIRED"], 3)
        self.assertTrue(all(item["classification"] == REVIEW_REQUIRED for item in report["findings"]))
        with self.assertRaises(CleanupValidationError):
            apply_cleanup(self.db_path)

    def test_legacy_transcript_is_scanned_only_without_canonical_blocks(self):
        episode_id = self._episode()
        with transaction(self.db_path) as conn:
            conn.execute("UPDATE audioletter_episodes SET transcript='legacy&lt;br&gt;text' WHERE id=?", (episode_id,))
        self.assertEqual(scan_cleanup(self.db_path)["SAFE_AUTO_FIX"], 1)
        self._block(episode_id, 1, "audio", transcript="canonical", key="audioletters/season1/s1-01-01.mp3")
        self.assertEqual(scan_cleanup(self.db_path)["SAFE_AUTO_FIX"], 0)

    def test_apply_keeps_other_seasons_counts_order_and_audio_storage_key_unchanged(self):
        season1_id = self._episode()
        season1_block = self._block(season1_id, 1, "audio", transcript="a&lt;br&gt;b",
                                    key="audioletters/season1/s1-01-01.mp3")
        season2_id = self._episode(sequence=43, season=2, title="Season 2")
        season2_block = self._block(season2_id, 1, "audio", transcript="c&lt;br&gt;d",
                                    key="audioletters/season2/s2-01-01.mp3")
        conn = connect(self.db_path)
        before_counts = (conn.execute("SELECT COUNT(*) FROM audioletter_episodes").fetchone()[0],
                         conn.execute("SELECT COUNT(*) FROM audioletter_blocks").fetchone()[0])
        before_key = conn.execute("SELECT audio_storage_key FROM audioletter_blocks WHERE id=?", (season1_block,)).fetchone()[0]
        conn.close()
        apply_cleanup(self.db_path)
        self.assertEqual(self._field(season1_block, "transcript"), "a\nb")
        self.assertEqual(self._field(season2_block, "transcript"), "c&lt;br&gt;d")
        conn = connect(self.db_path)
        self.assertEqual((conn.execute("SELECT COUNT(*) FROM audioletter_episodes").fetchone()[0],
                          conn.execute("SELECT COUNT(*) FROM audioletter_blocks").fetchone()[0]), before_counts)
        self.assertEqual(conn.execute("SELECT audio_storage_key FROM audioletter_blocks WHERE id=?", (season1_block,)).fetchone()[0], before_key)
        self.assertEqual(conn.execute("SELECT sort_order FROM audioletter_blocks WHERE id=?", (season1_block,)).fetchone()[0], 1)
        conn.close()

    def test_apply_rolls_back_when_an_update_fails(self):
        episode_id = self._episode()
        first = self._block(episode_id, 1, "audio", transcript="a&lt;br&gt;b", key="audioletters/season1/s1-01-01.mp3")
        second = self._block(episode_id, 2, "info", body="c&lt;br&gt;d")
        from season1_audioletter_cleanup import _update_finding as real_update
        calls = 0

        def fail_after_first(conn, finding):
            nonlocal calls
            calls += 1
            real_update(conn, finding)
            if calls == 1:
                raise RuntimeError("forced rollback")

        with patch("season1_audioletter_cleanup._update_finding", side_effect=fail_after_first):
            with self.assertRaises(RuntimeError):
                apply_cleanup(self.db_path)
        self.assertEqual(self._field(first, "transcript"), "a&lt;br&gt;b")
        self.assertEqual(self._field(second, "body"), "c&lt;br&gt;d")

    def test_cli_dry_run_is_read_only_and_apply_requires_both_flags(self):
        episode_id = self._episode()
        self._block(episode_id, 1, "audio", transcript="a&lt;br&gt;b", key="audioletters/season1/s1-01-01.mp3")
        before = Path(self.db_path).read_bytes()
        completed = subprocess.run(
            [sys.executable, "manage.py", "season1-audioletter-cleanup", "--db", self.db_path],
            cwd=ROOT, check=True, text=True, capture_output=True,
        )
        self.assertEqual(Path(self.db_path).read_bytes(), before)
        self.assertEqual(json.loads(completed.stdout)["mode"], "dry-run")
        refused = subprocess.run(
            [sys.executable, "manage.py", "season1-audioletter-cleanup", "--db", self.db_path, "--apply"],
            cwd=ROOT, text=True, capture_output=True,
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("--apply requires --confirm-apply", refused.stderr)
        self.assertEqual(Path(self.db_path).read_bytes(), before)

    def test_json_report_contains_full_audioletter_findings_but_console_keeps_previews(self):
        episode_id = self._episode()
        source = "앞 문장<br>\t한 줄뿐인 들여쓰기"
        self._block(episode_id, 1, "audio", transcript=source, key="audioletters/season1/s1-01-01.mp3")
        report_path = Path(self.temp.name) / "season1-report.json"
        before = Path(self.db_path).read_bytes()
        console = scan_cleanup(self.db_path, report_json=report_path)
        self.assertEqual(Path(self.db_path).read_bytes(), before)
        self.assertEqual(console["REVIEW_REQUIRED"], 1)
        self.assertNotIn("before_value", console["findings"][0])
        self.assertIn("before_preview", console["findings"][0])
        saved = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["summary"]["scanned_episodes"], 1)
        finding = saved["findings"][0]
        self.assertEqual(finding["before_value"], source)
        self.assertEqual(finding["proposed_after_value"], "앞 문장\n\t한 줄뿐인 들여쓰기")
        self.assertIn("LITERAL_BR", finding["safe_patterns"])
        self.assertIn("AMBIGUOUS_INDENTATION", finding["review_patterns"])
        self.assertEqual(finding["classification"], REVIEW_REQUIRED)
        serialized = report_path.read_text(encoding="utf-8")
        self.assertNotIn("subscriber", serialized.lower())
        self.assertNotIn("email", serialized.lower())
        self.assertEqual(Path(self.db_path).read_bytes(), before)

    def test_cli_writes_full_json_report_without_db_write(self):
        episode_id = self._episode()
        self._block(episode_id, 1, "audio", transcript="a&lt;br&gt;b", key="audioletters/season1/s1-01-01.mp3")
        report_path = Path(self.temp.name) / "report.json"
        before = Path(self.db_path).read_bytes()
        completed = subprocess.run(
            [sys.executable, "manage.py", "season1-audioletter-cleanup", "--db", self.db_path,
             "--report-json", str(report_path)],
            cwd=ROOT, check=True, text=True, capture_output=True,
        )
        self.assertEqual(Path(self.db_path).read_bytes(), before)
        self.assertEqual(json.loads(completed.stdout)["report_json"], str(report_path))
        self.assertEqual(json.loads(report_path.read_text(encoding="utf-8"))["findings"][0]["before_value"], "a&lt;br&gt;b")

    def test_safe_cleanup_does_not_regress_xss_protection(self):
        episode_id = self._episode()
        block_id = self._block(episode_id, 1, "audio", transcript="첫 줄&lt;br&gt;<script>alert(1)</script>",
                               key="audioletters/season1/s1-01-01.mp3")
        # Unknown raw HTML remains review-only, and the existing renderer still escapes it.
        report = scan_cleanup(self.db_path)
        self.assertEqual(report["REVIEW_REQUIRED"], 1)
        rendered = str(render_audioletter_text(self._field(block_id, "transcript")))
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)
        self.assertNotIn("<script>alert(1)</script>", rendered)


if __name__ == "__main__":
    unittest.main()
