import json
import tempfile
import unittest
from pathlib import Path

from app import create_app, create_default_feedback, seed_demo
from db import connect, transaction, utcnow
from importers import import_google_form_payload, migrate_anonymous_feedback, migrate_historical_responses
from presenters import feedback_summary, format_korean_datetime
from services import complete_attempt, save_answer, save_feedback, start_attempt, subscriber_counts


ROOT = Path(__file__).resolve().parents[1]


class QuizAppTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "ADMIN_PASSWORD": "admin-test",
            "DB_PATH": self.db_path,
            "ENABLE_TEST_IDENTITY": True,
            "SEED_DEMO_DATA": False,
            "MIGRATION_HASH_SECRET": "migration-test-secret",
        })
        seed_demo(self.db_path)
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def ids(self):
        conn = connect(self.db_path)
        subscriber = conn.execute("SELECT id FROM subscribers WHERE public_id='test-alpha'").fetchone()[0]
        episode = conn.execute("SELECT id,season_id FROM episodes WHERE code='R041'").fetchone()
        questions = conn.execute("SELECT id FROM questions WHERE episode_id=? ORDER BY display_order", (episode["id"],)).fetchall()
        conn.close()
        return subscriber, episode, [q["id"] for q in questions]

    def finish_attempt(self, subscriber, episode, question_ids):
        attempt = start_attempt(self.db_path, subscriber, episode["id"])
        conn = connect(self.db_path)
        for qid in question_ids:
            choice = conn.execute("SELECT id FROM choices WHERE question_id=? AND is_correct=1", (qid,)).fetchone()[0]
            save_answer(self.db_path, attempt, qid, choice)
        conn.close()
        return attempt, complete_attempt(self.db_path, attempt)

    def test_01_episode_exists(self):
        conn = connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0], 1)
        conn.close()

    def test_02_variable_question_count(self):
        conn = connect(self.db_path)
        episode = conn.execute("SELECT id FROM episodes WHERE code='R041'").fetchone()[0]
        conn.execute("DELETE FROM questions WHERE episode_id=?", (episode,))
        conn.commit()
        for count in (2, 4):
            with transaction(self.db_path) as tx:
                tx.execute("DELETE FROM questions WHERE episode_id=?", (episode,))
                for index in range(count):
                    qid = tx.execute("INSERT INTO questions(episode_id,text,points,display_order) VALUES(?,?,1,?)", (episode, f"Q{index}", index)).lastrowid
                    tx.execute("INSERT INTO choices(question_id,text,is_correct,display_order) VALUES(?,?,1,1)", (qid, "A"))
                    tx.execute("INSERT INTO choices(question_id,text,is_correct,display_order) VALUES(?,?,0,2)", (qid, "B"))
            check = connect(self.db_path)
            self.assertEqual(check.execute("SELECT COUNT(*) FROM questions WHERE episode_id=?", (episode,)).fetchone()[0], count)
            check.close()
        conn.close()

    def test_03_quiz_scoring(self):
        subscriber, episode, questions = self.ids()
        _, result = self.finish_attempt(subscriber, episode, questions)
        self.assertEqual(result["score"], result["total"])

    def test_04_explanation_optional(self):
        conn = connect(self.db_path)
        values = [r[0] for r in conn.execute("SELECT explanation FROM questions ORDER BY id").fetchall()]
        conn.close()
        self.assertIn(None, values)
        self.assertTrue(any(values))

    def test_05_completion_status(self):
        subscriber, episode, questions = self.ids()
        attempt, _ = self.finish_attempt(subscriber, episode, questions)
        conn = connect(self.db_path)
        self.assertEqual(conn.execute("SELECT status FROM quiz_attempts WHERE id=?", (attempt,)).fetchone()[0], "completed")
        conn.close()

    def test_06_participation_created_once(self):
        subscriber, episode, questions = self.ids()
        self.finish_attempt(subscriber, episode, questions)
        self.finish_attempt(subscriber, episode, questions)
        conn = connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM participation WHERE subscriber_id=? AND episode_id=?", (subscriber, episode["id"])).fetchone()[0]
        attempts = conn.execute("SELECT COUNT(*) FROM quiz_attempts WHERE subscriber_id=? AND episode_id=?", (subscriber, episode["id"])).fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)
        self.assertEqual(attempts, 2)

    def test_07_season_and_total_counts(self):
        subscriber, episode, questions = self.ids()
        self.finish_attempt(subscriber, episode, questions)
        conn = connect(self.db_path)
        counts = subscriber_counts(conn, subscriber, episode["season_id"])
        conn.close()
        self.assertEqual(counts, {"season": 1, "total": 1})

    def test_08_feedback_saved(self):
        subscriber, episode, questions = self.ids()
        self.finish_attempt(subscriber, episode, questions)
        conn = connect(self.db_path)
        feedback_q = conn.execute("SELECT id,response_type FROM feedback_questions WHERE episode_id=? ORDER BY display_order", (episode["id"],)).fetchall()
        conn.close()
        values = {feedback_q[0]["id"]: ["핵심 개념"], feedback_q[1]["id"]: "5", feedback_q[2]["id"]: "다음 주제"}
        save_feedback(self.db_path, episode["id"], subscriber, values)
        conn = connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM feedback_submissions").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM feedback_answers").fetchone()[0], 3)
        conn.close()

    def test_09_completion_without_feedback(self):
        subscriber, episode, questions = self.ids()
        self.finish_attempt(subscriber, episode, questions)
        conn = connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM participation").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM feedback_submissions").fetchone()[0], 0)
        conn.close()

    def test_10_unpublished_episode_blocked(self):
        with transaction(self.db_path) as conn:
            conn.execute("UPDATE episodes SET is_published=0 WHERE code='R041'")
        with self.client.session_transaction() as state:
            state["subscriber_id"] = self.ids()[0]
        self.assertEqual(self.client.get("/quiz?episode=R041").status_code, 404)

    def test_11_sample_form_import(self):
        payload = json.loads((ROOT / "fixtures" / "sample_google_form.json").read_text(encoding="utf-8"))
        result = import_google_form_payload(self.db_path, payload)
        self.assertEqual(result["episode_code"], "R042")
        self.assertEqual(result["questions"], 2)
        conn = connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM questions WHERE episode_id=?", (result["episode_id"],)).fetchone()[0], 2)
        conn.close()

    def test_12_historical_migration_deduplicates(self):
        text = (ROOT / "fixtures" / "sample_historical_responses.csv").read_text(encoding="utf-8")
        result = migrate_historical_responses(self.db_path, text, "R041", "secret", "last")
        self.assertEqual(result["rows"], 3)
        self.assertEqual(result["participations_created"], 2)
        conn = connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM participation WHERE source='historical'").fetchone()[0], 2)
        conn.close()

    def test_13_duplicate_migration_remains_idempotent_for_participation(self):
        text = (ROOT / "fixtures" / "sample_historical_responses.csv").read_text(encoding="utf-8")
        migrate_historical_responses(self.db_path, text, "R041", "secret")
        second = migrate_historical_responses(self.db_path, text, "R041", "secret")
        self.assertEqual(second["participations_created"], 0)
        conn = connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM participation WHERE source='historical'").fetchone()[0], 2)
        conn.close()

    def test_14_anonymous_feedback_migration(self):
        text = (ROOT / "fixtures" / "sample_historical_feedback.csv").read_text(encoding="utf-8")
        result = migrate_anonymous_feedback(self.db_path, text, "R041")
        self.assertEqual(result["feedback_submissions_created"], 2)
        conn = connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM feedback_submissions WHERE subscriber_id IS NULL").fetchone()[0], 2)
        conn.close()

    def test_15_admin_authentication(self):
        self.assertEqual(self.client.get("/admin").status_code, 302)
        with self.client.session_transaction() as state:
            state["csrf_token"] = "test-csrf"
        response = self.client.post(
            "/admin/login", data={"password": "admin-test", "csrf_token": "test-csrf"}, follow_redirects=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("운영 현황", response.get_data(as_text=True))

    def test_16_mobile_quiz_route_flow(self):
        subscriber, episode, question_ids = self.ids()
        with self.client.session_transaction() as state:
            state["subscriber_id"] = subscriber
            state["csrf_token"] = "route-csrf"
        entry = self.client.get("/quiz?episode=R041")
        self.assertEqual(entry.status_code, 200)
        start = self.client.post(
            "/quiz/R041/start", data={"csrf_token": "route-csrf"}, follow_redirects=False
        )
        self.assertEqual(start.status_code, 302)
        location = start.headers["Location"]
        conn = connect(self.db_path)
        for index, question_id in enumerate(question_ids, start=1):
            choice_id = conn.execute(
                "SELECT id FROM choices WHERE question_id=? AND is_correct=1", (question_id,)
            ).fetchone()[0]
            response = self.client.post(
                location,
                data={"csrf_token": "route-csrf", "choice_id": choice_id},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 302)
            location = response.headers["Location"]
        conn.close()
        complete = self.client.get(location)
        self.assertEqual(complete.status_code, 200)
        self.assertIn("이해 테스트 완료", complete.get_data(as_text=True))

    def test_17_admin_presenters_are_human_readable(self):
        self.assertEqual(
            format_korean_datetime("2026-09-19T09:36:11.350866+00:00"),
            "2026.09.19 18:36",
        )
        multi = feedback_summary(
            "multi_choice",
            [{"value_text": None, "value_json": '["핵심 개념", "실제 사례"]'}],
        )
        rating = feedback_summary(
            "rating", [{"value_text": "5", "value_json": None}]
        )
        self.assertEqual(multi[0]["label"], "실제 사례")
        self.assertEqual({item["label"] for item in multi}, {"핵심 개념", "실제 사례"})
        self.assertEqual(rating, [{"label": "5점 / 5점 (매우 만족)", "count": 1}])

    def test_18_admin_pages_render_clean_feedback_and_korean_time(self):
        subscriber, episode, questions = self.ids()
        self.finish_attempt(subscriber, episode, questions)
        conn = connect(self.db_path)
        feedback_q = conn.execute(
            "SELECT id,response_type FROM feedback_questions WHERE episode_id=? ORDER BY display_order",
            (episode["id"],),
        ).fetchall()
        conn.close()
        save_feedback(
            self.db_path,
            episode["id"],
            subscriber,
            {feedback_q[0]["id"]: ["핵심 개념"], feedback_q[1]["id"]: "5"},
        )
        with self.client.session_transaction() as state:
            state["is_admin"] = True
        feedback_page = self.client.get(f"/admin/episodes/{episode['id']}/feedback")
        subscriber_page = self.client.get("/admin/subscribers")
        feedback_html = feedback_page.get_data(as_text=True)
        subscriber_html = subscriber_page.get_data(as_text=True)
        self.assertIn("핵심 개념 · 1건", feedback_html)
        self.assertIn("5점 / 5점 (매우 만족) · 1건", feedback_html)
        self.assertNotIn('[&#34;핵심 개념&#34;]', feedback_html)
        self.assertIn("최근 참여 (한국시간)", subscriber_html)
        self.assertNotIn("T", subscriber_html.split("최근 참여 (한국시간)", 1)[1])

    def test_19_production_mode_hides_test_identity_and_rejects_stale_test_session(self):
        production_app = create_app({
            "TESTING": True,
            "SECRET_KEY": "production-test-secret",
            "DB_PATH": self.db_path,
            "ENABLE_TEST_IDENTITY": False,
            "SEED_DEMO_DATA": False,
        })
        client = production_app.test_client()
        self.assertEqual(client.get("/test-identity").status_code, 404)
        with client.session_transaction() as state:
            state["subscriber_id"] = self.ids()[0]
        self.assertEqual(client.get("/quiz?episode=R041").status_code, 401)
        with client.session_transaction() as state:
            self.assertNotIn("subscriber_id", state)

    def test_20_demo_seed_requires_test_identity_mode(self):
        isolated = str(Path(self.temp.name) / "invalid-mode.db")
        with self.assertRaises(RuntimeError):
            create_app({
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "DB_PATH": isolated,
                "ENABLE_TEST_IDENTITY": False,
                "SEED_DEMO_DATA": True,
            })


if __name__ == "__main__":
    unittest.main()
