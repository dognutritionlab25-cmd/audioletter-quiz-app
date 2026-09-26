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

    def _seed_production_s1_07_reference(self):
        with transaction(self.db_path) as conn:
            now = utcnow()
            episode_id = conn.execute(
                """INSERT INTO audioletter_episodes
                   (sequence,season,season_episode,title,audio_storage_key,transcript,quiz_episode_code,is_published,created_at,updated_at)
                   VALUES(7,1,7,'Production S1-07 reference',NULL,'','R007',1,?,?)""",
                (now, now),
            ).lastrowid
            for order, block_type, title, body, key, transcript in [
                (1, "audio", "reference greeting", "", "reference/s1-07-01.mp3", "reference transcript 1"),
                (2, "audio", "reference tip", "", "reference/s1-07-02.mp3", "reference transcript 2"),
                (3, "info", "reference info", "reference info body", None, ""),
                (4, "audio", "reference nutrition", "", "reference/s1-07-03.mp3", "reference transcript 3"),
            ]:
                conn.execute(
                    """INSERT INTO audioletter_blocks
                       (episode_id,sort_order,block_type,title,body,audio_storage_key,transcript,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (episode_id, order, block_type, title, body, key, transcript, now, now),
                )
        return episode_id

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
        self.assertEqual(first, {"mode": "apply", "created": 42, "updated": 0, "unchanged": 0, "preserved": 0})
        second = apply_manifest(self.db_path, MANIFEST)
        self.assertEqual(second, {"mode": "apply", "created": 0, "updated": 0, "unchanged": 41, "preserved": 1})
        result = dry_run(self.db_path, MANIFEST)
        self.assertEqual(result["UNCHANGED episodes"], 41)
        self.assertEqual(result["PRESERVE_EXISTING episodes"], 1)
        conn = connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM audioletter_episodes").fetchone()[0], 42)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM audioletter_blocks").fetchone()[0], 122)
        conn.close()

    def test_existing_s1_07_reference_is_preserved_without_any_write(self):
        episode_id = self._seed_production_s1_07_reference()
        conn = connect(self.db_path)
        before_episode = tuple(conn.execute("SELECT * FROM audioletter_episodes WHERE id=?", (episode_id,)).fetchone())
        before_blocks = [tuple(row) for row in conn.execute(
            "SELECT * FROM audioletter_blocks WHERE episode_id=? ORDER BY sort_order", (episode_id,)
        ).fetchall()]
        conn.close()
        result = dry_run(self.db_path, MANIFEST)
        s107 = result["episodes"][6]
        self.assertEqual(result["CREATE episodes"], 41)
        self.assertEqual(result["CONFLICT episodes"], 0)
        self.assertEqual(result["PRESERVE_EXISTING episodes"], 1)
        self.assertEqual(s107["episode"], "PRESERVE_EXISTING")
        self.assertEqual(s107["blocks"], ["PRESERVE_EXISTING"] * 4)
        self.assertIn("preserved by migration policy", s107["reasons"][0])
        applied = apply_manifest(self.db_path, MANIFEST)
        self.assertEqual(applied["created"], 41)
        self.assertEqual(applied["preserved"], 1)
        conn = connect(self.db_path)
        self.assertEqual(tuple(conn.execute("SELECT * FROM audioletter_episodes WHERE id=?", (episode_id,)).fetchone()), before_episode)
        self.assertEqual([tuple(row) for row in conn.execute(
            "SELECT * FROM audioletter_blocks WHERE episode_id=? ORDER BY sort_order", (episode_id,)
        ).fetchall()], before_blocks)
        conn.close()

    def test_non_s1_07_conflict_still_blocks_apply(self):
        apply_manifest(self.db_path, MANIFEST)
        with transaction(self.db_path) as conn:
            conn.execute("UPDATE audioletter_episodes SET title='unexpected S1-08 change' WHERE sequence=8")
        result = dry_run(self.db_path, MANIFEST)
        self.assertEqual(result["episodes"][6]["episode"], "PRESERVE_EXISTING")
        self.assertEqual(result["episodes"][7]["episode"], "CONFLICT")
        self.assertEqual(result["CONFLICT episodes"], 1)
        with self.assertRaises(ManifestValidationError):
            apply_manifest(self.db_path, MANIFEST)

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
