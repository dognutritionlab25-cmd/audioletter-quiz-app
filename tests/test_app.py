import csv
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from app import create_app, create_default_feedback, seed_demo
from db import connect, transaction, utcnow
from importers import import_google_form_payload, migrate_anonymous_feedback, migrate_historical_responses
from magic_links import MagicLinkDeliveryError, create_magic_link_token, send_magic_link_via_brevo
from presenters import feedback_summary, format_korean_datetime
from quiz_csv_import import import_quiz_rows, parse_quiz_csv, preview_quiz_import
from services import complete_attempt, email_hash, save_answer, save_feedback, start_attempt, subscriber_counts


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

    def create_real_subscriber(self, email="member@example.invalid", display_name="실제 구독자"):
        with transaction(self.db_path) as conn:
            return conn.execute(
                """INSERT INTO subscribers(public_id,display_name,email_hash,is_test,created_at)
                   VALUES(?,?,?,0,?)""",
                ("sub_real_member", display_name, email_hash(email, "migration-test-secret"), utcnow()),
            ).lastrowid

    def production_client(self, sender):
        app = create_app({
            "TESTING": True,
            "SECRET_KEY": "production-test-secret",
            "ADMIN_PASSWORD": "admin-test",
            "DB_PATH": self.db_path,
            "ENABLE_TEST_IDENTITY": False,
            "SEED_DEMO_DATA": False,
            "MIGRATION_HASH_SECRET": "migration-test-secret",
            "PUBLIC_BASE_URL": "https://quiz.example.test",
            "MAGIC_LINK_TTL_MINUTES": 15,
            "MAGIC_LINK_REQUEST_COOLDOWN_SECONDS": 0,
            "MAGIC_LINK_SENDER": sender,
            "PERMANENT_SESSION_LIFETIME": timedelta(days=180),
        })
        return app, app.test_client()

    @staticmethod
    def set_csrf(client, value="auth-csrf"):
        with client.session_transaction() as state:
            state["csrf_token"] = value
        return value

    @staticmethod
    def quiz_csv(code, episode_number, question_count, choice_count=4, explanation=True):
        output = io.StringIO()
        fields = [
            "R코드", "회차", "Form 제목", "문항", "질문", "선택지(JSON)",
            "정답", "정답 해설", "배점", "Form ID",
        ]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for order in range(1, question_count + 1):
            choices = [f"{order}번 선택지 {index}" for index in range(1, choice_count + 1)]
            writer.writerow({
                "R코드": code,
                "회차": episode_number,
                "Form 제목": f"[{code}] 반려견 영양 오디오레터 {episode_number}회차 퀴즈",
                "문항": order,
                "질문": f"{code} 질문 {order}",
                "선택지(JSON)": json.dumps(choices, ensure_ascii=False),
                "정답": choices[-1],
                "정답 해설": f"{order}번 해설" if explanation else "",
                "배점": order,
                "Form ID": f"form-{code}",
            })
        return output.getvalue()

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
        response = client.get("/quiz?episode=R041")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/auth/email", response.headers["Location"])
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

    def test_21_registered_and_unknown_email_have_same_external_response(self):
        self.create_real_subscriber()
        sent = []
        _, client = self.production_client(lambda email, url, config: sent.append((email, url)))
        csrf = self.set_csrf(client)
        known = client.post(
            "/auth/email",
            data={"csrf_token": csrf, "email": "member@example.invalid", "next": "/quiz?episode=R041"},
        )
        unknown = client.post(
            "/auth/email",
            data={"csrf_token": csrf, "email": "unknown@example.invalid", "next": "/quiz?episode=R041"},
        )
        self.assertEqual(known.status_code, 200)
        self.assertEqual(known.get_data(), unknown.get_data())
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "member@example.invalid")

    def test_22_magic_link_authenticates_once_and_returns_to_original_episode(self):
        subscriber_id = self.create_real_subscriber()
        sent = []
        app, client = self.production_client(lambda email, url, config: sent.append(url))
        csrf = self.set_csrf(client)
        client.post(
            "/auth/email",
            data={"csrf_token": csrf, "email": "member@example.invalid", "next": "/quiz?episode=R041"},
        )
        token = parse_qs(urlsplit(sent[0]).query)["token"][0]
        conn = connect(self.db_path)
        stored_token = conn.execute(
            "SELECT token_hash FROM magic_link_tokens WHERE subscriber_id=?", (subscriber_id,)
        ).fetchone()[0]
        conn.close()
        self.assertNotEqual(stored_token, token)
        verified = client.get(f"/auth/verify?token={token}")
        self.assertEqual(verified.status_code, 302)
        self.assertEqual(verified.headers["Location"], "/quiz?episode=R041")
        self.assertEqual(verified.headers["Cache-Control"], "no-store")
        self.assertEqual(verified.headers["Referrer-Policy"], "no-referrer")
        with client.session_transaction() as state:
            self.assertEqual(state["subscriber_id"], subscriber_id)
            self.assertTrue(state.permanent)
        self.assertEqual(app.permanent_session_lifetime, timedelta(days=180))
        self.assertEqual(client.get(f"/auth/verify?token={token}").status_code, 400)

    def test_23_expired_and_invalid_magic_links_are_rejected(self):
        subscriber_id = self.create_real_subscriber()
        _, client = self.production_client(lambda email, url, config: None)
        raw_token = create_magic_link_token(self.db_path, subscriber_id, "/", 15)
        with transaction(self.db_path) as conn:
            conn.execute(
                "UPDATE magic_link_tokens SET expires_at=? WHERE subscriber_id=?",
                ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), subscriber_id),
            )
        self.assertEqual(client.get(f"/auth/verify?token={raw_token}").status_code, 400)
        self.assertEqual(client.get("/auth/verify?token=not-a-real-token").status_code, 400)

    def test_24_external_redirect_is_replaced_with_internal_home(self):
        self.create_real_subscriber()
        sent = []
        _, client = self.production_client(lambda email, url, config: sent.append(url))
        csrf = self.set_csrf(client)
        client.post(
            "/auth/email",
            data={"csrf_token": csrf, "email": "member@example.invalid", "next": "https://evil.example/phish"},
        )
        token = parse_qs(urlsplit(sent[0]).query)["token"][0]
        verified = client.get(f"/auth/verify?token={token}")
        self.assertEqual(verified.headers["Location"], "/")

    def test_25_delivery_failure_is_not_reported_as_success_and_token_is_invalidated(self):
        subscriber_id = self.create_real_subscriber()

        def fail_sender(email, url, config):
            raise MagicLinkDeliveryError("simulated provider failure")

        _, client = self.production_client(fail_sender)
        csrf = self.set_csrf(client)
        response = client.post(
            "/auth/email",
            data={"csrf_token": csrf, "email": "member@example.invalid", "next": "/quiz?episode=R041"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("인증 메일을 보내지 못했습니다", response.get_data(as_text=True))
        conn = connect(self.db_path)
        token_row = conn.execute(
            "SELECT used_at FROM magic_link_tokens WHERE subscriber_id=? ORDER BY id DESC LIMIT 1",
            (subscriber_id,),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(token_row["used_at"])

    def test_26_admin_can_register_subscriber_without_storing_plain_email(self):
        with self.client.session_transaction() as state:
            state["is_admin"] = True
            state["csrf_token"] = "admin-csrf"
        response = self.client.post(
            "/admin/subscribers/new",
            data={
                "csrf_token": "admin-csrf",
                "email": "new-member@example.invalid",
                "display_name": "새 구독자",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        conn = connect(self.db_path)
        person = conn.execute(
            "SELECT * FROM subscribers WHERE display_name='새 구독자'"
        ).fetchone()
        conn.close()
        self.assertTrue(person["public_id"].startswith("sub_"))
        self.assertEqual(
            person["email_hash"], email_hash("new-member@example.invalid", "migration-test-secret")
        )
        self.assertNotEqual(person["public_id"], "new-member@example.invalid")

    def test_27_legacy_count_adds_only_to_lifetime_total(self):
        subscriber, episode, questions = self.ids()
        self.finish_attempt(subscriber, episode, questions)
        with self.client.session_transaction() as state:
            state["is_admin"] = True
            state["csrf_token"] = "legacy-csrf"
        response = self.client.post(
            f"/admin/subscribers/{subscriber}",
            data={"csrf_token": "legacy-csrf", "participation_count": "18", "note": "시즌1 수동 반영"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        conn = connect(self.db_path)
        counts = subscriber_counts(conn, subscriber, episode["season_id"])
        actual_participation = conn.execute(
            "SELECT COUNT(*) FROM participation WHERE subscriber_id=?", (subscriber,)
        ).fetchone()[0]
        legacy = conn.execute(
            "SELECT participation_count,note FROM legacy_participation WHERE subscriber_id=?",
            (subscriber,),
        ).fetchone()
        conn.close()
        self.assertEqual(counts, {"season": 1, "total": 19})
        self.assertEqual(actual_participation, 1)
        self.assertEqual(legacy["participation_count"], 18)
        self.assertEqual(legacy["note"], "시즌1 수동 반영")
        with self.client.session_transaction() as state:
            state["subscriber_id"] = subscriber
        history = self.client.get("/me").get_data(as_text=True)
        self.assertIn("누적 19회", history)
        self.assertIn("시즌1 과거 참여 18회", history)

    def test_28_existing_database_rows_survive_additive_schema_upgrade(self):
        legacy_db = str(Path(self.temp.name) / "existing.db")
        raw = sqlite3.connect(legacy_db)
        raw.execute(
            """CREATE TABLE subscribers (
               id INTEGER PRIMARY KEY AUTOINCREMENT, public_id TEXT NOT NULL UNIQUE,
               display_name TEXT, email_hash TEXT UNIQUE, is_test INTEGER NOT NULL DEFAULT 0,
               created_at TEXT NOT NULL)"""
        )
        raw.execute(
            "INSERT INTO subscribers(public_id,display_name,is_test,created_at) VALUES('kept-row','보존 대상',0,?)",
            (utcnow(),),
        )
        raw.commit()
        raw.close()
        create_app({
            "TESTING": True,
            "SECRET_KEY": "upgrade-test",
            "DB_PATH": legacy_db,
            "ENABLE_TEST_IDENTITY": False,
            "SEED_DEMO_DATA": False,
        })
        conn = connect(legacy_db)
        kept = conn.execute("SELECT display_name FROM subscribers WHERE public_id='kept-row'").fetchone()
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        self.assertEqual(kept["display_name"], "보존 대상")
        self.assertIn("magic_link_tokens", tables)
        self.assertIn("legacy_participation", tables)

    def test_29_brevo_transactional_payload_matches_api_contract(self):
        class FakeResponse:
            status = 201

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        config = {
            "BREVO_API_KEY": "test-api-key",
            "MAGIC_LINK_SENDER_EMAIL": "sender@example.invalid",
            "MAGIC_LINK_SENDER_NAME": "반려견영양연구소",
            "BREVO_TIMEOUT_SECONDS": 10,
        }
        with patch("magic_links.urllib.request.urlopen", return_value=FakeResponse()) as mocked:
            send_magic_link_via_brevo(
                "member@example.invalid",
                "https://quiz.example.test/auth/verify?token=safe-test-token",
                config,
            )
        request = mocked.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://api.brevo.com/v3/smtp/email")
        self.assertEqual(request.get_header("Api-key"), "test-api-key")
        self.assertEqual(payload["sender"]["email"], "sender@example.invalid")
        self.assertEqual(payload["to"], [{"email": "member@example.invalid"}])
        self.assertIn("htmlContent", payload)

    def test_30_test_identity_picker_still_authenticates_test_subscriber(self):
        subscriber, _, _ = self.ids()
        page = self.client.get("/test-identity?next=/quiz?episode=R041")
        self.assertEqual(page.status_code, 200)
        self.assertIn("테스트 구독자 A", page.get_data(as_text=True))
        csrf = self.set_csrf(self.client, "test-identity-csrf")
        selected = self.client.post(
            "/test-identity",
            data={
                "csrf_token": csrf,
                "subscriber_id": subscriber,
                "next": "/quiz?episode=R041",
            },
        )
        self.assertEqual(selected.status_code, 302)
        self.assertEqual(selected.headers["Location"], "/quiz?episode=R041")
        with self.client.session_transaction() as state:
            self.assertEqual(state["subscriber_id"], subscriber)
            self.assertTrue(state.permanent)

    def test_31_subscriber_list_has_new_registration_button_and_form(self):
        with self.client.session_transaction() as state:
            state["is_admin"] = True
        listing = self.client.get("/admin/subscribers")
        form = self.client.get("/admin/subscribers/new")
        self.assertEqual(listing.status_code, 200)
        self.assertIn("새 구독자 등록", listing.get_data(as_text=True))
        self.assertEqual(form.status_code, 200)
        self.assertIn('name="email"', form.get_data(as_text=True))
        self.assertIn('name="display_name"', form.get_data(as_text=True))

    def test_32_duplicate_subscriber_email_is_blocked(self):
        with self.client.session_transaction() as state:
            state["is_admin"] = True
            state["csrf_token"] = "duplicate-csrf"
        data = {
            "csrf_token": "duplicate-csrf",
            "email": "duplicate@example.invalid",
            "display_name": "중복 확인",
        }
        first = self.client.post("/admin/subscribers/new", data=data)
        second = self.client.post("/admin/subscribers/new", data=data, follow_redirects=True)
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 200)
        self.assertIn("이미 등록된 이메일입니다.", second.get_data(as_text=True))
        conn = connect(self.db_path)
        digest = email_hash("duplicate@example.invalid", "migration-test-secret")
        count = conn.execute(
            "SELECT COUNT(*) FROM subscribers WHERE email_hash=?", (digest,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_33_admin_registered_email_is_found_by_magic_link_lookup(self):
        with self.client.session_transaction() as state:
            state["is_admin"] = True
            state["csrf_token"] = "lookup-csrf"
        self.client.post(
            "/admin/subscribers/new",
            data={
                "csrf_token": "lookup-csrf",
                "email": "lookup@example.invalid",
                "display_name": "Magic Link 확인",
            },
        )
        sent = []
        _, production_client = self.production_client(
            lambda email, url, config: sent.append((email, url))
        )
        csrf = self.set_csrf(production_client, "magic-lookup-csrf")
        response = production_client.post(
            "/auth/email",
            data={
                "csrf_token": csrf,
                "email": "lookup@example.invalid",
                "next": "/quiz?episode=R041",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "lookup@example.invalid")

    def test_34_existing_test_subscribers_remain_unchanged(self):
        conn = connect(self.db_path)
        people = conn.execute(
            """SELECT public_id,is_test,email_hash FROM subscribers
               WHERE public_id IN ('test-alpha','test-beta') ORDER BY public_id"""
        ).fetchall()
        conn.close()
        self.assertEqual([row["public_id"] for row in people], ["test-alpha", "test-beta"])
        self.assertTrue(all(row["is_test"] == 1 for row in people))
        self.assertTrue(all(row["email_hash"] is None for row in people))

    def test_35_imports_two_question_episode(self):
        rows, errors = parse_quiz_csv(self.quiz_csv("R001", 1, 2))
        self.assertEqual(errors, [])
        result = import_quiz_rows(self.db_path, rows)
        conn = connect(self.db_path)
        episode = conn.execute("SELECT * FROM episodes WHERE code='R001'").fetchone()
        count = conn.execute(
            "SELECT COUNT(*) FROM questions WHERE episode_id=?", (episode["id"],)
        ).fetchone()[0]
        feedback_count = conn.execute(
            "SELECT COUNT(*) FROM feedback_questions WHERE episode_id=?", (episode["id"],)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(result["questions_created"], 2)
        self.assertEqual(count, 2)
        self.assertEqual(episode["season_id"], self.ids()[1]["season_id"])
        self.assertEqual(episode["is_published"], 0)
        self.assertEqual(feedback_count, 3)

    def test_36_imports_three_question_episode(self):
        rows, errors = parse_quiz_csv(self.quiz_csv("R002", 2, 3))
        self.assertEqual(errors, [])
        import_quiz_rows(self.db_path, rows)
        conn = connect(self.db_path)
        count = conn.execute(
            """SELECT COUNT(*) FROM questions q JOIN episodes e ON e.id=q.episode_id
               WHERE e.code='R002'"""
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 3)

    def test_37_imports_four_choices(self):
        rows, _ = parse_quiz_csv(self.quiz_csv("R003", 3, 1, choice_count=4))
        import_quiz_rows(self.db_path, rows)
        conn = connect(self.db_path)
        count = conn.execute(
            """SELECT COUNT(*) FROM choices c JOIN questions q ON q.id=c.question_id
               JOIN episodes e ON e.id=q.episode_id WHERE e.code='R003'"""
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 4)

    def test_38_imports_five_choices(self):
        rows, _ = parse_quiz_csv(self.quiz_csv("R004", 4, 1, choice_count=5))
        import_quiz_rows(self.db_path, rows)
        conn = connect(self.db_path)
        count = conn.execute(
            """SELECT COUNT(*) FROM choices c JOIN questions q ON q.id=c.question_id
               JOIN episodes e ON e.id=q.episode_id WHERE e.code='R004'"""
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 5)

    def test_39_correct_answer_matches_exact_choice_string(self):
        rows, _ = parse_quiz_csv(self.quiz_csv("R005", 5, 1))
        expected = rows[0]["answer"]
        import_quiz_rows(self.db_path, rows)
        conn = connect(self.db_path)
        correct = conn.execute(
            """SELECT c.text FROM choices c JOIN questions q ON q.id=c.question_id
               JOIN episodes e ON e.id=q.episode_id
               WHERE e.code='R005' AND c.is_correct=1"""
        ).fetchall()
        conn.close()
        self.assertEqual([row["text"] for row in correct], [expected])

    def test_40_imports_explanation_points_and_display_order(self):
        rows, _ = parse_quiz_csv(self.quiz_csv("R006", 6, 2))
        import_quiz_rows(self.db_path, rows)
        conn = connect(self.db_path)
        questions = conn.execute(
            """SELECT q.* FROM questions q JOIN episodes e ON e.id=q.episode_id
               WHERE e.code='R006' ORDER BY q.display_order"""
        ).fetchall()
        conn.close()
        self.assertEqual(
            [(q["display_order"], q["points"], q["explanation"]) for q in questions],
            [(1, 1, "1번 해설"), (2, 2, "2번 해설")],
        )

    def test_41_reimport_is_idempotent(self):
        rows, _ = parse_quiz_csv(self.quiz_csv("R007", 7, 2))
        first = import_quiz_rows(self.db_path, rows)
        second = import_quiz_rows(self.db_path, rows)
        conn = connect(self.db_path)
        question_count = conn.execute(
            """SELECT COUNT(*) FROM questions q JOIN episodes e ON e.id=q.episode_id
               WHERE e.code='R007'"""
        ).fetchone()[0]
        episode_count = conn.execute("SELECT COUNT(*) FROM episodes WHERE code='R007'").fetchone()[0]
        conn.close()
        self.assertEqual(first["questions_created"], 2)
        self.assertEqual(second["questions_created"], 0)
        self.assertEqual(second["questions_skipped"], 2)
        self.assertEqual((episode_count, question_count), (1, 2))

    def test_42_existing_episode_is_reused_without_changing_metadata(self):
        rows, _ = parse_quiz_csv(self.quiz_csv("R041", 41, 4))
        rows = [rows[-1]]
        conn = connect(self.db_path)
        before = conn.execute("SELECT id,title,is_published FROM episodes WHERE code='R041'").fetchone()
        conn.close()
        result = import_quiz_rows(self.db_path, rows)
        conn = connect(self.db_path)
        after = conn.execute("SELECT id,title,is_published FROM episodes WHERE code='R041'").fetchone()
        conn.close()
        self.assertEqual(result["episodes_created"], 0)
        self.assertEqual(tuple(before), tuple(after))

    def test_43_invalid_choices_json_is_rejected(self):
        text = self.quiz_csv("R008", 8, 1).replace(
            '"[""1번 선택지 1"", ""1번 선택지 2"", ""1번 선택지 3"", ""1번 선택지 4""]"',
            'not-json',
        )
        rows, errors = parse_quiz_csv(text)
        self.assertEqual(rows, [])
        self.assertTrue(any("JSON" in error["message"] for error in errors))

    def test_44_answer_not_in_choices_is_rejected(self):
        text = self.quiz_csv("R009", 9, 1).replace("1번 선택지 4", "존재하지 않는 정답", 1)
        rows, errors = parse_quiz_csv(text)
        self.assertEqual(rows, [])
        self.assertTrue(any("정답은 선택지" in error["message"] for error in errors))

    def test_45_existing_participation_and_feedback_survive_import(self):
        subscriber, episode, questions = self.ids()
        self.finish_attempt(subscriber, episode, questions)
        conn = connect(self.db_path)
        feedback_questions = conn.execute(
            "SELECT id,response_type FROM feedback_questions WHERE episode_id=? ORDER BY display_order",
            (episode["id"],),
        ).fetchall()
        conn.close()
        save_feedback(
            self.db_path,
            episode["id"],
            subscriber,
            {feedback_questions[0]["id"]: ["핵심 개념"], feedback_questions[1]["id"]: "5"},
        )
        rows, _ = parse_quiz_csv(self.quiz_csv("R041", 41, 4))
        import_quiz_rows(self.db_path, [rows[-1]])
        conn = connect(self.db_path)
        participation_count = conn.execute("SELECT COUNT(*) FROM participation").fetchone()[0]
        feedback_count = conn.execute("SELECT COUNT(*) FROM feedback_submissions").fetchone()[0]
        conn.close()
        self.assertEqual(participation_count, 1)
        self.assertEqual(feedback_count, 1)

    def test_46_preview_reports_conflict_and_does_not_write(self):
        rows, _ = parse_quiz_csv(self.quiz_csv("R041", 41, 1))
        before = connect(self.db_path)
        question_count = before.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        before.close()
        preview = preview_quiz_import(self.db_path, rows)
        after = connect(self.db_path)
        self.assertEqual(after.execute("SELECT COUNT(*) FROM questions").fetchone()[0], question_count)
        after.close()
        self.assertEqual(len(preview["conflicts"]), 1)
        self.assertEqual(len(preview["new_questions"]), 0)

    def test_47_admin_preview_and_confirm_flow(self):
        with self.client.session_transaction() as state:
            state["is_admin"] = True
            state["csrf_token"] = "import-csrf"
        response = self.client.post(
            "/admin/quiz-import",
            data={
                "csrf_token": "import-csrf",
                "action": "preview",
                "csv_file": (io.BytesIO(self.quiz_csv("R010", 10, 2).encode("utf-8")), "quiz.csv"),
            },
            content_type="multipart/form-data",
        )
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("확인 후 2개 문항 Import", html)
        marker = 'name="payload" value="'
        payload = html.split(marker, 1)[1].split('"', 1)[0]
        imported = self.client.post(
            "/admin/quiz-import",
            data={"csrf_token": "import-csrf", "action": "import", "payload": payload},
            follow_redirects=True,
        )
        self.assertEqual(imported.status_code, 200)
        self.assertIn("Import 완료", imported.get_data(as_text=True))
        conn = connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM episodes WHERE code='R010'").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
