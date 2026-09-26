import sqlite3
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from db import connect, init_db, transaction, utcnow
from season1_manifest_importer import ManifestValidationError, apply_manifest, dry_run, parse_manifest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "fixtures" / "Season1_Final_Migration_Manifest.md"


class Season1ManifestImporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "portal.db")
        init_db(self.db_path)
        self.episodes = parse_manifest(MANIFEST)
        with transaction(self.db_path) as conn:
            now = utcnow()
            season_id = conn.execute(
                "INSERT INTO seasons(code,title,created_at) VALUES('S1','Season 1',?)", (now,)
            ).lastrowid
            for episode in self.episodes:
                conn.execute(
                    """INSERT INTO episodes(season_id,code,title,description,display_order,is_published,created_at,updated_at)
                       VALUES(?,?,?,'',?,1,?,?)""",
                    (season_id, episode.quiz_episode_code, episode.quiz_episode_code,
                     episode.sequence, now, now),
                )

    def tearDown(self):
        self.temp.cleanup()

    def test_parser_reads_final_manifest_and_preserves_reference_order(self):
        self.assertEqual(len(self.episodes), 42)
        self.assertEqual(sum(b.block_type == "audio" for e in self.episodes for b in e.blocks), 86)
        self.assertEqual(sum(b.block_type == "info" for e in self.episodes for b in e.blocks), 36)
        self.assertEqual(len({b.source_mp3 for e in self.episodes for b in e.blocks if b.block_type == "audio"}), 86)
        self.assertEqual(len({b.bucket_key for e in self.episodes for b in e.blocks if b.block_type == "audio"}), 86)
        s107 = self.episodes[6]
        self.assertEqual([(b.sort_order, b.block_type) for b in s107.blocks],
                         [(1, "audio"), (2, "audio"), (3, "info"), (4, "audio")])
        self.assertEqual([b.bucket_key for b in s107.blocks if b.block_type == "audio"], [
            "audioletters/season1/s1-07-01.mp3",
            "audioletters/season1/s1-07-02.mp3",
            "audioletters/season1/s1-07-03.mp3",
        ])
        self.assertEqual(self.episodes[4].blocks[1].content_code, "HE")
        self.assertEqual(self.episodes[4].blocks[1].source_mp3, "R005-HE AU.MP3")

    def test_dry_run_is_read_only_and_reports_creates(self):
        before = Path(self.db_path).read_bytes()
        result = dry_run(self.db_path, MANIFEST)
        self.assertEqual(Path(self.db_path).read_bytes(), before)
        self.assertEqual(result["CREATE episodes"], 42)
        self.assertEqual(result["CONFLICT episodes"], 0)
        self.assertEqual(result["episodes"][6]["code"], "S1-07")
        self.assertEqual(result["episodes"][6]["blocks"], ["CREATE", "CREATE", "CREATE", "CREATE"])

    def test_cli_dry_run_does_not_initialize_or_write_database(self):
        before = Path(self.db_path).read_bytes()
        completed = subprocess.run(
            [sys.executable, "manage.py", "season1-audioletter-import",
             "--manifest", str(MANIFEST), "--db", self.db_path],
            cwd=ROOT, check=True, text=True, capture_output=True,
        )
        self.assertEqual(Path(self.db_path).read_bytes(), before)
        self.assertEqual(json.loads(completed.stdout)["mode"], "dry-run")

    def test_apply_is_idempotent_and_followup_dry_run_is_unchanged(self):
        first = apply_manifest(self.db_path, MANIFEST)
        self.assertEqual(first, {"mode": "apply", "created": 42, "updated": 0, "unchanged": 0})
        second = apply_manifest(self.db_path, MANIFEST)
        self.assertEqual(second, {"mode": "apply", "created": 0, "updated": 0, "unchanged": 42})
        result = dry_run(self.db_path, MANIFEST)
        self.assertEqual(result["UNCHANGED episodes"], 42)
        conn = connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM audioletter_episodes").fetchone()[0], 42)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM audioletter_blocks").fetchone()[0], 122)
        conn.close()

    def test_existing_populated_s1_07_is_conflict_and_never_overwritten(self):
        apply_manifest(self.db_path, MANIFEST)
        with transaction(self.db_path) as conn:
            conn.execute("UPDATE audioletter_episodes SET title='수동 S1-07' WHERE sequence=7")
        result = dry_run(self.db_path, MANIFEST)
        s107 = result["episodes"][6]
        self.assertEqual(s107["episode"], "CONFLICT")
        self.assertIn("episode metadata differs", s107["reasons"])
        with self.assertRaises(ManifestValidationError):
            apply_manifest(self.db_path, MANIFEST)
        conn = connect(self.db_path)
        self.assertEqual(conn.execute("SELECT title FROM audioletter_episodes WHERE sequence=7").fetchone()[0], "수동 S1-07")
        conn.close()

    def test_only_empty_unpublished_draft_can_update(self):
        with transaction(self.db_path) as conn:
            now = utcnow()
            conn.execute(
                """INSERT INTO audioletter_episodes
                   (sequence,season,season_episode,title,audio_storage_key,transcript,quiz_episode_code,is_published,created_at,updated_at)
                   VALUES(1,1,1,'',NULL,'',NULL,0,?,?)""", (now, now)
            )
        result = dry_run(self.db_path, MANIFEST)
        self.assertEqual(result["episodes"][0]["episode"], "UPDATE")
        self.assertEqual(result["episodes"][0]["blocks"], ["CREATE", "CREATE", "CREATE"])


if __name__ == "__main__":
    unittest.main()
