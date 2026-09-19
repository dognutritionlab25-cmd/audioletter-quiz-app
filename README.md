# Quiz/Participation App v0.1

반려견 영양 오디오레터의 회차별 이해 테스트, 참여 기록, 선택적 피드백을 한곳에서 관리하는 독립 앱입니다. Production Google Sheets, Make, Brevo, Notion, 기존 Google Forms와 연결하거나 변경하지 않습니다.

## 주요 기능

- 모바일 우선 구독자 화면과 한 문제씩 진행하는 Quiz
- 회차별 가변 문항 수, 자동 채점, 선택적 해설
- 재응시 허용 + `subscriber_id, episode_id` DB 유일 제약으로 참여 1회만 인정
- 시즌/누적 참여 횟수와 과거 참여 회차
- Quiz 완료와 분리된 선택적 피드백
- 관리자 인증, 시즌/회차/문항/선택지/정답/배점/해설 관리
- 회차·구독자·점수·피드백 현황 및 CSV export
- Google Forms JSON, 과거 응답 CSV, 익명 피드백 CSV용 분리된 import/migration 계층
- Production 인증과 분리된 테스트 신원 선택 화면
- Brevo Transactional Email 기반 일회용 Magic Link 인증과 180일 장기 세션
- 관리자 구독자 등록과 시즌1 과거 참여 횟수 수동 반영

## 파일 구조

```text
app.py                 Flask routes, 화면, 관리자 기능
auth.py                교체 가능한 신원/관리자 경계
db.py                  SQLite schema와 transaction
magic_links.py         일회용 token과 Brevo Transactional Email 발송
presenters.py           관리자 표시용 날짜·피드백 변환
services.py            Quiz·참여·피드백 core
importers.py           Forms/응답/피드백 변환 계층
manage.py              관리·import CLI
templates/             모바일/관리자 화면
static/style.css       반응형 UI
fixtures/              Production이 아닌 sample 데이터
tests/test_app.py      회귀 테스트
railway.toml, Procfile Railway 실행 설정
```

## 로컬 실행

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export APP_SECRET='긴 무작위 문자열'
export ADMIN_PASSWORD='강한 관리자 비밀번호'
export DB_PATH='./quiz.db'
export ENABLE_TEST_IDENTITY='true'
export SEED_DEMO_DATA='true'
python app.py
```

브라우저에서 `/quiz?episode=R041`로 접근하면 테스트 구독자 선택 후 demo Quiz를 진행할 수 있습니다. 관리자 화면은 `/admin`입니다.

## Railway 배포

1. 이 폴더를 GitHub 저장소에 올려 Railway 서비스와 연결합니다.
2. Railway Volume을 생성하여 앱 컨테이너의 `/data`에 mount합니다.
3. 아래 환경변수를 설정합니다.
4. 최초 독립 테스트 배포에서는 `ENABLE_TEST_IDENTITY=true`, `SEED_DEMO_DATA=true`를 사용할 수 있습니다.
5. 실제 Magic Link 흐름을 사용할 때는 두 값을 `false`로 변경합니다.

### 환경변수

| 이름 | 필수 | 예시/설명 |
|---|---:|---|
| `APP_SECRET` | 예 | 세션 서명용 긴 무작위 값 |
| `ADMIN_PASSWORD` | 예 | 관리자 비밀번호 |
| `DB_PATH` | 예 | Railway에서는 `/data/quiz.db` |
| `ENABLE_TEST_IDENTITY` | 예 | 독립 테스트 시 `true`, 공개 전 `false` |
| `SEED_DEMO_DATA` | 예 | 최초 demo 데이터가 필요할 때만 `true` |
| `MIGRATION_HASH_SECRET` | 예 | 이메일 HMAC identity key. 등록 후 절대 임의 변경하지 않음 |
| `SESSION_COOKIE_SECURE` | 예 | Railway HTTPS에서는 `true` |
| `SUBSCRIBER_SESSION_DAYS` | 예 | 인증 후 같은 브라우저 유지 기간. 권장 `180` |
| `PUBLIC_BASE_URL` | 예 | Railway 공개 URL. 예: `https://...up.railway.app` |
| `BREVO_API_KEY` | 예 | Brevo Transactional Email API key |
| `MAGIC_LINK_SENDER_EMAIL` | 예 | Brevo에서 확인된 발신 이메일 |
| `MAGIC_LINK_SENDER_NAME` | 예 | 메일에 표시할 발신자 이름 |
| `MAGIC_LINK_TTL_MINUTES` | 예 | 일회용 링크 만료시간. 기본 `15` |
| `MAGIC_LINK_REQUEST_COOLDOWN_SECONDS` | 예 | 같은 구독자의 재요청 제한. 기본 `60` |
| `BREVO_TIMEOUT_SECONDS` | 선택 | Brevo API timeout. 기본 `10` |
| `PORT` | 자동 | Railway가 자동 제공 |

`SEED_DEMO_DATA=true`는 동일 데이터를 중복 생성하지 않습니다. 실제 운영 전에는 반드시 `false`로 바꾸십시오.

## DB schema 요약

- `subscribers`: 앱 내부 식별자, 표시명, 선택적 이메일 HMAC 해시
- `seasons`, `episodes`: 시즌과 회차·공개 상태
- `questions`, `choices`: 가변 문항, 선택지, 정답, 배점, 해설, 순서
- `quiz_attempts`, `attempt_answers`: 재응시를 포함한 모든 시도와 답
- `participation`: 혜택용 1회 참여. `(subscriber_id, episode_id)` UNIQUE
- `legacy_participation`: 실제 episode와 분리된 시즌별 과거 참여 횟수와 선택 메모
- `magic_link_tokens`: subscriber, token SHA-256 hash, 내부 redirect, 만료·사용 시각
- `feedback_questions`, `feedback_options`: 변경 가능한 피드백 문항 정의
- `feedback_submissions`, `feedback_answers`: 회원 또는 과거 익명 피드백

## 인증과 Quiz core의 분리

Quiz core는 `session['subscriber_id']`로 확인된 내부 subscriber ID만 받습니다. `/test-identity`와 실제 Magic Link 모두 인증 성공 뒤 `establish_subscriber_session()`을 사용하므로 Quiz·참여·피드백 core는 변경되지 않습니다.

## 테스트 모드에서 운영 준비 모드로 전환

Railway의 기존 Volume과 `DB_PATH=/data/quiz.db`는 그대로 유지합니다. 환경변수는 아래처럼 바꿉니다.

```text
ENABLE_TEST_IDENTITY=false
SEED_DEMO_DATA=false
SESSION_COOKIE_SECURE=true
SUBSCRIBER_SESSION_DAYS=180
```

`SEED_DEMO_DATA=true`인데 `ENABLE_TEST_IDENTITY=false`인 혼합 상태는 앱 시작 시 거부합니다. 두 값을 함께 변경해야 합니다. 테스트 신원 기능이 꺼지면 `/test-identity`는 404가 되고, 과거에 발급된 `test-alpha`/`test-beta` 브라우저 세션도 Quiz 접근 시 제거·거부됩니다.

환경변수를 끄는 것은 기존 DB 행을 삭제하지 않습니다. 다음 demo 데이터는 명시적인 삭제 승인 전까지 남습니다.

- `subscribers`: `test-alpha`, `test-beta` (`is_test=1`)
- `episodes`: `R041` demo 여부는 코드만으로 판정하지 말고 관리자에서 제목과 실제 용도를 확인
- 위 신원/회차에 연결된 `quiz_attempts`, `attempt_answers`, `participation`, `feedback_submissions`, `feedback_answers`

`R041`이 실제 운영 회차로 계속 사용될 수 있으므로 자동 삭제하지 않습니다. 삭제가 필요하면 Railway DB 백업 후 FK 연결 행의 범위를 먼저 조회하고 별도 승인된 정리 작업으로 수행합니다.

## 실제 구독자 Magic Link 인증

현재 Brevo `LANDING_URL`은 회차별 Notion 페이지를 가리키지만 동일 회차의 Quiz 링크는 공통이므로 그 링크만으로 개인을 식별하지 않습니다. 최소 변경 권장안은 **Notion → Quiz → 최초 1회 이메일 magic link → 장기 signed session**입니다.

1. Quiz에서 이메일을 입력합니다. 응답은 등록 여부와 무관하게 동일한 안내를 보여 계정 존재를 노출하지 않습니다.
2. 서버는 이메일 원문을 DB에 저장하지 않고, 고정된 비밀키의 HMAC 값으로 사전 등록된 `subscribers.email_hash`와 대조합니다.
3. 일치하면 Brevo Transactional Email API로 짧은 만료시간의 단일 사용 random token을 보냅니다. URL에는 이메일·이름·subscriber ID를 넣지 않고 token만 넣습니다. DB에는 token 원문이 아닌 SHA-256 hash만 저장합니다.
4. token 검증 후 `auth.establish_subscriber_session()`이 내부 `subscribers.id`만 signed session에 저장합니다. Quiz/참여 core는 변경하지 않습니다.
5. 같은 브라우저에서는 `SUBSCRIBER_SESSION_DAYS` 동안 재인증하지 않습니다. 다른 기기, 쿠키 삭제, 만료 뒤에는 이메일 인증을 다시 합니다.

DB의 실제 identity 기준은 내부 FK인 `subscribers.id`입니다. `public_id`는 이메일과 무관한 불투명 ID, `email_hash`는 인증 입력과 기존 구독자 명부를 매칭하는 값으로 사용합니다. 평문 PII는 URL·token 테이블·애플리케이션 로그에 남기지 않습니다.

관리자는 `/admin/subscribers`의 `새 구독자 등록` 버튼을 눌러 `/admin/subscribers/new`에서 이메일과 표시 이름으로 실제 subscriber를 등록합니다. 이메일 원문은 저장하지 않으며 `MIGRATION_HASH_SECRET`을 사용한 HMAC 값만 `email_hash`에 저장합니다. 이 secret을 subscriber 등록 뒤 변경하면 기존 이메일과 매칭할 수 없으므로 계속 같은 값을 유지해야 합니다.

Magic Link token은 기본 15분 뒤 만료되며 한 번 사용하면 다시 사용할 수 없습니다. 새 링크가 발급되면 해당 subscriber의 이전 미사용 링크는 무효화됩니다. 발송 실패 시 새 token도 즉시 무효화하고, raw token·이메일을 로그에 남기지 않습니다.

`ENABLE_TEST_IDENTITY`는 위 Production adapter와 별도입니다. 운영에서는 계속 `false`로 두고, 실제 인증 성공 시에만 공통 세션 생성 함수를 호출합니다.

## 시즌1 과거 참여 수동 반영

관리자 구독자 목록에서 subscriber를 선택하고 `시즌1 과거 참여 횟수`와 선택 메모를 저장합니다. 이 값은 `legacy_participation`에 `season_code='S1'`로 저장되며 `participation`, `quiz_attempts`, 점수 또는 가짜 episode를 만들지 않습니다.

- 누적 참여: 실제 `participation` 개수 + 모든 legacy count
- 현재 시즌 참여: 실제 `participation`만 집계
- 같은 회차 재응시: 기존 UNIQUE 제약대로 참여 1회만 유지

## Brevo 실제 발송 확인 순서

1. Brevo에서 Transactional Email을 보낼 발신 이메일/도메인을 확인합니다.
2. Brevo API key를 새로 만들고 Railway의 `BREVO_API_KEY`에 저장합니다.
3. 확인된 주소와 표시 이름을 `MAGIC_LINK_SENDER_EMAIL`, `MAGIC_LINK_SENDER_NAME`에 저장합니다.
4. `PUBLIC_BASE_URL`을 현재 Railway 공개 URL로 설정합니다.
5. 관리자에서 본인이 받을 수 있는 테스트 이메일을 실제 subscriber로 등록합니다.
6. `ENABLE_TEST_IDENTITY=false`, `SEED_DEMO_DATA=false`인 상태에서 비공개 테스트 회차 URL에 접속해 메일 수신, 원래 회차 복귀, 재사용 차단을 확인합니다.

이 과정은 기존 Brevo Automation이나 contact attribute를 변경하지 않습니다. 앱이 Brevo의 `/v3/smtp/email` Transactional Email endpoint를 직접 호출합니다.

## sample import 검증

실제 Google 계정이나 Production 자료를 사용하지 않습니다.

```bash
python manage.py import-form-json fixtures/sample_google_form.json --season S1
python manage.py migrate-history fixtures/sample_historical_responses.csv --episode R041 --score-policy last
python manage.py migrate-feedback fixtures/sample_historical_feedback.csv --episode R041
```

과거 응답 migration은 이메일 원문을 저장하지 않고 HMAC 해시로 subscriber를 매칭합니다. 같은 이메일·회차의 여러 제출은 하나의 participation으로 합칩니다. 점수 선택 정책은 `first`, `last`, `max` 중 실행 시 선택할 수 있습니다.

## 테스트

```bash
python -m unittest discover -s tests -v
```

## Production 연결 전 남은 작업

- 실제 Forms 원본 계정의 read-only export/API 연결
- 실제 응답 Sheet 열 이름 매핑 및 dry-run 보고서 확인
- 기존 참여 소급 반영 범위와 점수 정책 확정
- Notion의 Google Form URL을 새 앱 회차 URL로 교체
- 테스트 identity와 demo seed 비활성화
- 관리자 비밀번호·세션 secret·migration secret 설정
- Railway Volume mount 및 백업 정책 설정
- 시즌2 구독 시작 시 관리자가 실제 subscriber를 등록하는 운영 절차 확정

## 실제 migration 실행 시 필요한 자료

- Form ID 목록 또는 Forms API JSON export
- 각 회차 코드와 Form ID의 대응표
- 응답 Sheet CSV와 회차 코드
- 실제 이메일 열·타임스탬프 열·점수 열 이름
- 피드백 Sheet CSV와 회차 코드
- 중복 제출 시 점수 정책(`first`, `last`, `max`)

## 알려진 제한사항

- v0.1은 객관식 단일 정답 문항만 지원합니다.
- 질문 이미지 자동 보관은 아직 구현하지 않았습니다.
- 관리자 계정은 환경변수 비밀번호 1개 방식입니다.
- 이메일 원문을 저장하지 않으므로 현재 관리자 화면에서 등록 이메일을 다시 조회하거나 변경하는 기능은 없습니다.
- 완료된 응시가 있는 문항의 수정·삭제 이력 보존 기능은 없습니다. 공개 후 문항 수정 전에는 별도 백업이 필요합니다.
- SQLite는 현재 1인 운영 규모에 적합하지만 동시 쓰기가 크게 늘면 PostgreSQL 전환을 검토해야 합니다.
