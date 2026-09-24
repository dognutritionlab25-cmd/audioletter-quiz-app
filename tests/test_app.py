import csv
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from botocore.exceptions import ClientError
from urllib.parse import parse_qs, urlsplit

from audioletters import accessible_audioletter_episode, episode_blocks
from audio_storage import AudioStorageUnavailable, audio_client
from app import create_app, create_default_feedback, seed_demo
from db import connect, transaction, utcnow
from importers import import_google_form_payload, migrate_anonymous_feedback, migrate_historical_responses
from magic_links import (
    MagicLinkDeliveryError,
    create_magic_link_token,
    send_community_post_notification_via_brevo,
    send_magic_link_via_brevo,
)
from presenters import feedback_summary, format_korean_datetime
from quiz_csv_import import import_quiz_rows, parse_quiz_csv, preview_quiz_import
from services import can_access_paid_audioletter, complete_attempt, email_hash, save_answer, save_feedback, start_attempt, subscriber_counts


ROOT = Path(__file__).resolve().parents[1]


class QuizAppTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.community_notifications = []
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "ADMIN_PASSWORD": "admin-test",
            "DB_PATH": self.db_path,
            "ENABLE_TEST_IDENTITY": True,
            "SEED_DEMO_DATA": False,
            "MIGRATION_HASH_SECRET": "migration-test-secret",
            "SUBSCRIBER_SYNC_API_KEY": "sync-test-secret",
            "SUBSCRIPTION_REGISTRATION_API_KEY": "registration-test-secret",
            "SUBSCRIPTION_REGISTRATION_ENCRYPTION_KEY": (
                "BVNXRQRh_hvpCeOkdlibHHdypT6aujBxlG5Oj_Rd7Ok="
            ),
            "COMMUNITY_NOTIFICATION_SENDER": (
                lambda *args: self.community_notifications.append(args)
            ),
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
            "PUBLIC_BASE_URL": "https://portal.dognutritionlab.com",
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

    def registration_form_data(self, **overrides):
        data = {
            "csrf_token": "registration-csrf",
            "plan_code": "three-month",
            "guardian_name": "보호자",
            "dog_name": "토리",
            "email": "member@example.invalid",
            "contact_phone": "010-1234-5678",
            "dog_birth_date": "2020-05-12",
            "dog_breed": "믹스견",
            "payer_name": "",
            "interests": "영양과 장 건강",
            "registration_type": "new",
            "privacy_agree": "yes",
        }
        data.update(overrides)
        return data

    def enable_subscription_registration(self):
        with transaction(self.db_path) as conn:
            conn.execute("UPDATE portal_settings SET subscription_page_enabled=1 WHERE id=1")
        self.set_csrf(self.client, "registration-csrf")

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

    def create_episode(self, code, published=True):
        episode_number = int(code[1:])
        with transaction(self.db_path) as conn:
            season_id = conn.execute(
                "SELECT id FROM seasons WHERE code='S1'"
            ).fetchone()[0]
            episode_id = conn.execute(
                """INSERT INTO episodes
                   (season_id,code,title,description,display_order,is_published,created_at,updated_at)
                   VALUES(?,?,?,'',?,?,?,?)""",
                (
                    season_id,
                    code,
                    f"{code} 운영 회차",
                    episode_number,
                    int(published),
                    utcnow(),
                    utcnow(),
                ),
            ).lastrowid
            question_id = conn.execute(
                """INSERT INTO questions
                   (episode_id,text,points,explanation,display_order)
                   VALUES(?,?,1,NULL,1)""",
                (episode_id, f"{code} 테스트 문항"),
            ).lastrowid
            conn.execute(
                "INSERT INTO choices(question_id,text,is_correct,display_order) VALUES(?,?,1,1)",
                (question_id, "정답"),
            )
            conn.execute(
                "INSERT INTO choices(question_id,text,is_correct,display_order) VALUES(?,?,0,2)",
                (question_id, "오답"),
            )
            create_default_feedback(conn, episode_id)
        return episode_id, [question_id]

    def create_resource(
        self,
        title="공개 자료",
        category="식단 가이드",
        body="자료 본문",
        external_url="https://example.invalid/resource",
        published=True,
        created_at=None,
    ):
        created_at = created_at or utcnow()
        with transaction(self.db_path) as conn:
            return conn.execute(
                """INSERT INTO resources
                   (title,body,category,external_url,is_published,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    title, body, category, external_url,
                    int(published), created_at, created_at,
                ),
            ).lastrowid

    def create_community_subscriber(
        self, email, display_name, paid=True, active=True
    ):
        public_id = "sub_" + email.split("@", 1)[0].replace(".", "_")
        with transaction(self.db_path) as conn:
            return conn.execute(
                """INSERT INTO subscribers
                   (public_id,display_name,email_hash,is_test,is_active,is_paid_subscriber,created_at)
                   VALUES(?,?,?,0,?,?,?)""",
                (
                    public_id,
                    display_name,
                    email_hash(email, "migration-test-secret"),
                    int(active),
                    int(paid),
                    utcnow(),
                ),
            ).lastrowid

    def login_community_subscriber(self, subscriber_id, csrf="community-csrf"):
        with self.client.session_transaction() as state:
            state["subscriber_id"] = subscriber_id
            state["csrf_token"] = csrf
        return csrf

    def create_community_post(self, subscriber_id, title="게시글", visible=True):
        with transaction(self.db_path) as conn:
            return conn.execute(
                """INSERT INTO community_posts
                   (subscriber_id,title,body,is_visible,created_at,updated_at)
                   VALUES(?,?,?, ?,?,?)""",
                (
                    subscriber_id,
                    title,
                    "게시글 본문",
                    int(visible),
                    utcnow(),
                    utcnow(),
                ),
            ).lastrowid

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
                "is_active": "on",
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
                "https://portal.dognutritionlab.com/auth/verify?token=safe-test-token",
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
        self.assertIn('name="is_active"', form.get_data(as_text=True))

    def test_32_duplicate_subscriber_email_is_blocked(self):
        with self.client.session_transaction() as state:
            state["is_admin"] = True
            state["csrf_token"] = "duplicate-csrf"
        data = {
            "csrf_token": "duplicate-csrf",
            "email": "duplicate@example.invalid",
            "display_name": "중복 확인",
            "is_active": "on",
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
                "is_active": "on",
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

    def test_48_admin_question_delete_preserves_participation_and_feedback(self):
        subscriber, episode, questions = self.ids()
        attempt_id, _ = self.finish_attempt(subscriber, episode, questions)
        conn = connect(self.db_path)
        feedback_questions = conn.execute(
            "SELECT id,response_type FROM feedback_questions WHERE episode_id=? ORDER BY display_order",
            (episode["id"],),
        ).fetchall()
        deleted_question_choices = conn.execute(
            "SELECT id FROM choices WHERE question_id=?", (questions[0],)
        ).fetchall()
        conn.close()
        save_feedback(
            self.db_path,
            episode["id"],
            subscriber,
            {feedback_questions[0]["id"]: ["핵심 개념"], feedback_questions[1]["id"]: "5"},
        )

        with self.client.session_transaction() as state:
            state["is_admin"] = True
            state["csrf_token"] = "delete-question-csrf"
        response = self.client.post(
            f"/admin/questions/{questions[0]}/delete",
            data={"csrf_token": "delete-question-csrf"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/admin/episodes/{episode['id']}", response.headers["Location"])

        conn = connect(self.db_path)
        self.assertIsNone(
            conn.execute("SELECT id FROM questions WHERE id=?", (questions[0],)).fetchone()
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM choices WHERE question_id=?", (questions[0],)).fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM attempt_answers WHERE question_id=?", (questions[0],)).fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM attempt_answers WHERE attempt_id=?", (attempt_id,)).fetchone()[0],
            len(questions) - 1,
        )
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM quiz_attempts").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM participation").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM feedback_submissions").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM feedback_answers").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM episodes WHERE id=?", (episode["id"],)).fetchone()[0], 1)
        self.assertEqual(
            conn.execute(
                f"SELECT COUNT(*) FROM choices WHERE id IN ({','.join('?' for _ in deleted_question_choices)})",
                tuple(row["id"] for row in deleted_question_choices),
            ).fetchone()[0],
            0,
        )
        conn.close()

    def test_49_magic_link_email_is_normalized(self):
        self.create_real_subscriber(email="member@example.invalid")
        sent = []
        _, client = self.production_client(
            lambda email, url, config: sent.append((email, url))
        )
        csrf = self.set_csrf(client, "normalize-csrf")
        response = client.post(
            "/auth/email",
            data={
                "csrf_token": csrf,
                "email": "  MEMBER@EXAMPLE.INVALID  ",
                "next": "/quiz?episode=R041",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "member@example.invalid")

    def test_50_inactive_subscriber_gets_generic_response_without_email(self):
        subscriber_id = self.create_real_subscriber()
        with transaction(self.db_path) as conn:
            conn.execute(
                "UPDATE subscribers SET is_active=0 WHERE id=?", (subscriber_id,)
            )
        sent = []
        _, client = self.production_client(
            lambda email, url, config: sent.append((email, url))
        )
        csrf = self.set_csrf(client, "inactive-request-csrf")
        inactive = client.post(
            "/auth/email",
            data={
                "csrf_token": csrf,
                "email": "member@example.invalid",
                "next": "/quiz?episode=R041",
            },
        )
        unknown = client.post(
            "/auth/email",
            data={
                "csrf_token": csrf,
                "email": "unknown@example.invalid",
                "next": "/quiz?episode=R041",
            },
        )
        self.assertEqual(inactive.get_data(), unknown.get_data())
        self.assertEqual(sent, [])

    def test_51_inactive_subscriber_cannot_verify_or_reuse_session(self):
        subscriber_id = self.create_real_subscriber()
        sent = []
        _, client = self.production_client(
            lambda email, url, config: sent.append(url)
        )
        csrf = self.set_csrf(client, "inactive-verify-csrf")
        client.post(
            "/auth/email",
            data={
                "csrf_token": csrf,
                "email": "member@example.invalid",
                "next": "/quiz?episode=R041",
            },
        )
        token = parse_qs(urlsplit(sent[0]).query)["token"][0]
        with transaction(self.db_path) as conn:
            conn.execute(
                "UPDATE subscribers SET is_active=0 WHERE id=?", (subscriber_id,)
            )
        self.assertEqual(client.get(f"/auth/verify?token={token}").status_code, 400)

        with client.session_transaction() as state:
            state["subscriber_id"] = subscriber_id
        blocked = client.get("/quiz?episode=R041")
        self.assertEqual(blocked.status_code, 302)
        self.assertIn("/auth/email", blocked.headers["Location"])
        with client.session_transaction() as state:
            self.assertNotIn("subscriber_id", state)

    def test_52_admin_registration_and_status_control_active_subscriber(self):
        with self.client.session_transaction() as state:
            state["is_admin"] = True
            state["csrf_token"] = "status-admin-csrf"
        created = self.client.post(
            "/admin/subscribers/new",
            data={
                "csrf_token": "status-admin-csrf",
                "email": "status@example.invalid",
                "display_name": "상태 확인",
                "is_active": "on",
            },
        )
        self.assertEqual(created.status_code, 302)
        conn = connect(self.db_path)
        person = conn.execute(
            "SELECT id,is_active FROM subscribers WHERE email_hash=?",
            (email_hash("status@example.invalid", "migration-test-secret"),),
        ).fetchone()
        conn.close()
        self.assertEqual(person["is_active"], 1)

        disabled = self.client.post(
            f"/admin/subscribers/{person['id']}",
            data={"csrf_token": "status-admin-csrf", "action": "status"},
        )
        self.assertEqual(disabled.status_code, 302)
        conn = connect(self.db_path)
        self.assertEqual(
            conn.execute(
                "SELECT is_active FROM subscribers WHERE id=?", (person["id"],)
            ).fetchone()[0],
            0,
        )
        conn.close()

    def test_53_additive_active_migration_preserves_existing_rows(self):
        legacy_db = str(Path(self.temp.name) / "subscriber-active-upgrade.db")
        raw = sqlite3.connect(legacy_db)
        raw.execute(
            """CREATE TABLE subscribers (
               id INTEGER PRIMARY KEY AUTOINCREMENT, public_id TEXT NOT NULL UNIQUE,
               display_name TEXT, email_hash TEXT UNIQUE,
               is_test INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)"""
        )
        raw.execute(
            """INSERT INTO subscribers(public_id,display_name,email_hash,is_test,created_at)
               VALUES('existing-production','기존 운영 구독자','kept-hash',0,?)""",
            (utcnow(),),
        )
        raw.commit()
        raw.close()

        create_app({
            "TESTING": True,
            "SECRET_KEY": "additive-test",
            "DB_PATH": legacy_db,
            "ENABLE_TEST_IDENTITY": False,
            "SEED_DEMO_DATA": False,
        })
        conn = connect(legacy_db)
        person = conn.execute(
            "SELECT * FROM subscribers WHERE public_id='existing-production'"
        ).fetchone()
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(subscribers)")
        }
        conn.close()
        self.assertIn("is_active", columns)
        self.assertEqual(person["display_name"], "기존 운영 구독자")
        self.assertEqual(person["email_hash"], "kept-hash")
        self.assertEqual(person["is_active"], 1)

    def test_54_schema_initialization_preserves_quiz_participation_and_feedback(self):
        subscriber, episode, questions = self.ids()
        self.finish_attempt(subscriber, episode, questions)
        conn = connect(self.db_path)
        feedback_questions = conn.execute(
            "SELECT id FROM feedback_questions WHERE episode_id=? ORDER BY display_order",
            (episode["id"],),
        ).fetchall()
        conn.close()
        save_feedback(
            self.db_path,
            episode["id"],
            subscriber,
            {feedback_questions[0]["id"]: ["핵심 개념"]},
        )
        conn = connect(self.db_path)
        tables = (
            "subscribers", "episodes", "questions", "choices", "quiz_attempts",
            "attempt_answers", "participation", "feedback_submissions", "feedback_answers",
        )
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }
        conn.close()

        from db import init_db
        init_db(self.db_path)

        conn = connect(self.db_path)
        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }
        conn.close()
        self.assertEqual(after, before)

    def test_55_home_does_not_expose_public_episode_list(self):
        self.create_episode("R018", published=True)
        self.create_episode("R042", published=True)
        response = self.client.get("/")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("오디오레터에서 안내된 회차", html)
        self.assertNotIn("R018", html)
        self.assertNotIn("R042", html)
        self.assertNotIn("/quiz?episode=", html)

    def test_56_authenticated_subscriber_can_open_each_fixed_episode_url(self):
        subscriber, _, _ = self.ids()
        self.create_episode("R018", published=True)
        self.create_episode("R042", published=True)
        with self.client.session_transaction() as state:
            state["subscriber_id"] = subscriber
        r018 = self.client.get("/quiz?episode=R018")
        r042 = self.client.get("/quiz?episode=R042")
        self.assertEqual(r018.status_code, 200)
        self.assertEqual(r042.status_code, 200)
        self.assertIn("R018 이해 테스트", r018.get_data(as_text=True))
        self.assertIn("R042 이해 테스트", r042.get_data(as_text=True))

    def test_57_magic_link_returns_to_requested_r018_episode(self):
        self.create_episode("R018", published=True)
        subscriber_id = self.create_real_subscriber()
        sent = []
        _, client = self.production_client(
            lambda email, url, config: sent.append(url)
        )

        entry = client.get("/quiz?episode=R018")
        self.assertEqual(entry.status_code, 302)
        login_url = entry.headers["Location"]
        self.assertIn("/auth/email", login_url)
        next_path = parse_qs(urlsplit(login_url).query)["next"][0]
        self.assertEqual(next_path, "/quiz?episode=R018")

        csrf = self.set_csrf(client, "r018-magic-csrf")
        requested = client.post(
            "/auth/email",
            data={
                "csrf_token": csrf,
                "email": "member@example.invalid",
                "next": next_path,
            },
        )
        self.assertEqual(requested.status_code, 200)
        token = parse_qs(urlsplit(sent[0]).query)["token"][0]
        verified = client.get(f"/auth/verify?token={token}")
        self.assertEqual(verified.status_code, 302)
        self.assertEqual(verified.headers["Location"], "/quiz?episode=R018")
        with client.session_transaction() as state:
            self.assertEqual(state["subscriber_id"], subscriber_id)
            self.assertTrue(state.permanent)
        self.assertEqual(client.get(verified.headers["Location"]).status_code, 200)

    def test_58_unpublished_episode_fixed_url_is_blocked(self):
        subscriber, _, _ = self.ids()
        self.create_episode("R018", published=False)
        with self.client.session_transaction() as state:
            state["subscriber_id"] = subscriber
        self.assertEqual(self.client.get("/quiz?episode=R018").status_code, 404)

    def test_59_history_shows_completed_episode_only(self):
        subscriber, _, _ = self.ids()
        r018_id, r018_questions = self.create_episode("R018", published=True)
        self.create_episode("R042", published=True)
        self.finish_attempt(
            subscriber,
            {"id": r018_id},
            r018_questions,
        )
        with self.client.session_transaction() as state:
            state["subscriber_id"] = subscriber
        history = self.client.get("/me")
        html = history.get_data(as_text=True)
        self.assertEqual(history.status_code, 200)
        self.assertIn("R018", html)
        self.assertNotIn("R042", html)

    def test_60_admin_still_sees_all_episodes(self):
        self.create_episode("R018", published=True)
        self.create_episode("R042", published=True)
        with self.client.session_transaction() as state:
            state["is_admin"] = True
        response = self.client.get("/admin")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("R018", html)
        self.assertIn("R042", html)

    def test_61_subscriber_sync_creates_real_subscriber_without_plaintext_email(self):
        response = self.client.post(
            "/api/subscribers/sync",
            headers={"Authorization": "Bearer sync-test-secret"},
            json={
                "email": "new-sync@example.invalid",
                "display_name": "신규 동기화 구독자",
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["status"], "created")

        digest = email_hash("new-sync@example.invalid", "migration-test-secret")
        conn = connect(self.db_path)
        person = conn.execute(
            "SELECT * FROM subscribers WHERE email_hash=?", (digest,)
        ).fetchone()
        stored_text = " ".join(
            str(value) for value in person if value is not None
        )
        conn.close()
        self.assertIsNotNone(person)
        self.assertEqual(person["display_name"], "신규 동기화 구독자")
        self.assertEqual(person["is_test"], 0)
        self.assertEqual(person["is_active"], 1)
        self.assertNotIn("new-sync@example.invalid", stored_text)

    def test_62_subscriber_sync_is_idempotent_and_normalizes_email(self):
        headers = {"Authorization": "Bearer sync-test-secret"}
        first = self.client.post(
            "/api/subscribers/sync",
            headers=headers,
            json={"email": " Repeat@Example.Invalid ", "display_name": "처음 이름"},
        )
        second = self.client.post(
            "/api/subscribers/sync",
            headers=headers,
            json={"email": "repeat@example.invalid", "display_name": "덮어쓸 이름"},
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json()["status"], "unchanged")
        self.assertEqual(
            first.get_json()["subscriber"]["public_id"],
            second.get_json()["subscriber"]["public_id"],
        )

        digest = email_hash("repeat@example.invalid", "migration-test-secret")
        conn = connect(self.db_path)
        people = conn.execute(
            "SELECT id,display_name FROM subscribers WHERE email_hash=?", (digest,)
        ).fetchall()
        conn.close()
        self.assertEqual(len(people), 1)
        self.assertEqual(people[0]["display_name"], "처음 이름")

    def test_63_subscriber_sync_preserves_existing_identity_and_participation(self):
        subscriber_id = self.create_real_subscriber(
            "existing-sync@example.invalid", "관리자 지정 이름"
        )
        _, episode, questions = self.ids()
        self.finish_attempt(subscriber_id, episode, questions)
        conn = connect(self.db_path)
        participation_before = conn.execute(
            "SELECT id,first_completed_at FROM participation WHERE subscriber_id=?",
            (subscriber_id,),
        ).fetchone()
        conn.close()

        response = self.client.post(
            "/api/subscribers/sync",
            headers={"Authorization": "Bearer sync-test-secret"},
            json={
                "email": " EXISTING-SYNC@example.invalid ",
                "display_name": "자동화 이름",
                "active": False,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "updated")

        conn = connect(self.db_path)
        person = conn.execute(
            "SELECT id,display_name,is_active FROM subscribers WHERE id=?",
            (subscriber_id,),
        ).fetchone()
        participation_after = conn.execute(
            "SELECT id,first_completed_at FROM participation WHERE subscriber_id=?",
            (subscriber_id,),
        ).fetchone()
        conn.close()
        self.assertEqual(person["id"], subscriber_id)
        self.assertEqual(person["display_name"], "관리자 지정 이름")
        self.assertEqual(person["is_active"], 0)
        self.assertEqual(dict(participation_after), dict(participation_before))

    def test_64_subscriber_sync_rejects_bad_auth_and_invalid_input(self):
        missing = self.client.post(
            "/api/subscribers/sync", json={"email": "member@example.invalid"}
        )
        wrong = self.client.post(
            "/api/subscribers/sync",
            headers={"Authorization": "Bearer wrong-secret"},
            json={"email": "member@example.invalid"},
        )
        invalid_email = self.client.post(
            "/api/subscribers/sync",
            headers={"Authorization": "Bearer sync-test-secret"},
            json={"email": "not-an-email"},
        )
        invalid_active = self.client.post(
            "/api/subscribers/sync",
            headers={"Authorization": "Bearer sync-test-secret"},
            json={"email": "member@example.invalid", "active": "true"},
        )
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(invalid_email.status_code, 400)
        self.assertEqual(invalid_active.status_code, 400)

        conn = connect(self.db_path)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM subscribers WHERE email_hash=?",
                (email_hash("member@example.invalid", "migration-test-secret"),),
            ).fetchone()[0],
            0,
        )
        conn.close()

    def test_65_admin_publishes_only_r001_through_r042_idempotently(self):
        for number in range(1, 43):
            code = f"R{number:03d}"
            if code != "R041":
                self.create_episode(code, published=False)

        subscriber, episode, questions = self.ids()
        attempt_id, _ = self.finish_attempt(subscriber, episode, questions)
        conn = connect(self.db_path)
        feedback_question = conn.execute(
            "SELECT id FROM feedback_questions WHERE episode_id=? ORDER BY display_order LIMIT 1",
            (episode["id"],),
        ).fetchone()[0]
        conn.close()
        save_feedback(
            self.db_path,
            episode["id"],
            subscriber,
            {feedback_question: ["기존 피드백"]},
        )

        protected_tables = (
            "subscribers", "questions", "choices", "quiz_attempts",
            "attempt_answers", "participation", "feedback_questions",
            "feedback_options", "feedback_submissions", "feedback_answers",
        )
        conn = connect(self.db_path)
        before_counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in protected_tables
        }
        before_episode_metadata = {
            row["code"]: tuple(row[key] for key in (
                "id", "season_id", "title", "description", "display_order",
                "created_at", "updated_at",
            ))
            for row in conn.execute(
                """SELECT id,season_id,code,title,description,display_order,created_at,updated_at
                   FROM episodes WHERE code BETWEEN 'R001' AND 'R042'"""
            )
        }
        conn.close()

        with self.client.session_transaction() as state:
            state["is_admin"] = True
            state["csrf_token"] = "publish-season-one-csrf"

        first = self.client.post(
            "/admin/episodes/publish-season-1",
            data={"csrf_token": "publish-season-one-csrf"},
        )
        second = self.client.post(
            "/admin/episodes/publish-season-1",
            data={"csrf_token": "publish-season-one-csrf"},
        )
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)

        conn = connect(self.db_path)
        self.assertEqual(
            conn.execute(
                """SELECT COUNT(*) FROM episodes
                   WHERE code BETWEEN 'R001' AND 'R042' AND is_published=1"""
            ).fetchone()[0],
            42,
        )
        after_counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in protected_tables
        }
        after_episode_metadata = {
            row["code"]: tuple(row[key] for key in (
                "id", "season_id", "title", "description", "display_order",
                "created_at", "updated_at",
            ))
            for row in conn.execute(
                """SELECT id,season_id,code,title,description,display_order,created_at,updated_at
                   FROM episodes WHERE code BETWEEN 'R001' AND 'R042'"""
            )
        }
        self.assertIsNotNone(
            conn.execute("SELECT id FROM quiz_attempts WHERE id=?", (attempt_id,)).fetchone()
        )
        conn.close()
        self.assertEqual(after_counts, before_counts)
        self.assertEqual(after_episode_metadata, before_episode_metadata)

    def test_66_season_one_publish_requires_all_42_episodes(self):
        self.create_episode("R001", published=False)
        with self.client.session_transaction() as state:
            state["is_admin"] = True
            state["csrf_token"] = "publish-incomplete-season-csrf"

        response = self.client.post(
            "/admin/episodes/publish-season-1",
            data={"csrf_token": "publish-incomplete-season-csrf"},
        )
        self.assertEqual(response.status_code, 302)

        conn = connect(self.db_path)
        self.assertEqual(
            conn.execute(
                "SELECT is_published FROM episodes WHERE code='R001'"
            ).fetchone()[0],
            0,
        )
        conn.close()

    def test_67_unauthenticated_resource_list_requires_subscriber_login(self):
        response = self.client.get("/resources")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/test-identity", response.headers["Location"])
        self.assertIn("next=", response.headers["Location"])

    def test_68_real_subscriber_sees_published_resources_newest_first(self):
        older_id = self.create_resource(
            title="이전 공개 자료",
            created_at="2026-09-01T00:00:00+00:00",
        )
        newer_id = self.create_resource(
            title="최근 공개 자료",
            created_at="2026-09-20T00:00:00+00:00",
        )
        subscriber_id = self.create_real_subscriber()
        with self.client.session_transaction() as state:
            state["subscriber_id"] = subscriber_id

        response = self.client.get("/resources")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("이전 공개 자료", html)
        self.assertIn("최근 공개 자료", html)
        self.assertLess(html.index("최근 공개 자료"), html.index("이전 공개 자료"))
        self.assertIn(f"/resources/{older_id}", html)
        self.assertIn(f"/resources/{newer_id}", html)

    def test_69_unpublished_resource_is_hidden_from_subscriber_list(self):
        self.create_resource(title="공개 자료", published=True)
        self.create_resource(title="관리자 초안", published=False)
        subscriber_id = self.create_real_subscriber()
        with self.client.session_transaction() as state:
            state["subscriber_id"] = subscriber_id

        response = self.client.get("/resources")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("공개 자료", html)
        self.assertNotIn("관리자 초안", html)

    def test_70_unpublished_resource_direct_url_returns_404(self):
        resource_id = self.create_resource(title="관리자 초안", published=False)
        subscriber_id = self.create_real_subscriber()
        with self.client.session_transaction() as state:
            state["subscriber_id"] = subscriber_id

        response = self.client.get(f"/resources/{resource_id}")

        self.assertEqual(response.status_code, 404)

    def test_71_admin_creates_resource_with_safe_external_link(self):
        with self.client.session_transaction() as state:
            state["is_admin"] = True
            state["csrf_token"] = "resource-create-csrf"

        response = self.client.post(
            "/admin/resources/new",
            data={
                "csrf_token": "resource-create-csrf",
                "title": "칼슘 자료",
                "category": "영양 가이드",
                "body": "칼슘과 인의 균형을 설명합니다.",
                "external_url": "https://example.invalid/calcium",
                "is_published": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        conn = connect(self.db_path)
        resource = conn.execute(
            "SELECT * FROM resources WHERE title='칼슘 자료'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(resource)
        self.assertEqual(resource["category"], "영양 가이드")
        self.assertEqual(resource["external_url"], "https://example.invalid/calcium")
        self.assertEqual(resource["is_published"], 1)

        subscriber_id = self.create_real_subscriber()
        with self.client.session_transaction() as state:
            state["subscriber_id"] = subscriber_id
        detail = self.client.get(f"/resources/{resource['id']}")
        html = detail.get_data(as_text=True)
        self.assertEqual(detail.status_code, 200)
        self.assertIn('rel="noopener noreferrer"', html)
        self.assertIn('referrerpolicy="no-referrer"', html)

    def test_72_admin_edits_existing_resource(self):
        resource_id = self.create_resource(title="수정 전", published=False)
        with self.client.session_transaction() as state:
            state["is_admin"] = True
            state["csrf_token"] = "resource-edit-csrf"

        response = self.client.post(
            f"/admin/resources/{resource_id}/edit",
            data={
                "csrf_token": "resource-edit-csrf",
                "title": "수정 후",
                "category": "관찰 가이드",
                "body": "수정된 본문",
                "external_url": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        conn = connect(self.db_path)
        resource = conn.execute(
            "SELECT * FROM resources WHERE id=?", (resource_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(resource["title"], "수정 후")
        self.assertEqual(resource["category"], "관찰 가이드")
        self.assertIsNone(resource["external_url"])
        self.assertEqual(resource["is_published"], 0)

    def test_73_admin_can_publish_and_unpublish_resource(self):
        resource_id = self.create_resource(published=False)
        with self.client.session_transaction() as state:
            state["is_admin"] = True
            state["csrf_token"] = "resource-publish-csrf"

        published = self.client.post(
            f"/admin/resources/{resource_id}/edit",
            data={
                "csrf_token": "resource-publish-csrf",
                "title": "공개 전환 자료",
                "category": "영양 가이드",
                "body": "본문",
                "external_url": "",
                "is_published": "on",
            },
        )
        unpublished = self.client.post(
            f"/admin/resources/{resource_id}/edit",
            data={
                "csrf_token": "resource-publish-csrf",
                "title": "비공개 전환 자료",
                "category": "영양 가이드",
                "body": "본문",
                "external_url": "",
            },
        )

        self.assertEqual(published.status_code, 302)
        self.assertEqual(unpublished.status_code, 302)
        conn = connect(self.db_path)
        self.assertEqual(
            conn.execute(
                "SELECT is_published FROM resources WHERE id=?", (resource_id,)
            ).fetchone()[0],
            0,
        )
        conn.close()

    def test_74_admin_deletes_only_selected_resource(self):
        deleted_id = self.create_resource(title="삭제 대상")
        kept_id = self.create_resource(title="보존 대상")
        with self.client.session_transaction() as state:
            state["is_admin"] = True
            state["csrf_token"] = "resource-delete-csrf"

        list_page = self.client.get("/admin/resources")
        self.assertIn("이 자료를 삭제하시겠습니까?", list_page.get_data(as_text=True))
        response = self.client.post(
            f"/admin/resources/{deleted_id}/delete",
            data={"csrf_token": "resource-delete-csrf"},
        )

        self.assertEqual(response.status_code, 302)
        conn = connect(self.db_path)
        self.assertIsNone(
            conn.execute("SELECT id FROM resources WHERE id=?", (deleted_id,)).fetchone()
        )
        self.assertIsNotNone(
            conn.execute("SELECT id FROM resources WHERE id=?", (kept_id,)).fetchone()
        )
        conn.close()

    def test_75_subscriber_cannot_access_admin_resource_crud(self):
        resource_id = self.create_resource()
        subscriber_id = self.create_real_subscriber()
        with self.client.session_transaction() as state:
            state["subscriber_id"] = subscriber_id
            state["csrf_token"] = "subscriber-resource-csrf"

        list_response = self.client.get("/admin/resources")
        new_response = self.client.get("/admin/resources/new")
        delete_response = self.client.post(
            f"/admin/resources/{resource_id}/delete",
            data={"csrf_token": "subscriber-resource-csrf"},
        )

        self.assertEqual(list_response.status_code, 302)
        self.assertIn("/admin/login", list_response.headers["Location"])
        self.assertEqual(new_response.status_code, 302)
        self.assertIn("/admin/login", new_response.headers["Location"])
        self.assertEqual(delete_response.status_code, 302)
        conn = connect(self.db_path)
        self.assertIsNotNone(
            conn.execute("SELECT id FROM resources WHERE id=?", (resource_id,)).fetchone()
        )
        conn.close()

    def test_76_resource_delete_preserves_quiz_participation_and_feedback(self):
        subscriber, episode, questions = self.ids()
        self.finish_attempt(subscriber, episode, questions)
        conn = connect(self.db_path)
        feedback_question = conn.execute(
            """SELECT id FROM feedback_questions
               WHERE episode_id=? ORDER BY display_order LIMIT 1""",
            (episode["id"],),
        ).fetchone()[0]
        conn.close()
        save_feedback(
            self.db_path,
            episode["id"],
            subscriber,
            {feedback_question: ["기존 피드백"]},
        )
        resource_id = self.create_resource(title="삭제해도 독립적인 자료")

        conn = connect(self.db_path)
        protected_tables = (
            "subscribers", "episodes", "questions", "choices", "quiz_attempts",
            "attempt_answers", "participation", "feedback_questions",
            "feedback_options", "feedback_submissions", "feedback_answers",
        )
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in protected_tables
        }
        conn.close()

        with self.client.session_transaction() as state:
            state["is_admin"] = True
            state["csrf_token"] = "resource-preserve-csrf"
        response = self.client.post(
            f"/admin/resources/{resource_id}/delete",
            data={"csrf_token": "resource-preserve-csrf"},
        )
        self.assertEqual(response.status_code, 302)

        conn = connect(self.db_path)
        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in protected_tables
        }
        conn.close()
        self.assertEqual(after, before)

        with self.client.session_transaction() as state:
            state["subscriber_id"] = subscriber
        self.assertEqual(self.client.get("/quiz?episode=R041").status_code, 200)
        self.assertEqual(self.client.get("/me").status_code, 200)

    def test_77_resource_external_url_rejects_non_http_scheme(self):
        with self.client.session_transaction() as state:
            state["is_admin"] = True
            state["csrf_token"] = "resource-url-csrf"

        response = self.client.post(
            "/admin/resources/new",
            data={
                "csrf_token": "resource-url-csrf",
                "title": "위험 링크",
                "category": "자료",
                "body": "본문",
                "external_url": "javascript:alert(1)",
                "is_published": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("올바른 http 또는 https", response.get_data(as_text=True))
        conn = connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0], 0)
        conn.close()

    def test_78_paid_migration_is_additive_and_creates_community_tables(self):
        legacy_db = str(Path(self.temp.name) / "paid-upgrade.db")
        raw = sqlite3.connect(legacy_db)
        raw.execute(
            """CREATE TABLE subscribers (
               id INTEGER PRIMARY KEY AUTOINCREMENT, public_id TEXT NOT NULL UNIQUE,
               display_name TEXT, email_hash TEXT UNIQUE, is_test INTEGER NOT NULL DEFAULT 0,
               is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)"""
        )
        raw.execute(
            """INSERT INTO subscribers
               (public_id,display_name,is_test,is_active,created_at)
               VALUES('legacy-paid-check','보존 대상',0,1,?)""",
            (utcnow(),),
        )
        raw.commit()
        raw.close()
        create_app({
            "TESTING": True,
            "SECRET_KEY": "paid-upgrade-secret",
            "DB_PATH": legacy_db,
            "ENABLE_TEST_IDENTITY": False,
            "SEED_DEMO_DATA": False,
        })
        conn = connect(legacy_db)
        person = conn.execute(
            """SELECT display_name,is_active,is_paid_subscriber
               FROM subscribers WHERE public_id='legacy-paid-check'"""
        ).fetchone()
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        self.assertEqual(dict(person), {
            "display_name": "보존 대상", "is_active": 1, "is_paid_subscriber": 0,
        })
        self.assertTrue({
            "community_posts", "community_comments", "community_likes"
        }.issubset(tables))

    def test_79_sync_without_paid_field_is_backward_compatible(self):
        subscriber_id = self.create_community_subscriber(
            "kept-paid@example.invalid", "유료 유지", paid=True
        )
        response = self.client.post(
            "/api/subscribers/sync",
            headers={"Authorization": "Bearer sync-test-secret"},
            json={"email": " KEPT-PAID@example.invalid ", "display_name": "변경 안 됨"},
        )
        self.assertEqual(response.status_code, 200)
        conn = connect(self.db_path)
        person = conn.execute(
            "SELECT id,is_active,is_paid_subscriber FROM subscribers WHERE id=?",
            (subscriber_id,),
        ).fetchone()
        conn.close()
        self.assertEqual(person["is_active"], 1)
        self.assertEqual(person["is_paid_subscriber"], 1)

        created = self.client.post(
            "/api/subscribers/sync",
            headers={"Authorization": "Bearer sync-test-secret"},
            json={"email": "new-free@example.invalid"},
        )
        self.assertEqual(created.status_code, 201)
        self.assertFalse(created.get_json()["subscriber"]["is_paid_subscriber"])

    def test_80_sync_explicit_paid_true_false_keeps_account_active(self):
        headers = {"Authorization": "Bearer sync-test-secret"}
        created = self.client.post(
            "/api/subscribers/sync",
            headers=headers,
            json={
                "email": "paid-sync@example.invalid",
                "is_paid_subscriber": True,
            },
        )
        self.assertEqual(created.status_code, 201)
        self.assertTrue(created.get_json()["subscriber"]["active"])
        self.assertTrue(created.get_json()["subscriber"]["is_paid_subscriber"])

        ended = self.client.post(
            "/api/subscribers/sync",
            headers=headers,
            json={
                "email": "paid-sync@example.invalid",
                "is_paid_subscriber": False,
            },
        )
        self.assertEqual(ended.status_code, 200)
        self.assertTrue(ended.get_json()["subscriber"]["active"])
        self.assertFalse(ended.get_json()["subscriber"]["is_paid_subscriber"])

    def test_81_sync_rejects_non_boolean_paid_status(self):
        response = self.client.post(
            "/api/subscribers/sync",
            headers={"Authorization": "Bearer sync-test-secret"},
            json={"email": "bad-paid@example.invalid", "is_paid_subscriber": "true"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error"],
            "is_paid_subscriber must be a boolean",
        )

    def test_audioletter_sync_initialization_monotonic_updates_and_paid_cycle(self):
        headers = {"Authorization": "Bearer sync-test-secret"}
        email = "audioletter@example.invalid"

        initial = self.client.post(
            "/api/subscribers/sync", headers=headers,
            json={"email": email, "is_paid_subscriber": True, "accessible_through": 6},
        )
        self.assertEqual(initial.status_code, 201)
        self.assertEqual(initial.get_json()["subscriber"]["accessible_through"], 6)
        public_id = initial.get_json()["subscriber"]["public_id"]

        for value, expected_status, expected_through in (
            (7, "updated", 7), (43, "updated", 43),
            (7, "unchanged", 43), (43, "unchanged", 43),
        ):
            response = self.client.post(
                "/api/subscribers/sync", headers=headers,
                json={"email": email, "accessible_through": value},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["status"], expected_status)
            self.assertEqual(response.get_json()["subscriber"]["accessible_through"], expected_through)

        for paid in (False, True):
            response = self.client.post(
                "/api/subscribers/sync", headers=headers,
                json={"email": email, "is_paid_subscriber": paid},
            )
            self.assertEqual(response.get_json()["subscriber"]["is_paid_subscriber"], paid)
            self.assertEqual(response.get_json()["subscriber"]["accessible_through"], 43)
            self.assertEqual(response.get_json()["subscriber"]["public_id"], public_id)

        unchanged = self.client.post(
            "/api/subscribers/sync", headers=headers, json={"email": email},
        )
        self.assertEqual(unchanged.get_json()["status"], "unchanged")
        self.assertEqual(unchanged.get_json()["subscriber"]["accessible_through"], 43)
        conn = connect(self.db_path)
        person = conn.execute(
            "SELECT * FROM subscribers WHERE email_hash=?",
            (email_hash(email, "migration-test-secret"),),
        ).fetchone()
        conn.close()
        self.assertEqual(person["accessible_through"], 43)
        self.assertNotIn(email, " ".join(str(v) for v in person if v is not None))
        self.assertTrue(can_access_paid_audioletter(person, 43, True))

    def test_audioletter_sync_rejects_non_integer_and_out_of_range_values(self):
        headers = {"Authorization": "Bearer sync-test-secret"}
        email = "bad-range@example.invalid"
        for invalid in (None, True, False, 6.0, "6", -1, 2**63):
            response = self.client.post(
                "/api/subscribers/sync", headers=headers,
                json={"email": email, "accessible_through": invalid},
            )
            self.assertEqual(response.status_code, 400, repr(invalid))
        conn = connect(self.db_path)
        self.assertIsNone(conn.execute(
            "SELECT id FROM subscribers WHERE email_hash=?",
            (email_hash(email, "migration-test-secret"),),
        ).fetchone())
        conn.close()

    def test_audioletter_existing_subscriber_migration_preserves_identity_and_flags(self):
        legacy_db = str(Path(self.temp.name) / "audioletter-upgrade.db")
        raw = sqlite3.connect(legacy_db)
        raw.execute(
            """CREATE TABLE subscribers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_id TEXT NOT NULL UNIQUE, display_name TEXT,
                email_hash TEXT UNIQUE, is_test INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                is_paid_subscriber INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL)"""
        )
        raw.execute(
            """INSERT INTO subscribers
               (public_id,email_hash,is_active,is_paid_subscriber,created_at)
               VALUES(?,?,?,?,?)""",
            ("kept-id", "kept-hash", 1, 1, utcnow()),
        )
        raw.commit()
        raw.close()
        config = {
            "TESTING": True, "SECRET_KEY": "migration-test",
            "DB_PATH": legacy_db, "ENABLE_TEST_IDENTITY": False,
            "SEED_DEMO_DATA": False,
        }
        create_app(config)
        create_app(config)  # Startup migration remains idempotent.
        conn = connect(legacy_db)
        row = conn.execute(
            "SELECT id,public_id,email_hash,is_active,is_paid_subscriber,accessible_through "
            "FROM subscribers WHERE public_id='kept-id'"
        ).fetchone()
        conn.close()
        self.assertEqual(tuple(row), (1, "kept-id", "kept-hash", 1, 1, None))

    def test_audioletter_access_helper_uses_paid_active_published_and_sequence(self):
        subscriber = {"is_active": 1, "is_paid_subscriber": 1, "accessible_through": 43}
        self.assertTrue(can_access_paid_audioletter(subscriber, 42, True))
        self.assertTrue(can_access_paid_audioletter(subscriber, 43, True))
        self.assertFalse(can_access_paid_audioletter(subscriber, 44, True))
        self.assertFalse(can_access_paid_audioletter(subscriber, 43, False))
        self.assertFalse(can_access_paid_audioletter(None, 43, True))
        for field in ("is_active", "is_paid_subscriber"):
            blocked = dict(subscriber, **{field: 0})
            self.assertFalse(can_access_paid_audioletter(blocked, 43, True))
        self.assertFalse(can_access_paid_audioletter(dict(subscriber, accessible_through=None), 7, True))
        self.assertFalse(can_access_paid_audioletter(subscriber, True, True))

    def test_82_community_requires_login_and_paid_status_only(self):
        unauthenticated = self.client.get("/community")
        self.assertEqual(unauthenticated.status_code, 302)

        free_id = self.create_community_subscriber(
            "free@example.invalid", "무료 구독자", paid=False
        )
        self.login_community_subscriber(free_id)
        blocked = self.client.get("/community")
        self.assertEqual(blocked.status_code, 403)
        self.assertIn("현재 유료 구독자 전용 공간입니다.", blocked.get_data(as_text=True))
        self.assertEqual(self.client.get("/resources").status_code, 200)
        self.assertEqual(self.client.get("/quiz?episode=R041").status_code, 200)

    def test_83_paid_access_end_and_resubscribe_do_not_change_is_active(self):
        subscriber_id = self.create_community_subscriber(
            "cycle@example.invalid", "재구독자", paid=True
        )
        self.login_community_subscriber(subscriber_id)
        self.assertEqual(self.client.get("/community").status_code, 200)

        headers = {"Authorization": "Bearer sync-test-secret"}
        self.client.post(
            "/api/subscribers/sync", headers=headers,
            json={"email": "cycle@example.invalid", "is_paid_subscriber": False},
        )
        self.assertEqual(self.client.get("/community").status_code, 403)
        self.client.post(
            "/api/subscribers/sync", headers=headers,
            json={"email": "cycle@example.invalid", "is_paid_subscriber": True},
        )
        self.assertEqual(self.client.get("/community").status_code, 200)
        conn = connect(self.db_path)
        self.assertEqual(
            conn.execute(
                "SELECT is_active FROM subscribers WHERE id=?", (subscriber_id,)
            ).fetchone()[0],
            1,
        )
        conn.close()

    def test_84_paid_subscriber_creates_post_and_admin_notification(self):
        subscriber_id = self.create_community_subscriber(
            "writer@example.invalid", "글쓴이", paid=True
        )
        csrf = self.login_community_subscriber(subscriber_id)
        response = self.client.post(
            "/community/new",
            data={"csrf_token": csrf, "title": "새 질문", "body": "궁금한 내용"},
        )
        self.assertEqual(response.status_code, 302)
        conn = connect(self.db_path)
        post = conn.execute(
            "SELECT * FROM community_posts WHERE subscriber_id=?", (subscriber_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(post["title"], "새 질문")
        self.assertEqual(len(self.community_notifications), 1)
        self.assertEqual(self.community_notifications[0][0:3], (
            "새 질문", "글쓴이", self.community_notifications[0][2]
        ))
        self.assertIn(f"/admin/community/{post['id']}", self.community_notifications[0][3])

    def test_85_notification_failure_does_not_rollback_post(self):
        subscriber_id = self.create_community_subscriber(
            "notify-fail@example.invalid", "알림 실패", paid=True
        )
        self.app.config["COMMUNITY_NOTIFICATION_SENDER"] = (
            lambda *args: (_ for _ in ()).throw(RuntimeError("delivery failed"))
        )
        csrf = self.login_community_subscriber(subscriber_id)
        response = self.client.post(
            "/community/new",
            data={"csrf_token": csrf, "title": "저장 유지", "body": "본문"},
        )
        self.assertEqual(response.status_code, 302)
        conn = connect(self.db_path)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM community_posts WHERE title='저장 유지'"
            ).fetchone()[0],
            1,
        )
        conn.close()

    def test_86_post_owner_can_edit_and_delete_with_dependents(self):
        subscriber_id = self.create_community_subscriber(
            "owner@example.invalid", "소유자", paid=True
        )
        post_id = self.create_community_post(subscriber_id)
        with transaction(self.db_path) as conn:
            conn.execute(
                "INSERT INTO community_comments(post_id,subscriber_id,body,created_at) VALUES(?,?,?,?)",
                (post_id, subscriber_id, "댓글", utcnow()),
            )
            conn.execute(
                "INSERT INTO community_likes(post_id,subscriber_id,created_at) VALUES(?,?,?)",
                (post_id, subscriber_id, utcnow()),
            )
        csrf = self.login_community_subscriber(subscriber_id)
        edited = self.client.post(
            f"/community/{post_id}/edit",
            data={"csrf_token": csrf, "title": "수정 제목", "body": "수정 본문"},
        )
        self.assertEqual(edited.status_code, 302)
        deleted = self.client.post(
            f"/community/{post_id}/delete", data={"csrf_token": csrf}
        )
        self.assertEqual(deleted.status_code, 302)
        conn = connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM community_posts").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM community_comments").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM community_likes").fetchone()[0], 0)
        conn.close()

    def test_87_other_subscriber_cannot_edit_or_delete_post(self):
        owner_id = self.create_community_subscriber(
            "owner2@example.invalid", "소유자", paid=True
        )
        other_id = self.create_community_subscriber(
            "other2@example.invalid", "다른 구독자", paid=True
        )
        post_id = self.create_community_post(owner_id)
        csrf = self.login_community_subscriber(other_id)
        self.assertEqual(self.client.get(f"/community/{post_id}/edit").status_code, 403)
        self.assertEqual(
            self.client.post(
                f"/community/{post_id}/delete", data={"csrf_token": csrf}
            ).status_code,
            403,
        )
        conn = connect(self.db_path)
        self.assertIsNotNone(
            conn.execute("SELECT id FROM community_posts WHERE id=?", (post_id,)).fetchone()
        )
        conn.close()

    def test_88_comment_owner_delete_and_other_delete_forbidden(self):
        owner_id = self.create_community_subscriber(
            "commenter@example.invalid", "댓글 작성자", paid=True
        )
        other_id = self.create_community_subscriber(
            "other-comment@example.invalid", "다른 사람", paid=True
        )
        post_id = self.create_community_post(owner_id)
        csrf = self.login_community_subscriber(owner_id)
        created = self.client.post(
            f"/community/{post_id}/comments",
            data={"csrf_token": csrf, "body": "첫 댓글"},
        )
        self.assertEqual(created.status_code, 302)
        conn = connect(self.db_path)
        comment_id = conn.execute("SELECT id FROM community_comments").fetchone()[0]
        conn.close()

        other_csrf = self.login_community_subscriber(other_id, "other-comment-csrf")
        self.assertEqual(
            self.client.post(
                f"/community/comments/{comment_id}/delete",
                data={"csrf_token": other_csrf},
            ).status_code,
            403,
        )
        owner_csrf = self.login_community_subscriber(owner_id, "owner-comment-csrf")
        self.assertEqual(
            self.client.post(
                f"/community/comments/{comment_id}/delete",
                data={"csrf_token": owner_csrf},
            ).status_code,
            302,
        )

    def test_89_likes_toggle_uniquely_and_count_each_subscriber(self):
        first_id = self.create_community_subscriber(
            "like-one@example.invalid", "첫 번째", paid=True
        )
        second_id = self.create_community_subscriber(
            "like-two@example.invalid", "두 번째", paid=True
        )
        post_id = self.create_community_post(first_id)
        csrf = self.login_community_subscriber(first_id)
        self.client.post(f"/community/{post_id}/like", data={"csrf_token": csrf})
        self.client.post(f"/community/{post_id}/like", data={"csrf_token": csrf})
        self.client.post(f"/community/{post_id}/like", data={"csrf_token": csrf})
        second_csrf = self.login_community_subscriber(second_id, "second-like-csrf")
        self.client.post(
            f"/community/{post_id}/like", data={"csrf_token": second_csrf}
        )
        conn = connect(self.db_path)
        likes = conn.execute(
            "SELECT subscriber_id FROM community_likes WHERE post_id=? ORDER BY subscriber_id",
            (post_id,),
        ).fetchall()
        conn.close()
        self.assertEqual([row[0] for row in likes], sorted([first_id, second_id]))

    def test_90_admin_can_hide_and_subscriber_cannot_discover_or_open_post(self):
        subscriber_id = self.create_community_subscriber(
            "hidden@example.invalid", "작성자", paid=True
        )
        post_id = self.create_community_post(subscriber_id, title="숨길 글")
        with self.client.session_transaction() as state:
            state.clear()
            state["is_admin"] = True
            state["csrf_token"] = "admin-hide-csrf"
        admin_page = self.client.get("/admin/community")
        self.assertIn("숨길 글", admin_page.get_data(as_text=True))
        hidden = self.client.post(
            f"/admin/community/{post_id}/visibility",
            data={"csrf_token": "admin-hide-csrf", "is_visible": "0"},
        )
        self.assertEqual(hidden.status_code, 302)

        self.login_community_subscriber(subscriber_id, "hidden-subscriber-csrf")
        listing = self.client.get("/community").get_data(as_text=True)
        self.assertNotIn("숨길 글", listing)
        self.assertEqual(self.client.get(f"/community/{post_id}").status_code, 404)

    def test_91_admin_can_delete_any_comment_and_post(self):
        subscriber_id = self.create_community_subscriber(
            "admin-delete@example.invalid", "작성자", paid=True
        )
        post_id = self.create_community_post(subscriber_id)
        with transaction(self.db_path) as conn:
            comment_id = conn.execute(
                "INSERT INTO community_comments(post_id,subscriber_id,body,created_at) VALUES(?,?,?,?)",
                (post_id, subscriber_id, "관리 댓글", utcnow()),
            ).lastrowid
        with self.client.session_transaction() as state:
            state["is_admin"] = True
            state["csrf_token"] = "admin-delete-csrf"
        self.assertEqual(
            self.client.post(
                f"/admin/community/comments/{comment_id}/delete",
                data={"csrf_token": "admin-delete-csrf"},
            ).status_code,
            302,
        )
        self.assertEqual(
            self.client.post(
                f"/admin/community/{post_id}/delete",
                data={"csrf_token": "admin-delete-csrf"},
            ).status_code,
            302,
        )

    def test_92_subscription_end_preserves_existing_post_and_comment(self):
        subscriber_id = self.create_community_subscriber(
            "preserved@example.invalid", "보존 작성자", paid=True
        )
        post_id = self.create_community_post(subscriber_id)
        with transaction(self.db_path) as conn:
            conn.execute(
                "INSERT INTO community_comments(post_id,subscriber_id,body,created_at) VALUES(?,?,?,?)",
                (post_id, subscriber_id, "보존 댓글", utcnow()),
            )
        self.client.post(
            "/api/subscribers/sync",
            headers={"Authorization": "Bearer sync-test-secret"},
            json={"email": "preserved@example.invalid", "is_paid_subscriber": False},
        )
        self.login_community_subscriber(subscriber_id)
        self.assertEqual(self.client.get("/community").status_code, 403)
        conn = connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM community_posts").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM community_comments").fetchone()[0], 1)
        conn.close()

    def test_93_community_post_routes_require_csrf(self):
        subscriber_id = self.create_community_subscriber(
            "csrf-community@example.invalid", "CSRF 확인", paid=True
        )
        post_id = self.create_community_post(subscriber_id)
        with self.client.session_transaction() as state:
            state["subscriber_id"] = subscriber_id
            state["csrf_token"] = "expected-csrf"
        routes = [
            ("/community/new", {"title": "제목", "body": "본문"}),
            (f"/community/{post_id}/delete", {}),
            (f"/community/{post_id}/comments", {"body": "댓글"}),
            (f"/community/{post_id}/like", {}),
        ]
        for route, data in routes:
            with self.subTest(route=route):
                self.assertEqual(self.client.post(route, data=data).status_code, 400)

    def test_94_community_user_input_is_html_escaped(self):
        subscriber_id = self.create_community_subscriber(
            "escape@example.invalid", "<b>작성자</b>", paid=True
        )
        post_id = self.create_community_post(
            subscriber_id, title="<script>alert(1)</script>"
        )
        self.login_community_subscriber(subscriber_id)
        html = self.client.get(f"/community/{post_id}").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<b>작성자</b>", html)

    def test_95_community_brevo_notification_uses_admin_recipient_without_member_email(self):
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
            "COMMUNITY_ADMIN_NOTIFICATION_EMAIL": "admin@example.invalid",
            "BREVO_TIMEOUT_SECONDS": 10,
        }
        with patch("magic_links.urllib.request.urlopen", return_value=FakeResponse()) as mocked:
            send_community_post_notification_via_brevo(
                "<새 글>",
                "작성자",
                "2026.09.21 18:00",
                "https://portal.example.invalid/admin/community/1",
                config,
            )
        request = mocked.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["to"], [{"email": "admin@example.invalid"}])
        self.assertIn("&lt;새 글&gt;", payload["htmlContent"])
        self.assertNotIn("member@example.invalid", request.data.decode("utf-8"))

    def test_96_subscriber_cannot_access_community_admin_routes(self):
        subscriber_id = self.create_community_subscriber(
            "not-admin@example.invalid", "일반 구독자", paid=True
        )
        post_id = self.create_community_post(subscriber_id)
        csrf = self.login_community_subscriber(subscriber_id)
        self.assertEqual(self.client.get("/admin/community").status_code, 302)
        self.assertEqual(
            self.client.post(
                f"/admin/community/{post_id}/delete",
                data={"csrf_token": csrf},
            ).status_code,
            302,
        )
        conn = connect(self.db_path)
        self.assertIsNotNone(
            conn.execute("SELECT id FROM community_posts WHERE id=?", (post_id,)).fetchone()
        )
        conn.close()

    def test_97_inactive_paid_subscriber_is_not_allowed_into_community(self):
        subscriber_id = self.create_community_subscriber(
            "inactive-paid@example.invalid", "비활성 유료", paid=True, active=False
        )
        self.login_community_subscriber(subscriber_id)
        response = self.client.get("/community")
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as state:
            self.assertNotIn("subscriber_id", state)

    def test_98_public_subscription_terms_and_privacy_need_no_login(self):
        for path, text in [
            ("/subscribe", "반려견 오디오레터"),
            ("/terms", "서비스 이용약관"),
            ("/privacy", "개인정보처리방침"),
        ]:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(text, response.get_data(as_text=True))

    def test_99_subscription_page_defaults_off_and_hides_checkout_destinations(self):
        html = self.client.get("/subscribe").get_data(as_text=True)
        self.assertIn("현재 구독 결제 페이지를 준비 중입니다", html)
        self.assertNotIn("payapp.kr", html.lower())
        conn = connect(self.db_path)
        settings = conn.execute("SELECT * FROM portal_settings WHERE id=1").fetchone()
        conn.close()
        self.assertEqual(settings["subscription_page_enabled"], 0)

    def test_100_off_page_rejects_general_checkout_even_with_agreements(self):
        csrf = self.set_csrf(self.client, "off-payment-csrf")
        response = self.client.post(
            "/subscribe/pay/one-month",
            data={"csrf_token": csrf, "agree_terms": "yes", "agree_privacy": "yes"},
        )
        self.assertEqual(response.status_code, 403)

    def test_101_admin_can_preview_checkout_while_page_is_off(self):
        with self.client.session_transaction() as state:
            state["is_admin"] = True
            state["csrf_token"] = "admin-preview-csrf"
        page = self.client.get("/subscribe")
        self.assertIn("관리자 미리보기", page.get_data(as_text=True))
        response = self.client.post(
            "/subscribe/pay/one-month",
            data={
                "csrf_token": "admin-preview-csrf",
                "agree_terms": "yes",
                "agree_privacy": "yes",
            },
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "https://www.payapp.kr/L/z49rzA")

    def test_102_admin_can_enable_page_and_set_both_effective_dates(self):
        with self.client.session_transaction() as state:
            state["is_admin"] = True
            state["csrf_token"] = "settings-csrf"
        response = self.client.post(
            "/admin/portal-settings",
            data={
                "csrf_token": "settings-csrf",
                "subscription_page_enabled": "1",
                "terms_effective_date": "2026-10-01",
                "privacy_effective_date": "2026-10-02",
            },
        )
        self.assertEqual(response.status_code, 302)
        conn = connect(self.db_path)
        settings = conn.execute("SELECT * FROM portal_settings WHERE id=1").fetchone()
        conn.close()
        self.assertEqual(settings["subscription_page_enabled"], 1)
        self.assertEqual(settings["terms_effective_date"], "2026-10-01")
        self.assertEqual(settings["privacy_effective_date"], "2026-10-02")
        self.assertIn("2026년 10월 1일", self.client.get("/terms").get_data(as_text=True))
        self.assertIn("2026년 10월 2일", self.client.get("/privacy").get_data(as_text=True))

    def test_103_enabled_page_exposes_internal_checkout_flow_not_payapp_url(self):
        with transaction(self.db_path) as conn:
            conn.execute("UPDATE portal_settings SET subscription_page_enabled=1 WHERE id=1")
        html = self.client.get("/subscribe").get_data(as_text=True)
        self.assertIn("/subscribe/pay/one-month", html)
        self.assertIn("/subscribe/pay/three-month", html)
        self.assertNotIn("payapp.kr", html.lower())
        self.assertIn("disabled", html)

    def test_104_checkout_requires_both_server_side_agreements(self):
        with transaction(self.db_path) as conn:
            conn.execute("UPDATE portal_settings SET subscription_page_enabled=1 WHERE id=1")
        csrf = self.set_csrf(self.client, "agreements-csrf")
        cases = [
            {},
            {"agree_terms": "yes"},
            {"agree_privacy": "yes"},
        ]
        for fields in cases:
            with self.subTest(fields=fields):
                response = self.client.post(
                    "/subscribe/pay/one-month",
                    data={"csrf_token": csrf, **fields},
                )
                self.assertEqual(response.status_code, 302)
                self.assertEqual(urlsplit(response.headers["Location"]).path, "/subscribe")

    def test_105_agreements_redirect_to_correct_one_month_payapp_url(self):
        with transaction(self.db_path) as conn:
            conn.execute("UPDATE portal_settings SET subscription_page_enabled=1 WHERE id=1")
        csrf = self.set_csrf(self.client, "one-month-csrf")
        response = self.client.post(
            "/subscribe/pay/one-month",
            data={"csrf_token": csrf, "agree_terms": "yes", "agree_privacy": "yes"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "https://www.payapp.kr/L/z49rzA")

    def test_106_agreements_redirect_to_correct_three_month_payapp_url(self):
        with transaction(self.db_path) as conn:
            conn.execute("UPDATE portal_settings SET subscription_page_enabled=1 WHERE id=1")
        csrf = self.set_csrf(self.client, "three-month-csrf")
        response = self.client.post(
            "/subscribe/pay/three-month",
            data={"csrf_token": csrf, "agree_terms": "yes", "agree_privacy": "yes"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "https://www.payapp.kr/L/z49suD")

    def test_107_unset_effective_dates_are_not_invented(self):
        self.assertIn("시행일: 확정 전", self.client.get("/terms").get_data(as_text=True))
        self.assertIn("시행일: 확정 전", self.client.get("/privacy").get_data(as_text=True))

    def test_108_portal_settings_requires_admin_and_csrf(self):
        self.assertEqual(self.client.get("/admin/portal-settings").status_code, 302)
        with self.client.session_transaction() as state:
            state["is_admin"] = True
            state["csrf_token"] = "expected-settings-csrf"
        self.assertEqual(
            self.client.post(
                "/admin/portal-settings",
                data={"subscription_page_enabled": "1"},
            ).status_code,
            400,
        )

    def test_109_checkout_requires_csrf_even_when_page_is_enabled(self):
        with transaction(self.db_path) as conn:
            conn.execute("UPDATE portal_settings SET subscription_page_enabled=1 WHERE id=1")
        self.assertEqual(
            self.client.post(
                "/subscribe/pay/one-month",
                data={"agree_terms": "yes", "agree_privacy": "yes"},
            ).status_code,
            400,
        )

    def test_110_invalid_plan_does_not_redirect_to_external_site(self):
        with transaction(self.db_path) as conn:
            conn.execute("UPDATE portal_settings SET subscription_page_enabled=1 WHERE id=1")
        csrf = self.set_csrf(self.client, "invalid-plan-csrf")
        response = self.client.post(
            "/subscribe/pay/not-a-plan",
            data={"csrf_token": csrf, "agree_terms": "yes", "agree_privacy": "yes"},
        )
        self.assertEqual(response.status_code, 404)

    def test_111_additive_portal_settings_migration_preserves_existing_data(self):
        old_path = str(Path(self.temp.name) / "old-portal.db")
        conn = sqlite3.connect(old_path)
        conn.executescript(
            """
            CREATE TABLE subscribers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_id TEXT NOT NULL UNIQUE,
                display_name TEXT,
                email_hash TEXT UNIQUE,
                is_test INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                is_paid_subscriber INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            INSERT INTO subscribers(public_id,display_name,is_test,created_at)
            VALUES('preserved-member','보존 회원',0,'2026-01-01T00:00:00+00:00');
            """
        )
        conn.commit()
        conn.close()
        migrated = create_app({
            "TESTING": True,
            "SECRET_KEY": "migration-secret",
            "DB_PATH": old_path,
            "ENABLE_TEST_IDENTITY": False,
            "SEED_DEMO_DATA": False,
        })
        self.assertIsNotNone(migrated)
        conn = connect(old_path)
        self.assertEqual(
            conn.execute("SELECT display_name FROM subscribers WHERE public_id='preserved-member'").fetchone()[0],
            "보존 회원",
        )
        self.assertEqual(
            conn.execute("SELECT subscription_page_enabled FROM portal_settings WHERE id=1").fetchone()[0],
            0,
        )
        conn.close()

    def test_112_subscription_copy_plan_order_and_registration_cta(self):
        self.enable_subscription_registration()
        html = self.client.get("/subscribe").get_data(as_text=True)
        self.assertIn("영양을 중심으로 생리와 질환, 생활환경까지 연결해", html)
        self.assertNotIn("Google Form", html)
        self.assertLess(html.index("3개월 이용권"), html.index("1개월 이용권"))
        self.assertIn("결제를 완료하셨나요?", html)
        self.assertIn('/subscription/register', html)

    def test_113_registration_page_respects_subscription_page_off_and_admin_preview(self):
        self.assertEqual(self.client.get("/subscription/register").status_code, 403)
        with self.client.session_transaction() as state:
            state["is_admin"] = True
        response = self.client.get("/subscription/register")
        self.assertEqual(response.status_code, 200)
        self.assertIn("구독 등록", response.get_data(as_text=True))

    def test_114_valid_registration_saves_application_and_dog_without_paid_access(self):
        self.enable_subscription_registration()
        response = self.client.post(
            "/subscription/register", data=self.registration_form_data()
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(urlsplit(response.headers["Location"]).path, "/subscription/register/complete")
        conn = connect(self.db_path)
        registration = conn.execute("SELECT * FROM subscription_registrations").fetchone()
        dog = conn.execute("SELECT * FROM dog_profiles").fetchone()
        subscriber = conn.execute(
            "SELECT * FROM subscribers WHERE email_hash=?",
            (email_hash("member@example.invalid", "migration-test-secret"),),
        ).fetchone()
        conn.close()
        self.assertEqual(registration["status"], "pending")
        self.assertEqual(dog["name"], "토리")
        self.assertEqual(dog["birth_date"], "2020-05-12")
        self.assertIsNone(registration["subscriber_id"])
        self.assertIsNone(subscriber)

    def test_115_registration_rejects_invalid_input_without_writing(self):
        self.enable_subscription_registration()
        response = self.client.post(
            "/subscription/register",
            data=self.registration_form_data(
                email="not-email",
                contact_phone="123",
                dog_birth_date="2999-01-01",
                privacy_agree="",
            ),
        )
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("이메일 주소를 올바르게", html)
        self.assertIn("개인정보 수집·이용에 동의", html)
        conn = connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM subscription_registrations").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM dog_profiles").fetchone()[0], 0)
        conn.close()

    def test_116_registration_post_requires_csrf(self):
        with transaction(self.db_path) as conn:
            conn.execute("UPDATE portal_settings SET subscription_page_enabled=1 WHERE id=1")
        data = self.registration_form_data()
        data.pop("csrf_token")
        self.assertEqual(self.client.post("/subscription/register", data=data).status_code, 400)

    def test_117_duplicate_pending_registration_updates_in_place(self):
        self.enable_subscription_registration()
        self.client.post("/subscription/register", data=self.registration_form_data())
        self.client.post(
            "/subscription/register",
            data=self.registration_form_data(
                email="  MEMBER@EXAMPLE.INVALID ", dog_name="토리 수정", dog_breed="말티즈"
            ),
        )
        conn = connect(self.db_path)
        registration_count = conn.execute("SELECT COUNT(*) FROM subscription_registrations").fetchone()[0]
        dog_rows = conn.execute("SELECT name,breed FROM dog_profiles").fetchall()
        conn.close()
        self.assertEqual(registration_count, 1)
        self.assertEqual(len(dog_rows), 1)
        self.assertEqual((dog_rows[0]["name"], dog_rows[0]["breed"]), ("토리 수정", "말티즈"))

    def test_118_unrelated_logged_in_subscriber_cannot_claim_dog_profile(self):
        self.enable_subscription_registration()
        unrelated_id = self.create_real_subscriber(
            "other@example.invalid", "다른 구독자"
        )
        with self.client.session_transaction() as state:
            state["subscriber_id"] = unrelated_id
            state["csrf_token"] = "registration-csrf"
        self.client.post("/subscription/register", data=self.registration_form_data())
        conn = connect(self.db_path)
        registration = conn.execute("SELECT subscriber_id FROM subscription_registrations").fetchone()
        dog = conn.execute("SELECT id,subscriber_id FROM dog_profiles").fetchone()
        conn.close()
        self.assertIsNone(registration["subscriber_id"])
        self.assertIsNone(dog["subscriber_id"])
        self.assertEqual(self.client.get(f"/subscription/dogs/{dog['id']}").status_code, 404)

    def test_119_pending_registration_api_requires_separate_bearer_key(self):
        self.enable_subscription_registration()
        self.client.post("/subscription/register", data=self.registration_form_data())
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            with self.subTest(headers=headers):
                self.assertEqual(
                    self.client.get("/api/subscription-registrations/pending", headers=headers).status_code,
                    401,
                )

    def test_120_pending_registration_api_returns_form_mapping_and_no_store(self):
        self.enable_subscription_registration()
        self.client.post("/subscription/register", data=self.registration_form_data())
        response = self.client.get(
            "/api/subscription-registrations/pending",
            headers={"Authorization": "Bearer registration-test-secret"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        item = response.get_json()["registrations"][0]
        self.assertEqual(item["subscription_period"], "3개월")
        self.assertEqual(item["email"], "member@example.invalid")
        self.assertEqual(item["contact_phone"], "01012345678")
        self.assertEqual(item["dog_birth_date"], "2020.05.12")
        self.assertEqual(item["registration_type"], "신규")
        self.assertTrue(item["privacy_agreed"])

    def test_121_subscriber_sync_links_profile_but_does_not_infer_paid_status(self):
        self.enable_subscription_registration()
        self.client.post("/subscription/register", data=self.registration_form_data())
        response = self.client.post(
            "/api/subscribers/sync",
            headers={"Authorization": "Bearer sync-test-secret"},
            json={"email": "member@example.invalid", "display_name": "보호자"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.get_json()["subscriber"]["is_paid_subscriber"])
        conn = connect(self.db_path)
        registration = conn.execute("SELECT subscriber_id,status FROM subscription_registrations").fetchone()
        dog = conn.execute("SELECT subscriber_id FROM dog_profiles").fetchone()
        person = conn.execute("SELECT is_paid_subscriber FROM subscribers WHERE id=?", (registration["subscriber_id"],)).fetchone()
        conn.close()
        self.assertIsNotNone(registration["subscriber_id"])
        self.assertEqual(dog["subscriber_id"], registration["subscriber_id"])
        self.assertEqual(person["is_paid_subscriber"], 0)
        self.assertEqual(registration["status"], "pending")

    def test_122_make_completion_requires_sync_and_is_idempotent(self):
        self.enable_subscription_registration()
        self.client.post("/subscription/register", data=self.registration_form_data())
        conn = connect(self.db_path)
        public_id = conn.execute("SELECT public_id FROM subscription_registrations").fetchone()[0]
        conn.close()
        headers = {"Authorization": "Bearer registration-test-secret"}
        self.assertEqual(
            self.client.post(f"/api/subscription-registrations/{public_id}/complete", headers=headers).status_code,
            409,
        )
        conn = connect(self.db_path)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM subscription_registration_payloads").fetchone()[0],
            1,
        )
        conn.close()
        self.client.post(
            "/api/subscribers/sync",
            headers={"Authorization": "Bearer sync-test-secret"},
            json={"email": "member@example.invalid", "is_paid_subscriber": True},
        )
        first = self.client.post(f"/api/subscription-registrations/{public_id}/complete", headers=headers)
        second = self.client.post(f"/api/subscription-registrations/{public_id}/complete", headers=headers)
        self.assertEqual(first.get_json()["status"], "completed")
        self.assertEqual(second.get_json()["status"], "unchanged")
        conn = connect(self.db_path)
        row = conn.execute(
            """SELECT r.status,s.is_paid_subscriber FROM subscription_registrations r
               JOIN subscribers s ON s.id=r.subscriber_id"""
        ).fetchone()
        payload_count = conn.execute(
            "SELECT COUNT(*) FROM subscription_registration_payloads"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["is_paid_subscriber"], 1)
        self.assertEqual(payload_count, 0)

    def test_123_completed_registration_allows_future_resubscription(self):
        self.enable_subscription_registration()
        self.client.post("/subscription/register", data=self.registration_form_data())
        self.client.post(
            "/api/subscribers/sync",
            headers={"Authorization": "Bearer sync-test-secret"},
            json={"email": "member@example.invalid", "is_paid_subscriber": True},
        )
        conn = connect(self.db_path)
        public_id = conn.execute("SELECT public_id FROM subscription_registrations").fetchone()[0]
        conn.close()
        headers = {"Authorization": "Bearer registration-test-secret"}
        self.client.post(f"/api/subscription-registrations/{public_id}/complete", headers=headers)
        self.client.post(
            "/subscription/register",
            data=self.registration_form_data(registration_type="renewal", plan_code="one-month"),
        )
        conn = connect(self.db_path)
        statuses = conn.execute(
            "SELECT status,registration_type,plan_code FROM subscription_registrations ORDER BY id"
        ).fetchall()
        conn.close()
        self.assertEqual(len(statuses), 2)
        self.assertEqual(statuses[0]["status"], "completed")
        self.assertEqual((statuses[1]["status"], statuses[1]["registration_type"], statuses[1]["plan_code"]), ("pending", "renewal", "one-month"))

    def test_124_additive_registration_migration_preserves_existing_rows(self):
        before = connect(self.db_path)
        counts_before = {
            table: before.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("subscribers", "episodes", "questions", "participation", "feedback_submissions")
        }
        before.close()
        migrated = create_app({
            "TESTING": True,
            "SECRET_KEY": "migration-secret",
            "DB_PATH": self.db_path,
            "ENABLE_TEST_IDENTITY": True,
            "SEED_DEMO_DATA": False,
            "MIGRATION_HASH_SECRET": "migration-test-secret",
        })
        self.assertIsNotNone(migrated)
        after = connect(self.db_path)
        counts_after = {
            table: after.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in counts_before
        }
        table_names = {
            row[0] for row in after.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        after.close()
        self.assertEqual(counts_before, counts_after)
        self.assertIn("subscription_registrations", table_names)
        self.assertIn("subscription_registration_payloads", table_names)
        self.assertIn("dog_profiles", table_names)

    def test_125_registration_pii_is_only_stored_as_encrypted_payload(self):
        self.enable_subscription_registration()
        values = self.registration_form_data(
            email="private-person@example.invalid",
            guardian_name="SensitiveGuardian",
            contact_phone="010-8765-4321",
            payer_name="SensitivePayer",
            interests="SensitiveInterests",
        )
        self.client.post("/subscription/register", data=values)
        conn = connect(self.db_path)
        registration_columns = {
            row["name"] for row in conn.execute(
                "PRAGMA table_info(subscription_registrations)"
            )
        }
        token = conn.execute(
            "SELECT encrypted_payload FROM subscription_registration_payloads"
        ).fetchone()[0]
        conn.close()
        self.assertTrue(
            {"email", "guardian_name", "contact_phone", "payer_name", "interests"}.isdisjoint(
                registration_columns
            )
        )
        for plaintext in (
            "private-person@example.invalid",
            "SensitiveGuardian",
            "01087654321",
            "SensitivePayer",
            "SensitiveInterests",
        ):
            self.assertNotIn(plaintext, token)
            self.assertNotIn(plaintext.encode("utf-8"), Path(self.db_path).read_bytes())

    def test_126_pending_api_preserves_existing_plaintext_response_contract(self):
        self.enable_subscription_registration()
        self.client.post(
            "/subscription/register",
            data=self.registration_form_data(payer_name="결제자", interests="장 건강"),
        )
        response = self.client.get(
            "/api/subscription-registrations/pending",
            headers={"Authorization": "Bearer registration-test-secret"},
        )
        self.assertEqual(response.status_code, 200)
        item = response.get_json()["registrations"][0]
        self.assertEqual(item["email"], "member@example.invalid")
        self.assertEqual(item["guardian_name"], "보호자")
        self.assertEqual(item["contact_phone"], "01012345678")
        self.assertEqual(item["payer_name"], "결제자")
        self.assertEqual(item["interests"], "장 건강")

    def test_127_wrong_key_and_corrupted_payload_fail_without_disclosure(self):
        self.enable_subscription_registration()
        self.client.post("/subscription/register", data=self.registration_form_data())
        headers = {"Authorization": "Bearer registration-test-secret"}
        original_key = self.app.config["SUBSCRIPTION_REGISTRATION_ENCRYPTION_KEY"]
        self.app.config["SUBSCRIPTION_REGISTRATION_ENCRYPTION_KEY"] = (
            "5CA4ejWD-gn8CZG1_Gqar825RDlSbbnRjHUw78-4WxY="
        )
        wrong_key = self.client.get(
            "/api/subscription-registrations/pending", headers=headers
        )
        self.assertEqual(wrong_key.status_code, 503)
        self.assertNotIn("member@example.invalid", wrong_key.get_data(as_text=True))
        self.app.config["SUBSCRIPTION_REGISTRATION_ENCRYPTION_KEY"] = original_key
        with transaction(self.db_path) as conn:
            conn.execute(
                "UPDATE subscription_registration_payloads SET encrypted_payload='damaged'"
            )
        damaged = self.client.get(
            "/api/subscription-registrations/pending", headers=headers
        )
        self.assertEqual(damaged.status_code, 503)
        self.assertNotIn("member@example.invalid", damaged.get_data(as_text=True))

    def test_128_complete_failure_keeps_payload_for_retry(self):
        self.enable_subscription_registration()
        self.client.post("/subscription/register", data=self.registration_form_data())
        conn = connect(self.db_path)
        public_id = conn.execute(
            "SELECT public_id FROM subscription_registrations"
        ).fetchone()[0]
        conn.close()
        response = self.client.post(
            f"/api/subscription-registrations/{public_id}/complete",
            headers={"Authorization": "Bearer registration-test-secret"},
        )
        self.assertEqual(response.status_code, 409)
        conn = connect(self.db_path)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM subscription_registration_payloads").fetchone()[0],
            1,
        )
        self.assertEqual(
            conn.execute("SELECT status FROM subscription_registrations").fetchone()[0],
            "pending",
        )
        conn.close()

    def test_129_complete_deletes_payload_but_keeps_linked_dog_profile(self):
        self.enable_subscription_registration()
        self.client.post("/subscription/register", data=self.registration_form_data())
        sync = self.client.post(
            "/api/subscribers/sync",
            headers={"Authorization": "Bearer sync-test-secret"},
            json={"email": "member@example.invalid", "is_paid_subscriber": True},
        )
        self.assertEqual(sync.status_code, 201)
        conn = connect(self.db_path)
        public_id = conn.execute(
            "SELECT public_id FROM subscription_registrations"
        ).fetchone()[0]
        conn.close()
        completed = self.client.post(
            f"/api/subscription-registrations/{public_id}/complete",
            headers={"Authorization": "Bearer registration-test-secret"},
        )
        self.assertEqual(completed.status_code, 200)
        conn = connect(self.db_path)
        registration = conn.execute(
            "SELECT id,subscriber_id,status FROM subscription_registrations"
        ).fetchone()
        dog = conn.execute(
            "SELECT registration_id,subscriber_id,name FROM dog_profiles"
        ).fetchone()
        payload_count = conn.execute(
            "SELECT COUNT(*) FROM subscription_registration_payloads"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(registration["status"], "completed")
        self.assertEqual(payload_count, 0)
        self.assertEqual(dog["registration_id"], registration["id"])
        self.assertEqual(dog["subscriber_id"], registration["subscriber_id"])
        self.assertEqual(dog["name"], "토리")

    def test_130_missing_encryption_configuration_rolls_back_registration(self):
        self.enable_subscription_registration()
        self.app.config["SUBSCRIPTION_REGISTRATION_ENCRYPTION_KEY"] = ""
        response = self.client.post(
            "/subscription/register", data=self.registration_form_data()
        )
        self.assertEqual(response.status_code, 503)
        conn = connect(self.db_path)
        for table in (
            "subscription_registrations",
            "subscription_registration_payloads",
            "dog_profiles",
        ):
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        conn.close()

    def test_131_additive_upgrade_from_plaintext_schema_uses_encrypted_payload(self):
        legacy_path = str(Path(self.temp.name) / "legacy-registration.db")
        legacy_app = create_app({
            "TESTING": True,
            "SECRET_KEY": "legacy-secret",
            "DB_PATH": legacy_path,
            "ENABLE_TEST_IDENTITY": False,
            "SEED_DEMO_DATA": False,
            "MIGRATION_HASH_SECRET": "migration-test-secret",
            "SUBSCRIPTION_REGISTRATION_ENCRYPTION_KEY": (
                "BVNXRQRh_hvpCeOkdlibHHdypT6aujBxlG5Oj_Rd7Ok="
            ),
        })
        with transaction(legacy_path) as conn:
            conn.execute("DROP TABLE subscription_registration_payloads")
            conn.execute("DROP TABLE dog_profiles")
            conn.execute("DROP TABLE subscription_registrations")
            conn.executescript(
                """
                CREATE TABLE subscription_registrations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    public_id TEXT NOT NULL UNIQUE,
                    subscriber_id INTEGER REFERENCES subscribers(id) ON DELETE SET NULL,
                    email_hash TEXT NOT NULL,
                    email TEXT NOT NULL,
                    guardian_name TEXT NOT NULL,
                    contact_phone TEXT NOT NULL,
                    payer_name TEXT,
                    plan_code TEXT NOT NULL CHECK(plan_code IN ('one-month','three-month')),
                    registration_type TEXT NOT NULL CHECK(registration_type IN ('new','renewal')),
                    interests TEXT,
                    privacy_agreed_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );
                """
            )
        legacy_app = create_app({
            "TESTING": True,
            "SECRET_KEY": "legacy-secret",
            "DB_PATH": legacy_path,
            "ENABLE_TEST_IDENTITY": False,
            "SEED_DEMO_DATA": False,
            "MIGRATION_HASH_SECRET": "migration-test-secret",
            "SUBSCRIPTION_REGISTRATION_ENCRYPTION_KEY": (
                "BVNXRQRh_hvpCeOkdlibHHdypT6aujBxlG5Oj_Rd7Ok="
            ),
        })
        legacy_client = legacy_app.test_client()
        with transaction(legacy_path) as conn:
            conn.execute("UPDATE portal_settings SET subscription_page_enabled=1 WHERE id=1")
        self.set_csrf(legacy_client, "registration-csrf")
        response = legacy_client.post(
            "/subscription/register", data=self.registration_form_data()
        )
        self.assertEqual(response.status_code, 302)
        conn = connect(legacy_path)
        legacy_row = conn.execute(
            "SELECT email,guardian_name,contact_phone,payer_name,interests FROM subscription_registrations"
        ).fetchone()
        payload_count = conn.execute(
            "SELECT COUNT(*) FROM subscription_registration_payloads"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(tuple(legacy_row), ("", "", "", None, None))
        self.assertEqual(payload_count, 1)

    def test_132_admin_registration_list_requires_admin_authentication(self):
        response = self.client.get("/admin/subscription-registrations")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            urlsplit(response.headers["Location"]).path,
            "/admin/login",
        )

    def test_133_admin_registration_list_shows_pending_original_submission(self):
        self.enable_subscription_registration()
        self.client.post(
            "/subscription/register",
            data=self.registration_form_data(
                guardian_name="원본 보호자",
                payer_name="원본 결제자",
                dog_name="원본 반려견",
                email="original@example.invalid",
                contact_phone="010-9876-5432",
                dog_birth_date="2019-04-03",
                dog_breed="푸들",
                interests="신장 건강",
            ),
        )
        conn = connect(self.db_path)
        registration_id = conn.execute(
            "SELECT public_id FROM subscription_registrations"
        ).fetchone()[0]
        conn.close()
        with self.client.session_transaction() as state:
            state["is_admin"] = True
        dashboard = self.client.get("/admin").get_data(as_text=True)
        self.assertIn("구독 신청 관리", dashboard)
        response = self.client.get("/admin/subscription-registrations")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        html = response.get_data(as_text=True)
        for expected in (
            "원본 보호자",
            "원본 결제자",
            "원본 반려견",
            "original@example.invalid",
            "01098765432",
            "2019-04-03",
            "푸들",
            "신장 건강",
            registration_id,
            "신규",
            "3개월",
            "처리 대기",
            "pending",
        ):
            self.assertIn(expected, html)

    def test_134_admin_registration_list_shows_completed_without_reconstructed_pii(self):
        self.enable_subscription_registration()
        self.client.post(
            "/subscription/register",
            data=self.registration_form_data(
                email="completed-original@example.invalid",
                guardian_name="완료 원본 보호자",
                contact_phone="010-2222-3333",
            ),
        )
        self.client.post(
            "/api/subscribers/sync",
            headers={"Authorization": "Bearer sync-test-secret"},
            json={
                "email": "completed-original@example.invalid",
                "display_name": "다른 표시명",
                "is_paid_subscriber": True,
            },
        )
        conn = connect(self.db_path)
        registration_id = conn.execute(
            "SELECT public_id FROM subscription_registrations"
        ).fetchone()[0]
        conn.close()
        completed = self.client.post(
            f"/api/subscription-registrations/{registration_id}/complete",
            headers={"Authorization": "Bearer registration-test-secret"},
        )
        self.assertEqual(completed.status_code, 200)
        with self.client.session_transaction() as state:
            state["is_admin"] = True
        response = self.client.get("/admin/subscription-registrations")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn(registration_id, html)
        self.assertIn("처리 완료", html)
        self.assertIn("completed", html)
        self.assertIn("처리 완료 · 개인정보 삭제", html)
        self.assertNotIn("completed-original@example.invalid", html)
        self.assertNotIn("완료 원본 보호자", html)
        self.assertNotIn("01022223333", html)

    def test_135_admin_subscriber_management_requires_admin_and_shows_exact_states(self):
        denied = self.client.get("/admin/subscribers")
        self.assertEqual(denied.status_code, 302)
        self.assertEqual(urlsplit(denied.headers["Location"]).path, "/admin/login")

        with transaction(self.db_path) as conn:
            paid_id = conn.execute(
                """INSERT INTO subscribers
                   (public_id,display_name,email_hash,is_test,is_active,is_paid_subscriber,created_at)
                   VALUES(?,?,?,0,1,1,?)""",
                (
                    "sub_paid_visible",
                    "현재 유료 회원",
                    email_hash("private-paid@example.invalid", "migration-test-secret"),
                    utcnow(),
                ),
            ).lastrowid
            conn.execute(
                """INSERT INTO subscribers
                   (public_id,display_name,email_hash,is_test,is_active,is_paid_subscriber,created_at)
                   VALUES(?,?,?,0,0,1,?)""",
                (
                    "sub_paid_inactive",
                    "비활성 유료 회원",
                    email_hash("private-inactive@example.invalid", "migration-test-secret"),
                    utcnow(),
                ),
            )
        _, episode, question_ids = self.ids()
        self.finish_attempt(paid_id, episode, question_ids)

        with self.client.session_transaction() as state:
            state["is_admin"] = True
        response = self.client.get("/admin/subscribers")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for expected in (
            "구독자 관리",
            "현재 유료 회원",
            "sub_paid_visible",
            "현재 유료",
            "비활성",
            "현재 유료 아님",
            "참여한 회차 수",
        ):
            self.assertIn(expected, html)
        self.assertNotIn("무료/종료", html)
        self.assertNotIn("private-paid@example.invalid", html)
        self.assertRegex(
            html,
            r"sub_paid_visible</a></td><td>활성</td><td>현재 유료</td><td>1</td>",
        )

    def test_136_admin_dashboard_shows_unambiguous_subscriber_totals(self):
        with transaction(self.db_path) as conn:
            conn.execute(
                """INSERT INTO subscribers
                   (public_id,display_name,is_test,is_active,is_paid_subscriber,created_at)
                   VALUES('sub_paid_active','유료 활성',0,1,1,?)""",
                (utcnow(),),
            )
            conn.execute(
                """INSERT INTO subscribers
                   (public_id,display_name,is_test,is_active,is_paid_subscriber,created_at)
                   VALUES('sub_paid_inactive','유료 비활성',0,0,1,?)""",
                (utcnow(),),
            )
        with self.client.session_transaction() as state:
            state["is_admin"] = True
        html = self.client.get("/admin").get_data(as_text=True)
        self.assertIn("구독자 관리", html)
        self.assertIn("<span>전체 subscriber</span><strong>4</strong>", html)
        self.assertIn("<span>현재 유료</span><strong>2</strong>", html)
        self.assertIn("<span>현재 유료 아님</span><strong>2</strong>", html)
        self.assertIn("<span>비활성 계정 (중복 가능)</span><strong>1</strong>", html)
        self.assertIn("콘텐츠 현황", html)

    def test_137_admin_registration_status_filters_and_completed_privacy_label(self):
        self.enable_subscription_registration()
        self.client.post(
            "/subscription/register",
            data=self.registration_form_data(
                email="completed-filter@example.invalid",
                guardian_name="완료 필터 보호자",
            ),
        )
        conn = connect(self.db_path)
        completed_id = conn.execute(
            "SELECT public_id FROM subscription_registrations"
        ).fetchone()[0]
        conn.close()
        self.client.post(
            "/api/subscribers/sync",
            headers={"Authorization": "Bearer sync-test-secret"},
            json={"email": "completed-filter@example.invalid"},
        )
        self.client.post(
            f"/api/subscription-registrations/{completed_id}/complete",
            headers={"Authorization": "Bearer registration-test-secret"},
        )
        self.client.post(
            "/subscription/register",
            data=self.registration_form_data(
                email="pending-filter@example.invalid",
                guardian_name="대기 필터 보호자",
            ),
        )
        conn = connect(self.db_path)
        pending_id = conn.execute(
            """SELECT public_id FROM subscription_registrations
               WHERE status='pending'"""
        ).fetchone()[0]
        conn.close()

        with self.client.session_transaction() as state:
            state["is_admin"] = True
        pending_html = self.client.get(
            "/admin/subscription-registrations?status=pending"
        ).get_data(as_text=True)
        completed_html = self.client.get(
            "/admin/subscription-registrations?status=completed"
        ).get_data(as_text=True)
        all_html = self.client.get(
            "/admin/subscription-registrations?status=all"
        ).get_data(as_text=True)
        self.assertIn(pending_id, pending_html)
        self.assertNotIn(completed_id, pending_html)
        self.assertIn(completed_id, completed_html)
        self.assertNotIn(pending_id, completed_html)
        self.assertIn("처리 완료 · 개인정보 삭제", completed_html)
        self.assertNotIn("완료 필터 보호자", completed_html)
        self.assertIn(pending_id, all_html)
        self.assertIn(completed_id, all_html)

    def test_138_admin_subscriber_detail_shows_real_activity_without_email_hash(self):
        subscriber_id = self.create_community_subscriber(
            "detail-private@example.invalid", "상세 확인 회원", paid=False
        )
        _, episode, question_ids = self.ids()
        self.finish_attempt(subscriber_id, episode, question_ids)
        conn = connect(self.db_path)
        feedback_questions = conn.execute(
            """SELECT id,response_type FROM feedback_questions
               WHERE episode_id=? ORDER BY display_order""",
            (episode["id"],),
        ).fetchall()
        stored_hash = conn.execute(
            "SELECT email_hash FROM subscribers WHERE id=?", (subscriber_id,)
        ).fetchone()[0]
        conn.close()
        save_feedback(
            self.db_path,
            episode["id"],
            subscriber_id,
            {
                feedback_questions[0]["id"]: ["핵심 개념"],
                feedback_questions[1]["id"]: "5",
            },
        )

        denied = self.client.get(f"/admin/subscribers/{subscriber_id}")
        self.assertEqual(denied.status_code, 302)
        self.assertEqual(urlsplit(denied.headers["Location"]).path, "/admin/login")

        with self.client.session_transaction() as state:
            state["is_admin"] = True
        response = self.client.get(f"/admin/subscribers/{subscriber_id}")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for expected in (
            "상세 확인 회원",
            "현재 유료 아님",
            "Quiz 시도",
            "완료 시도",
            "피드백",
            "R041",
            "삭제 영향 확인",
        ):
            self.assertIn(expected, html)
        self.assertNotIn("detail-private@example.invalid", html)
        self.assertNotIn(stored_hash, html)

    def test_139_admin_subscriber_delete_is_confirmed_atomic_and_fk_safe(self):
        target_id = self.create_community_subscriber(
            "delete-target@example.invalid", "삭제 대상", paid=True
        )
        other_id = self.create_community_subscriber(
            "delete-other@example.invalid", "보존 대상", paid=True
        )
        _, episode, question_ids = self.ids()
        self.finish_attempt(target_id, episode, question_ids)
        conn = connect(self.db_path)
        feedback_question = conn.execute(
            """SELECT id FROM feedback_questions WHERE episode_id=?
               ORDER BY display_order LIMIT 1""",
            (episode["id"],),
        ).fetchone()[0]
        conn.close()
        save_feedback(
            self.db_path,
            episode["id"],
            target_id,
            {feedback_question: ["핵심 개념"]},
        )
        create_magic_link_token(self.db_path, target_id, "/", 15)
        target_post = self.create_community_post(target_id, "삭제될 글")
        other_post = self.create_community_post(other_id, "보존될 글")
        now = utcnow()
        with transaction(self.db_path) as conn:
            conn.execute(
                """INSERT INTO legacy_participation
                   (subscriber_id,season_code,participation_count,note,updated_at)
                   VALUES(?, 'S1', 2, '삭제 테스트', ?)""",
                (target_id, now),
            )
            conn.execute(
                """INSERT INTO community_comments
                   (post_id,subscriber_id,body,created_at) VALUES(?,?,?,?)""",
                (other_post, target_id, "대상이 쓴 댓글", now),
            )
            conn.execute(
                """INSERT INTO community_comments
                   (post_id,subscriber_id,body,created_at) VALUES(?,?,?,?)""",
                (target_post, other_id, "대상 글의 타인 댓글", now),
            )
            conn.execute(
                """INSERT INTO community_likes
                   (post_id,subscriber_id,created_at) VALUES(?,?,?)""",
                (other_post, target_id, now),
            )
            conn.execute(
                """INSERT INTO community_likes
                   (post_id,subscriber_id,created_at) VALUES(?,?,?)""",
                (target_post, other_id, now),
            )
            registration_id = conn.execute(
                """INSERT INTO subscription_registrations
                   (public_id,subscriber_id,email_hash,plan_code,registration_type,
                    privacy_agreed_at,status,created_at,updated_at,completed_at)
                   VALUES(?,?,?,?,?,?,'completed',?,?,?)""",
                (
                    "reg_delete_target",
                    target_id,
                    email_hash("delete-target@example.invalid", "migration-test-secret"),
                    "one-month",
                    "new",
                    now,
                    now,
                    now,
                    now,
                ),
            ).lastrowid
            conn.execute(
                """INSERT INTO dog_profiles
                   (subscriber_id,registration_id,name,created_at,updated_at)
                   VALUES(?,?,?,?,?)""",
                (target_id, registration_id, "보존견", now, now),
            )

        delete_path = f"/admin/subscribers/{target_id}/delete"
        denied = self.client.get(delete_path)
        self.assertEqual(denied.status_code, 302)
        with self.client.session_transaction() as state:
            state["csrf_token"] = "anonymous-delete-csrf"
        denied_post = self.client.post(
            delete_path,
            data={
                "csrf_token": "anonymous-delete-csrf",
                "confirm_delete": "yes",
            },
        )
        self.assertEqual(denied_post.status_code, 302)
        self.assertEqual(
            urlsplit(denied_post.headers["Location"]).path, "/admin/login"
        )
        with self.client.session_transaction() as state:
            state["is_admin"] = True
            state["csrf_token"] = "delete-csrf"
        confirmation = self.client.get(delete_path)
        self.assertEqual(confirmation.status_code, 200)
        confirmation_html = confirmation.get_data(as_text=True)
        self.assertIn("삭제 대상", confirmation_html)
        self.assertIn("복구할 수 없습니다", confirmation_html)
        self.assertIn("보존되지만 subscriber 연결이 해제", confirmation_html)
        conn = connect(self.db_path)
        self.assertIsNotNone(
            conn.execute("SELECT id FROM subscribers WHERE id=?", (target_id,)).fetchone()
        )
        conn.close()

        self.assertEqual(
            self.client.post(
                delete_path, data={"confirm_delete": "yes"}
            ).status_code,
            400,
        )
        deleted = self.client.post(
            delete_path,
            data={"csrf_token": "delete-csrf", "confirm_delete": "yes"},
        )
        self.assertEqual(deleted.status_code, 302)
        self.assertEqual(urlsplit(deleted.headers["Location"]).path, "/admin/subscribers")

        conn = connect(self.db_path)
        self.assertIsNone(
            conn.execute("SELECT id FROM subscribers WHERE id=?", (target_id,)).fetchone()
        )
        for table in (
            "quiz_attempts",
            "participation",
            "legacy_participation",
            "feedback_submissions",
            "magic_link_tokens",
            "community_posts",
            "community_comments",
            "community_likes",
        ):
            self.assertEqual(
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE subscriber_id=?",
                    (target_id,),
                ).fetchone()[0],
                0,
            )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM attempt_answers").fetchone()[0], 0
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM feedback_answers").fetchone()[0], 0
        )
        self.assertIsNotNone(
            conn.execute("SELECT id FROM subscribers WHERE id=?", (other_id,)).fetchone()
        )
        self.assertIsNotNone(
            conn.execute("SELECT id FROM community_posts WHERE id=?", (other_post,)).fetchone()
        )
        self.assertIsNone(
            conn.execute("SELECT id FROM community_posts WHERE id=?", (target_post,)).fetchone()
        )
        registration = conn.execute(
            "SELECT subscriber_id FROM subscription_registrations WHERE id=?",
            (registration_id,),
        ).fetchone()
        dog = conn.execute(
            "SELECT subscriber_id,name FROM dog_profiles WHERE registration_id=?",
            (registration_id,),
        ).fetchone()
        conn.close()
        self.assertIsNone(registration["subscriber_id"])
        self.assertIsNone(dog["subscriber_id"])
        self.assertEqual(dog["name"], "보존견")

    def test_140_subscriber_delete_failure_rolls_back_partial_work(self):
        target_id = self.create_community_subscriber(
            "delete-rollback@example.invalid", "롤백 대상", paid=True
        )
        create_magic_link_token(self.db_path, target_id, "/", 15)

        def fail_after_first_delete(conn, subscriber_id):
            conn.execute(
                "DELETE FROM magic_link_tokens WHERE subscriber_id=?",
                (subscriber_id,),
            )
            raise RuntimeError("forced deletion failure")

        with self.client.session_transaction() as state:
            state["is_admin"] = True
            state["csrf_token"] = "rollback-csrf"
        with patch("app.delete_subscriber_data", side_effect=fail_after_first_delete):
            response = self.client.post(
                f"/admin/subscribers/{target_id}/delete",
                data={"csrf_token": "rollback-csrf", "confirm_delete": "yes"},
            )
        self.assertEqual(response.status_code, 302)
        conn = connect(self.db_path)
        self.assertIsNotNone(
            conn.execute("SELECT id FROM subscribers WHERE id=?", (target_id,)).fetchone()
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM magic_link_tokens WHERE subscriber_id=?",
                (target_id,),
            ).fetchone()[0],
            1,
        )
        conn.close()

    def test_141_official_portal_domain_magic_link_keeps_authenticated_browser_on_portal(self):
        subscriber_id = self.create_real_subscriber(
            email="portal-domain@example.invalid", display_name="공식 도메인 회원"
        )
        with transaction(self.db_path) as conn:
            conn.execute(
                "UPDATE subscribers SET is_paid_subscriber=1 WHERE id=?",
                (subscriber_id,),
            )
        sent = []
        _, client = self.production_client(
            lambda email, url, config: sent.append((email, url))
        )
        with client.session_transaction(
            base_url="https://portal.dognutritionlab.com"
        ) as state:
            state["csrf_token"] = "portal-domain-csrf"
        csrf = "portal-domain-csrf"
        requested = client.post(
            "/auth/email",
            base_url="https://portal.dognutritionlab.com",
            data={
                "csrf_token": csrf,
                "email": "portal-domain@example.invalid",
                "next": "/community",
            },
        )
        self.assertEqual(requested.status_code, 200)
        self.assertEqual(len(sent), 1)
        magic_url = sent[0][1]
        parsed = urlsplit(magic_url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "portal.dognutritionlab.com")
        self.assertEqual(parsed.path, "/auth/verify")
        self.assertNotIn("audioletter-quiz-app-production.up.railway.app", magic_url)

        verified = client.get(
            f"{parsed.path}?{parsed.query}",
            base_url="https://portal.dognutritionlab.com",
        )
        self.assertEqual(verified.status_code, 302)
        self.assertEqual(verified.headers["Location"], "/community")
        community = client.get(
            verified.headers["Location"],
            base_url="https://portal.dognutritionlab.com",
        )
        self.assertEqual(community.status_code, 200)
        with client.session_transaction(
            base_url="https://portal.dognutritionlab.com"
        ) as state:
            self.assertEqual(state["subscriber_id"], subscriber_id)


    def test_audioletter_table_is_additive_and_separate_from_quiz_episodes(self):
        conn = connect(self.db_path)
        quiz_before = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        quiz_columns_before = [
            row["name"] for row in conn.execute("PRAGMA table_info(episodes)")
        ]
        self.assertIn("audioletter_episodes", {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        })
        conn.close()

        legacy_db = str(Path(self.temp.name) / "audioletter-table-upgrade.db")
        create_app({"TESTING": True, "DB_PATH": legacy_db, "SEED_DEMO_DATA": False})
        create_app({"TESTING": True, "DB_PATH": legacy_db, "SEED_DEMO_DATA": False})
        migrated = connect(legacy_db)
        self.assertEqual(migrated.execute(
            "SELECT COUNT(*) FROM audioletter_episodes"
        ).fetchone()[0], 0)
        migrated.close()

        with transaction(self.db_path) as conn:
            conn.execute(
                """INSERT INTO audioletter_episodes
                   (sequence,season,season_episode,title,transcript,created_at,updated_at)
                   VALUES(42,1,42,'마지막 시즌1 회차','원고',?,?)""",
                (utcnow(), utcnow()),
            )
            conn.execute(
                """INSERT INTO audioletter_episodes
                   (sequence,season,season_episode,title,transcript,created_at,updated_at)
                   VALUES(43,2,1,'첫 시즌2 회차','원고',?,?)""",
                (utcnow(), utcnow()),
            )
        conn = connect(self.db_path)
        episodes = conn.execute(
            "SELECT id,sequence,season,season_episode,is_published "
            "FROM audioletter_episodes ORDER BY sequence"
        ).fetchall()
        self.assertNotEqual(episodes[0]["id"], episodes[0]["sequence"])
        self.assertEqual(
            [(e["sequence"], e["season"], e["season_episode"]) for e in episodes],
            [(42, 1, 42), (43, 2, 1)],
        )
        self.assertEqual([e["is_published"] for e in episodes], [0, 0])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0], quiz_before)
        self.assertEqual(
            [row["name"] for row in conn.execute("PRAGMA table_info(episodes)")],
            quiz_columns_before,
        )
        conn.close()

        for sequence, season, season_episode in ((43, 3, 1), (44, 2, 1)):
            with self.assertRaises(sqlite3.IntegrityError):
                with transaction(self.db_path) as conn:
                    conn.execute(
                        """INSERT INTO audioletter_episodes
                           (sequence,season,season_episode,title,created_at,updated_at)
                           VALUES(?,?,?,?,?,?)""",
                        (sequence, season, season_episode, "중복", utcnow(), utcnow()),
                    )

    def test_audioletter_admin_create_edit_and_preserve_long_plain_transcript(self):
        self.assertNotEqual(self.client.get("/admin/audioletters").status_code, 200)
        with self.client.session_transaction() as state:
            state["is_admin"] = True
            state["csrf_token"] = "audioletter-admin-csrf"
        transcript = ("첫 문단\n\n  들여쓴 줄\n" * 600) + "<script>alert(1)</script>"
        data = {
            "csrf_token": "audioletter-admin-csrf",
            "sequence": "43", "season": "2", "season_episode": "1",
            "title": "시즌2 첫 회차", "audio_storage_key": "season2/episode-01.mp3",
            "transcript": transcript,
        }
        created = self.client.post("/admin/audioletters/new", data=data)
        self.assertEqual(created.status_code, 302)
        conn = connect(self.db_path)
        episode = conn.execute(
            "SELECT * FROM audioletter_episodes WHERE sequence=43"
        ).fetchone()
        conn.close()
        self.assertEqual(episode["season"], 2)
        self.assertEqual(episode["season_episode"], 1)
        self.assertEqual(episode["audio_storage_key"], "season2/episode-01.mp3")
        self.assertEqual(episode["transcript"], transcript)
        self.assertEqual(episode["is_published"], 0)
        self.assertIn("시즌2 · 1회", self.client.get("/admin/audioletters").get_data(as_text=True))
        edit_url = f"/admin/audioletters/{episode['id']}/edit"
        edit_page = self.client.get(edit_url).get_data(as_text=True)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", edit_page)
        self.assertNotIn("<script>alert(1)</script>", edit_page)

        changed = self.client.post(
            edit_url, data=dict(data, title="수정한 회차", is_published="on")
        )
        self.assertEqual(changed.status_code, 302)
        conn = connect(self.db_path)
        updated = conn.execute(
            "SELECT id,title,transcript,is_published FROM audioletter_episodes WHERE sequence=43"
        ).fetchone()
        conn.close()
        self.assertEqual(updated["id"], episode["id"])
        self.assertEqual(updated["title"], "수정한 회차")
        self.assertEqual(updated["transcript"], transcript)
        self.assertEqual(updated["is_published"], 1)

        duplicate = self.client.post(
            "/admin/audioletters/new", data=dict(data, season_episode="2")
        )
        self.assertEqual(duplicate.status_code, 200)
        conn = connect(self.db_path)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM audioletter_episodes WHERE sequence=43"
        ).fetchone()[0], 1)
        conn.close()

    def test_audioletter_content_lookup_requires_published_paid_active_and_range(self):
        subscriber_id = self.create_real_subscriber("audio-access@example.invalid")
        with transaction(self.db_path) as conn:
            conn.execute(
                "UPDATE subscribers SET is_paid_subscriber=1,accessible_through=43 WHERE id=?",
                (subscriber_id,),
            )
            episode_ids = []
            for sequence, published in ((42, 1), (43, 1), (44, 1), (41, 0)):
                episode_ids.append(conn.execute(
                    """INSERT INTO audioletter_episodes
                       (sequence,season,season_episode,title,transcript,is_published,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (sequence, 1 if sequence <= 42 else 2,
                     sequence if sequence <= 42 else sequence - 42,
                     "테스트 회차", "보호할 원고", published, utcnow(), utcnow()),
                ).lastrowid)

        conn = connect(self.db_path)
        self.assertIsNotNone(accessible_audioletter_episode(conn, subscriber_id, episode_ids[0]))
        self.assertIsNotNone(accessible_audioletter_episode(conn, subscriber_id, episode_ids[1]))
        self.assertIsNone(accessible_audioletter_episode(conn, subscriber_id, episode_ids[2]))
        self.assertIsNone(accessible_audioletter_episode(conn, subscriber_id, episode_ids[3]))
        self.assertIsNone(accessible_audioletter_episode(conn, subscriber_id, 99999))
        self.assertIsNone(accessible_audioletter_episode(conn, 99999, episode_ids[1]))
        conn.close()

        for column in ("is_paid_subscriber", "is_active"):
            with transaction(self.db_path) as conn:
                conn.execute(f"UPDATE subscribers SET {column}=0 WHERE id=?", (subscriber_id,))
            conn = connect(self.db_path)
            self.assertIsNone(accessible_audioletter_episode(conn, subscriber_id, episode_ids[1]))
            conn.close()
            with transaction(self.db_path) as conn:
                conn.execute(f"UPDATE subscribers SET {column}=1 WHERE id=?", (subscriber_id,))

    def audioletter_fixture(self):
        subscriber_id = self.create_real_subscriber("audio-playback@example.invalid")
        with transaction(self.db_path) as conn:
            conn.execute(
                "UPDATE subscribers SET is_paid_subscriber=1,accessible_through=7 WHERE id=?",
                (subscriber_id,),
            )
            episodes = {}
            for sequence, published, key in (
                (7, 1, "audioletters/season1/007.mp3"),
                (8, 1, "audioletters/season1/008.mp3"),
                (6, 0, "audioletters/season1/006.mp3"),
                (5, 1, None),
            ):
                episodes[sequence] = conn.execute(
                    """INSERT INTO audioletter_episodes
                       (sequence,season,season_episode,title,audio_storage_key,
                        transcript,is_published,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (sequence, 1, sequence, f"회차 {sequence}", key,
                     "첫 문단\n\n둘째 줄<script>alert(1)</script>", published,
                     utcnow(), utcnow()),
                ).lastrowid
        return subscriber_id, episodes

    def test_audioletter_user_list_detail_and_entitlement(self):
        subscriber_id, episodes = self.audioletter_fixture()
        self.assertEqual(self.client.get("/audioletters").status_code, 302)
        self.assertEqual(self.client.get(f"/audioletters/{episodes[7]}").status_code, 302)
        self.assertEqual(self.client.get(f"/audioletters/{episodes[7]}/audio").status_code, 302)

        with self.client.session_transaction() as state:
            state["subscriber_id"] = subscriber_id
        listing = self.client.get("/audioletters")
        html = listing.get_data(as_text=True)
        self.assertEqual(listing.status_code, 200)
        self.assertIn("회차 7", html)
        self.assertIn("회차 5", html)
        self.assertNotIn("회차 8", html)
        self.assertNotIn("회차 6", html)
        self.assertEqual(listing.headers["Cache-Control"], "private, no-store")
        detail = self.client.get(f"/audioletters/{episodes[7]}")
        content = detail.get_data(as_text=True)
        self.assertEqual(detail.status_code, 200)
        self.assertIn(f'/audioletters/{episodes[7]}/audio', content)
        self.assertIn("<details>", content)
        self.assertIn("첫 문단\n\n둘째 줄", content)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", content)
        self.assertNotIn("<script>alert(1)</script>", content)
        self.assertIn("white-space:pre-wrap", (ROOT / "static/style.css").read_text())
        self.assertNotIn("<audio", self.client.get(f"/audioletters/{episodes[5]}").get_data(as_text=True))
        for sequence in (6, 8):
            self.assertEqual(self.client.get(f"/audioletters/{episodes[sequence]}").status_code, 404)
            with patch("audioletters.fetch_audio") as fetch:
                self.assertEqual(self.client.get(f"/audioletters/{episodes[sequence]}/audio").status_code, 404)
                fetch.assert_not_called()
        with patch("audioletters.fetch_audio") as fetch:
            self.assertEqual(self.client.get(f"/audioletters/{episodes[5]}/audio").status_code, 404)
            fetch.assert_not_called()

        with transaction(self.db_path) as conn:
            conn.execute("UPDATE subscribers SET is_paid_subscriber=0 WHERE id=?", (subscriber_id,))
        self.assertEqual(self.client.get("/audioletters").status_code, 403)
        self.assertEqual(self.client.get(f"/audioletters/{episodes[7]}").status_code, 403)
        with patch("audioletters.fetch_audio") as fetch:
            self.assertEqual(self.client.get(f"/audioletters/{episodes[7]}/audio").status_code, 403)
            fetch.assert_not_called()
        with transaction(self.db_path) as conn:
            conn.execute("UPDATE subscribers SET is_paid_subscriber=1,is_active=0 WHERE id=?", (subscriber_id,))
        for url in ("/audioletters", f"/audioletters/{episodes[7]}",
                    f"/audioletters/{episodes[7]}/audio"):
            self.assertEqual(self.client.get(url).status_code, 302)

    def test_audioletter_player_hides_browser_download_ui(self):
        subscriber_id, episodes = self.audioletter_fixture()
        with self.client.session_transaction() as state:
            state["subscriber_id"] = subscriber_id
        detail = self.client.get(f"/audioletters/{episodes[7]}")
        self.assertEqual(detail.status_code, 200)
        html = detail.get_data(as_text=True)
        self.assertIn('<audio controls controlsList="nodownload"', html)
        self.assertIn('oncontextmenu="return false;"', html)
        self.assertIn(f'/audioletters/{episodes[7]}/audio', html)
        self.assertIn('<details>', html)

    def test_audioletter_stream_range_errors_and_revocation(self):
        subscriber_id, episodes = self.audioletter_fixture()
        with self.client.session_transaction() as state:
            state["subscriber_id"] = subscriber_id
        url = f"/audioletters/{episodes[7]}/audio"

        class FakeBody:
            def __init__(self, data):
                self.data = data
                self.closed = False
            def iter_chunks(self, chunk_size):
                yield self.data
            def close(self):
                self.closed = True

        def fake_fetch(config, key, byte_range):
            self.assertEqual(key, "audioletters/season1/007.mp3")
            payload = b"0123456789" if byte_range is None else b"456"
            body = FakeBody(payload)
            bodies.append(body)
            value = {"Body": body, "ContentLength": len(payload),
                     "ResponseMetadata": {"HTTPStatusCode": 200 if byte_range is None else 206}}
            if byte_range is not None:
                self.assertEqual(byte_range, "bytes=4-6")
                value["ContentRange"] = "bytes 4-6/10"
            return value

        bodies = []
        with patch("audioletters.fetch_audio", side_effect=fake_fetch):
            whole = self.client.get(url)
            self.assertEqual(whole.status_code, 200)
            self.assertEqual(whole.get_data(), b"0123456789")
            self.assertTrue(bodies[-1].closed)
            part = self.client.get(url, headers={"Range": "bytes=4-6"})
            self.assertEqual(part.status_code, 206)
            self.assertEqual(part.get_data(), b"456")
            self.assertEqual(part.headers["Content-Range"], "bytes 4-6/10")
            self.assertEqual(part.headers["Accept-Ranges"], "bytes")
            self.assertEqual(part.headers["Cache-Control"], "private, no-store")
            self.assertEqual(part.headers["Content-Type"], "audio/mpeg")
            self.assertTrue(bodies[-1].closed)
        with patch("audioletters.fetch_audio") as fetch:
            self.assertEqual(self.client.get(url, headers={"Range": "bytes=0-1,3-4"}).status_code, 416)
            fetch.assert_not_called()
        with patch("audioletters.fetch_audio", side_effect=ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "internal bucket key"}}, "GetObject"
        )):
            missing = self.client.get(url)
            self.assertEqual(missing.status_code, 404)
            self.assertNotIn(b"internal bucket key", missing.get_data())
        with patch("audioletters.fetch_audio", side_effect=ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "private credential"}}, "GetObject"
        )):
            failed = self.client.get(url)
            self.assertEqual(failed.status_code, 503)
            self.assertNotIn(b"private credential", failed.get_data())
        self.assertEqual(self.client.get(url).status_code, 503)  # No bucket variables configured.
        with transaction(self.db_path) as conn:
            conn.execute("UPDATE subscribers SET is_paid_subscriber=0 WHERE id=?", (subscriber_id,))
        with patch("audioletters.fetch_audio") as fetch:
            self.assertEqual(self.client.get(url).status_code, 403)
            fetch.assert_not_called()

    def test_audioletter_bucket_client_uses_explicit_railway_variables(self):
        with self.assertRaises(AudioStorageUnavailable):
            audio_client(self.app.config)
        config = dict(self.app.config,
                      AUDIOLETTER_BUCKET_NAME="bucket-example",
                      AUDIOLETTER_BUCKET_ENDPOINT="https://storage.example.invalid",
                      AUDIOLETTER_BUCKET_ACCESS_KEY_ID="fixture-id",
                      AUDIOLETTER_BUCKET_SECRET_ACCESS_KEY="fixture-secret",
                      AUDIOLETTER_BUCKET_ADDRESSING_STYLE="path")
        with patch("audio_storage.boto3.client") as make_client:
            audio_client(config)
            kwargs = make_client.call_args.kwargs
            self.assertEqual(kwargs["endpoint_url"], "https://storage.example.invalid")
            self.assertEqual(kwargs["aws_access_key_id"], "fixture-id")
            self.assertEqual(kwargs["config"].s3["addressing_style"], "path")
        config["AUDIOLETTER_BUCKET_ADDRESSING_STYLE"] = "invalid"
        with self.assertRaises(AudioStorageUnavailable):
            audio_client(config)

    def test_audioletter_blocks_migrate_additively_and_preserve_legacy_episode(self):
        legacy_path = str(Path(self.temp.name) / "legacy-blocks.db")
        conn = sqlite3.connect(legacy_path)
        conn.executescript("""
            CREATE TABLE episodes(id INTEGER PRIMARY KEY,code TEXT UNIQUE);
            CREATE TABLE audioletter_episodes (
                id INTEGER PRIMARY KEY,sequence INTEGER UNIQUE,season INTEGER,
                season_episode INTEGER,title TEXT,audio_storage_key TEXT,
                transcript TEXT,is_published INTEGER,created_at TEXT,updated_at TEXT);
            INSERT INTO audioletter_episodes VALUES
                (7,7,1,7,'기존 회차','audioletters/season1/007.mp3',
                 '기존 원문',1,'2026-09-01','2026-09-01');
        """)
        conn.commit()
        conn.close()
        from db import init_db
        init_db(legacy_path)
        init_db(legacy_path)
        conn = connect(legacy_path)
        legacy = conn.execute("SELECT * FROM audioletter_episodes WHERE id=7").fetchone()
        self.assertEqual(legacy["transcript"], "기존 원문")
        self.assertIsNone(legacy["quiz_episode_code"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM audioletter_blocks").fetchone()[0], 0)
        self.assertEqual(episode_blocks(conn, legacy)[0]["audio_storage_key"],
                         "audioletters/season1/007.mp3")
        conn.close()

    def test_audioletter_admin_blocks_order_promotion_and_safe_output(self):
        subscriber_id, episodes = self.audioletter_fixture()
        episode_id = episodes[7]
        with self.client.session_transaction() as state:
            state["subscriber_id"] = subscriber_id
            state["is_admin"] = True
            state["csrf_token"] = "blocks-csrf"
        self.assertIn("회차 7", self.client.get(f"/audioletters/{episode_id}").get_data(as_text=True))
        new_url = f"/admin/audioletters/{episode_id}/blocks/new"
        self.assertEqual(self.client.get(new_url).status_code, 200)
        info_data = {"csrf_token": "blocks-csrf", "sort_order": "2", "block_type": "info",
                     "title": "잠깐 쉬어가기", "body": "정보 첫 줄\n둘째 줄<script>alert(2)</script>"}
        self.assertEqual(self.client.post(new_url, data=info_data).status_code, 302)
        audio_data = {"csrf_token": "blocks-csrf", "sort_order": "3", "block_type": "audio",
                      "title": "추가 오디오", "audio_storage_key": "audioletters/season1/007-extra.mp3",
                      "transcript": "추가 원고\n\n둘째 문단"}
        self.assertEqual(self.client.post(new_url, data=audio_data).status_code, 302)
        conn = connect(self.db_path)
        episode = conn.execute("SELECT * FROM audioletter_episodes WHERE id=?", (episode_id,)).fetchone()
        blocks = episode_blocks(conn, episode)
        self.assertEqual([(b["sort_order"], b["block_type"]) for b in blocks],
                         [(1, "audio"), (2, "info"), (3, "audio")])
        self.assertEqual(blocks[0]["transcript"], episode["transcript"])
        self.assertEqual(episode["audio_storage_key"], "audioletters/season1/007.mp3")
        extra_id = blocks[2]["id"]
        conn.close()
        page = self.client.get(f"/audioletters/{episode_id}").get_data(as_text=True)
        self.assertLess(page.index("메인 오디오"), page.index("잠깐 쉬어가기"))
        self.assertLess(page.index("잠깐 쉬어가기"), page.index("추가 오디오"))
        self.assertIn("정보 첫 줄\n둘째 줄", page)
        self.assertIn("&lt;script&gt;alert(2)&lt;/script&gt;", page)
        self.assertNotIn("<script>alert(2)</script>", page)
        self.assertIn("추가 원고\n\n둘째 문단", page)
        self.assertEqual(page.count('controlsList="nodownload"'), 2)
        self.assertEqual(page.count("이 콘텐츠는 교육 및 정보 제공을 위한 자료"), 1)
        self.assertIn(f"/audioletters/{episode_id}/blocks/{extra_id}/audio", page)
        self.assertEqual(self.client.post(new_url, data=info_data).status_code, 200)
        with transaction(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM audioletter_blocks WHERE episode_id=?",
                                          (episode_id,)).fetchone()[0], 3)
        # Admin edits legacy metadata without overriding the canonical block body.
        edit_page = self.client.get(f"/admin/audioletters/{episode_id}/edit")
        self.assertIn("블록 추가", edit_page.get_data(as_text=True))
        self.assertIn("블록에서 수정", edit_page.get_data(as_text=True))
        edited = self.client.post(
            f"/admin/audioletters/{episode_id}/blocks/{extra_id}/edit",
            data=dict(audio_data, title="편집된 추가 오디오"),
        )
        self.assertEqual(edited.status_code, 302)
        self.assertIn("편집된 추가 오디오", self.client.get(f"/audioletters/{episode_id}").get_data(as_text=True))
        self.assertEqual(self.client.post(new_url, data=dict(audio_data,
                             sort_order="4", audio_storage_key="https://public.invalid/a.mp3")).status_code, 200)

    def test_audioletter_block_audio_entitlement_and_each_range(self):
        subscriber_id, episodes = self.audioletter_fixture()
        episode_id = episodes[7]
        with self.client.session_transaction() as state:
            state["subscriber_id"] = subscriber_id
            state["is_admin"] = True
            state["csrf_token"] = "block-audio-csrf"
        with transaction(self.db_path) as conn:
            from audioletters import _promote_legacy_block
            episode = conn.execute("SELECT * FROM audioletter_episodes WHERE id=?", (episode_id,)).fetchone()
            _promote_legacy_block(conn, episode)
            extra_id = conn.execute(
                """INSERT INTO audioletter_blocks
                   (episode_id,sort_order,block_type,title,audio_storage_key,created_at,updated_at)
                   VALUES(?,2,'audio','추가','audioletters/season1/007-extra.mp3',?,?)""",
                (episode_id, utcnow(), utcnow()),
            ).lastrowid
            main_id = conn.execute("SELECT id FROM audioletter_blocks WHERE episode_id=? AND sort_order=1",
                                   (episode_id,)).fetchone()[0]

        class FakeBody:
            def iter_chunks(self, chunk_size):
                yield b"abc"
            def close(self):
                pass

        keys = []
        def fake_fetch(config, key, byte_range):
            keys.append((key, byte_range))
            return {"Body": FakeBody(), "ContentLength": 3,
                    "ContentRange": "bytes 0-2/10",
                    "ResponseMetadata": {"HTTPStatusCode": 206}}

        with patch("audioletters.fetch_audio", side_effect=fake_fetch):
            for block_id in (main_id, extra_id):
                res = self.client.get(f"/audioletters/{episode_id}/blocks/{block_id}/audio",
                                      headers={"Range": "bytes=0-2"})
                self.assertEqual(res.status_code, 206)
                self.assertEqual(res.get_data(), b"abc")
                self.assertEqual(res.headers["Content-Range"], "bytes 0-2/10")
            self.assertEqual(self.client.get(f"/audioletters/{episode_id}/audio",
                             headers={"Range": "bytes=0-2"}).status_code, 206)
        self.assertEqual(keys[0][0], "audioletters/season1/007.mp3")
        self.assertEqual(keys[1][0], "audioletters/season1/007-extra.mp3")
        self.assertEqual(keys[2][0], "audioletters/season1/007.mp3")
        with patch("audioletters.fetch_audio", side_effect=ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "sensitive object name"}}, "GetObject"
        )):
            missing = self.client.get(f"/audioletters/{episode_id}/blocks/{extra_id}/audio")
            self.assertEqual(missing.status_code, 404)
            self.assertNotIn(b"sensitive object name", missing.get_data())
        with patch("audioletters.fetch_audio") as fetch:
            self.assertEqual(self.client.get(f"/audioletters/{episode_id}/blocks/999999/audio").status_code, 404)
            fetch.assert_not_called()
        for column, value, expected in (("accessible_through", 6, 404),
                                        ("is_paid_subscriber", 0, 403),
                                        ("is_active", 0, 302)):
            with transaction(self.db_path) as conn:
                conn.execute(f"UPDATE subscribers SET {column}=? WHERE id=?", (value, subscriber_id))
            with patch("audioletters.fetch_audio") as fetch:
                self.assertEqual(self.client.get(f"/audioletters/{episode_id}/blocks/{extra_id}/audio").status_code,
                                 expected)
                fetch.assert_not_called()
            with transaction(self.db_path) as conn:
                conn.execute(f"UPDATE subscribers SET {column}=? WHERE id=?", (7 if column == "accessible_through" else 1, subscriber_id))
            with self.client.session_transaction() as state:
                state["subscriber_id"] = subscriber_id
        with transaction(self.db_path) as conn:
            conn.execute("UPDATE audioletter_episodes SET is_published=0 WHERE id=?", (episode_id,))
        with patch("audioletters.fetch_audio") as fetch:
            self.assertEqual(self.client.get(f"/audioletters/{episode_id}/blocks/{extra_id}/audio").status_code,404)
            fetch.assert_not_called()

    def test_audioletter_explicit_quiz_link_uses_existing_participation_feedback(self):
        subscriber_id, episodes = self.audioletter_fixture()
        with self.client.session_transaction() as state:
            state["subscriber_id"] = subscriber_id
            state["is_admin"] = True
            state["csrf_token"] = "quiz-link-csrf"
        conn = connect(self.db_path)
        quiz = conn.execute("SELECT id FROM episodes WHERE code='R041'").fetchone()
        conn.close()
        with transaction(self.db_path) as conn:
            conn.execute("UPDATE audioletter_episodes SET quiz_episode_code='R041' WHERE id=?",
                         (episodes[7],))
            conn.execute("UPDATE episodes SET is_published=1 WHERE id=?", (quiz["id"],))
        url = f"/audioletters/{episodes[7]}"
        before = self.client.get(url).get_data(as_text=True)
        self.assertIn("/quiz?episode=R041", before)
        self.assertNotIn("/episode/R041/feedback", before)
        with transaction(self.db_path) as conn:
            conn.execute("""INSERT INTO participation
                         (subscriber_id,episode_id,first_completed_at,source)
                         VALUES(?,?,?,'app')""", (subscriber_id, quiz["id"], utcnow()))
        after = self.client.get(url).get_data(as_text=True)
        self.assertIn("/episode/R041/feedback", after)
        with transaction(self.db_path) as conn:
            conn.execute("UPDATE episodes SET is_published=0 WHERE id=?", (quiz["id"],))
        self.assertNotIn("/quiz?episode=R041", self.client.get(url).get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
