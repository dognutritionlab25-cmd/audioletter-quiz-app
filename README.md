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
- 관리자 구독자 등록·활성 상태 관리와 시즌1 과거 참여 횟수 수동 반영
- 관리자 Preview/검증 기반 퀴즈 CSV 일괄 Import
- 로그인 구독자용 자료실과 관리자 자료 CRUD(외부 링크, 공개/비공개)
- Google Sheet/Make에서 전달받은 유료 상태와 현재 유료 구독자 전용 게시판
- 구독자 게시글·댓글·좋아요와 관리자 숨김/삭제·새 글 이메일 알림
- 공개 구독 안내·이용약관·개인정보처리방침과 관리자 결제 공개 설정
- PayApp 결제 후 Portal 유료 구독 등록 신청과 Make용 안전한 조회·완료 API

## 파일 구조

```text
app.py                 Flask routes, 화면, 관리자 기능
auth.py                교체 가능한 신원/관리자 경계
db.py                  SQLite schema와 transaction
magic_links.py         일회용 token과 Brevo Transactional Email 발송
presenters.py           관리자 표시용 날짜·피드백 변환
services.py            Quiz·참여·피드백 core
importers.py           Forms/응답/피드백 변환 계층
quiz_csv_import.py     관리자 퀴즈 CSV 검증·중복 판정·Import
resources.py           자료실 구독자 화면과 관리자 CRUD Blueprint
community.py           유료 구독자 게시판과 관리자 관리 Blueprint
public_pages.py        공개 안내·약관·개인정보·PayApp 이동·관리자 설정 Blueprint
subscriptions.py       구독 등록 신청·dog profile·Make 연동 API Blueprint
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
| `SUBSCRIBER_SYNC_API_KEY` | Make 연동 시 예 | subscriber sync API 전용 긴 무작위 Bearer secret. 다른 secret과 재사용하지 않음 |
| `SUBSCRIPTION_REGISTRATION_API_KEY` | 구독 등록 연동 시 예 | Make가 등록 신청을 조회·완료 처리할 때 쓰는 별도 Bearer secret |
| `SUBSCRIPTION_REGISTRATION_ENCRYPTION_KEY` | 구독 등록 사용 시 예 | Make 전달 전 임시 개인정보 payload 전용 Fernet 키. 다른 secret과 재사용하지 않으며 설정 후 임의 변경하지 않음 |
| `SESSION_COOKIE_SECURE` | 예 | Railway HTTPS에서는 `true` |
| `SUBSCRIBER_SESSION_DAYS` | 예 | 인증 후 같은 브라우저 유지 기간. 권장 `180` |
| `PUBLIC_BASE_URL` | 예 | 사용자에게 발송할 공식 Portal URL. Production 값: `https://portal.dognutritionlab.com` |
| `BREVO_API_KEY` | 예 | Brevo Transactional Email API key |
| `MAGIC_LINK_SENDER_EMAIL` | 예 | Brevo에서 확인된 발신 이메일 |
| `MAGIC_LINK_SENDER_NAME` | 예 | 메일에 표시할 발신자 이름 |
| `MAGIC_LINK_TTL_MINUTES` | 예 | 일회용 링크 만료시간. 기본 `15` |
| `MAGIC_LINK_REQUEST_COOLDOWN_SECONDS` | 예 | 같은 구독자의 재요청 제한. 기본 `60` |
| `BREVO_TIMEOUT_SECONDS` | 선택 | Brevo API timeout. 기본 `10` |
| `COMMUNITY_ADMIN_NOTIFICATION_EMAIL` | 게시판 사용 시 예 | 새 게시글 알림을 받을 관리자 이메일 |
| `PORT` | 자동 | Railway가 자동 제공 |

`SEED_DEMO_DATA=true`는 동일 데이터를 중복 생성하지 않습니다. 실제 운영 전에는 반드시 `false`로 바꾸십시오.

## DB schema 요약

- `subscribers`: 앱 내부 식별자, 표시명, 이메일 HMAC 해시, 활성 상태
- `seasons`, `episodes`: 시즌과 회차·공개 상태
- `questions`, `choices`: 가변 문항, 선택지, 정답, 배점, 해설, 순서
- `quiz_attempts`, `attempt_answers`: 재응시를 포함한 모든 시도와 답
- `participation`: 혜택용 1회 참여. `(subscriber_id, episode_id)` UNIQUE
- `legacy_participation`: 실제 episode와 분리된 시즌별 과거 참여 횟수와 선택 메모
- `magic_link_tokens`: subscriber, token SHA-256 hash, 내부 redirect, 만료·사용 시각
- `feedback_questions`, `feedback_options`: 변경 가능한 피드백 문항 정의
- `feedback_submissions`, `feedback_answers`: 회원 또는 과거 익명 피드백
- `resources`: 자료 제목·본문·카테고리·외부 링크·공개 상태와 작성/수정 시각
- `subscribers.is_paid_subscriber`: Google Sheet/Make가 판단한 현재 유료 구독 상태. `is_active`와 별도
- `subscribers.accessible_through`: 해당 구독자에게 시스템상 공개된 최신 오디오레터 내부 연속 회차. 기존 구독자는 `NULL`로 유지하고 실제 범위 동기화 시에만 설정
- `audioletter_episodes`: Quiz `episodes`와 분리된 오디오레터 회차 메타데이터·entitlement 기준. 기존 단일 오디오/스크립트 열은 7회차 등 이전 데이터 호환을 위해 유지
- `audioletter_blocks`: 회차별 순서 있는 `audio`/`info` 콘텐츠. 오디오 블록의 비공개 object key·스크립트, 정보 블록의 본문을 각각 저장
- `community_posts`, `community_comments`, `community_likes`: 게시글·댓글·게시글별 subscriber 1회 좋아요
- `portal_settings`: 결제 안내 공개 여부와 이용약관·개인정보처리방침 시행일(단일 설정 행)
- `subscription_registrations`: 결제 후 입력한 유료 구독 등록 신청의 메타데이터·이메일 HMAC·처리 상태
- `subscription_registration_payloads`: Make 처리 전까지만 보관하는 인증 암호화 개인정보 payload. 완료 처리와 같은 transaction에서 삭제
- `dog_profiles`: 신청별 반려견 정보. subscriber sync 전에는 소유자가 비어 있고, sync 후 내부 subscriber ID에 연결

`resources` 테이블은 앱 시작 시 `CREATE TABLE IF NOT EXISTS`로 추가됩니다. 기존 테이블이나 행을 변경·삭제하지 않는 additive schema 초기화입니다. 관리자는 `/admin/resources`, 로그인한 구독자는 `/resources`를 사용합니다.

게시판 테이블도 같은 additive 초기화 방식으로 추가됩니다. 기존 subscriber에는 `is_paid_subscriber=0`이 적용되며 앱이 실제 유료 여부를 추측하지 않습니다. Google Sheet가 계산한 결과를 Make가 sync API의 `is_paid_subscriber` boolean으로 보내야 합니다. 필드를 보내지 않으면 기존 paid 값은 유지됩니다.

## 공개 구독 안내와 법적 고지

- `/subscribe`: 서비스·플랜·환불 핵심 안내. `portal_settings.subscription_page_enabled=0`이 기본값이며, 이때 일반 방문자는 결제할 수 없습니다.
- `/terms`, `/privacy`: 로그인 없이 열람할 수 있습니다. 시행일은 관리자 설정을 사용하며 비어 있으면 `확정 전`으로 표시합니다.
- `/admin/portal-settings`: 관리자가 결제 페이지 공개 여부와 두 시행일을 저장합니다. 공개 OFF 상태에서도 로그인한 관리자는 결제 흐름을 미리 볼 수 있습니다.
- PayApp 주소는 `public_pages.py`의 `PAYMENT_PLANS` 한 곳에서 관리합니다. 브라우저에는 내부 POST route만 제공되며, 공개 여부·CSRF·두 필수 동의를 서버에서 확인한 뒤 외부 결제 페이지로 이동합니다.

배포 직후 기본 상태는 결제 페이지 **OFF**이므로, 약관·개인정보처리방침의 시행일과 화면 내용을 확인한 뒤 관리자가 명시적으로 공개해야 합니다.

## PayApp 결제 후 구독 등록

`/subscription/register`는 기존 Google Form을 대체하는 공개 신청 화면입니다. 결제 페이지가 공개된 동안만 일반 방문자가 접근할 수 있고, 공개 OFF 상태에서는 관리자만 미리 볼 수 있습니다. 입력 필드는 구독 개월수, 보호자 이름, 반려견 이름, 이메일, 연락처, 선택적인 반려견 생일·견종·결제자 이름·관심 내용, 신규/재구독, 개인정보 동의입니다.

신청 제출은 `subscription_registrations.status='pending'`과 `dog_profiles`만 만듭니다. subscriber를 만들거나 `is_paid_subscriber`를 변경하지 않습니다. 같은 이메일의 미처리 신청을 다시 제출하면 기존 pending 신청과 dog profile을 갱신하므로 중복 생성되지 않습니다. 완료된 신청 뒤의 재구독 신청은 별도 기록으로 생성할 수 있습니다.

Make 전달에 필요한 이메일·보호자 이름·연락처·결제자 이름·관심 내용은 `subscription_registrations`에 원문으로 저장하지 않습니다. 별도 `subscription_registration_payloads`에 authenticated encryption(Fernet)으로 임시 보관하고, pending API에서만 서버가 복호화합니다. Make의 현재 JSON field mapping은 그대로 유지됩니다. `/complete`가 성공하면 encrypted payload는 즉시 삭제되며 신청 메타데이터, 이메일 HMAC과 subscriber에 연결된 `dog_profiles`는 유지됩니다.

관리자는 `/admin/subscription-registrations`에서 신청 목록을 읽기 전용으로 확인할 수 있습니다. pending 신청은 해당 신청의 encrypted payload를 서버에서 복호화해 제출 원문을 표시합니다. completed 신청은 최소보관 정책에 따라 원문 payload가 이미 삭제되므로 신청 메타데이터·처리상태·반려견 프로필만 표시하고, 이메일·연락처 등을 subscriber나 외부 시스템에서 재구성하지 않습니다.

전용 키는 아래처럼 한 번 생성하여 Railway secret variable로 설정합니다. 기존 `APP_SECRET`, `MIGRATION_HASH_SECRET` 또는 API key를 재사용하면 안 됩니다.

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Make는 별도 `SUBSCRIPTION_REGISTRATION_API_KEY`를 사용합니다. API key는 `APP_SECRET`, `MIGRATION_HASH_SECRET`, `SUBSCRIBER_SYNC_API_KEY`와 재사용하지 않습니다.

```http
GET /api/subscription-registrations/pending?limit=20
Authorization: Bearer <SUBSCRIPTION_REGISTRATION_API_KEY>
```

응답의 `registrations[]`에는 기존 Form 응답 Sheet로 매핑할 등록정보와 불투명한 `registration_id`가 포함됩니다. 이메일과 연락처가 포함되므로 응답에는 `Cache-Control: no-store`가 적용되며 endpoint는 올바른 Bearer key 없이는 열리지 않습니다.

Make의 실제 결제 검증, 구독자명단 추가, 기존 `POST /api/subscribers/sync` 성공과 최종 알림톡까지 모두 끝난 뒤 다음 endpoint를 호출합니다.

```http
POST /api/subscription-registrations/<registration_id>/complete
Authorization: Bearer <SUBSCRIPTION_REGISTRATION_API_KEY>
```

완료 endpoint는 subscriber sync로 신청과 subscriber가 먼저 연결되지 않았다면 `409`를 반환합니다. 반복 호출은 `unchanged`로 안전하게 성공합니다. Portal 신청은 결제 인증이 아니며, 유료 권한은 기존 sync API에 Google Sheet/Make가 명시적으로 `"is_paid_subscriber": true`를 보낸 경우에만 생깁니다.

### Make의 유료 상태 전달

`POST /api/subscribers/sync`의 기존 Bearer 인증과 이메일 identity 규칙은 그대로입니다. 기존 payload는 계속 동작하며, Google Sheet에서 판단한 구독 결과를 반영할 때만 아래 boolean 필드를 추가합니다.

```json
{
  "email": "member@example.com",
  "display_name": "구독자 이름",
  "is_paid_subscriber": true
}
```

- 무료 신규 또는 유료 구독 종료: `"is_paid_subscriber": false`
- 유료 신규 또는 재구독: `"is_paid_subscriber": true`
- 필드 생략: 기존 subscriber의 유료 상태를 변경하지 않음. 신규 subscriber는 기본 `false`

유료 구독 종료는 `active`를 `false`로 만드는 작업이 아닙니다. `is_active`는 계정 사용 가능 여부로 계속 분리하여 유지합니다.

오디오레터 접근 범위는 선택 입력 `"accessible_through": 6`처럼 같은 sync API에 전달합니다. 신규 유료 등록은 Sheet의 초기 L=6을 전달할 수 있고, 향후 7회차 전용 Make의 Sheet #5 및 Master의 Sheet #20 업데이트가 성공한 뒤 해당 회차 값을 전달할 예정입니다. 이 필드가 없으면 기존 범위를 그대로 두고, 전달되면 0 이상의 정수만 받아 기존 값보다 큰 경우에만 저장합니다. `is_paid_subscriber=false` 또는 재구독 sync도 범위를 지우지 않습니다. 현재 오디오레터 사용자 접근 경로는 아직 구현되지 않았습니다.

### 오디오레터 콘텐츠 기반

`audioletter_episodes`는 앱 시작 시 additive schema 초기화로 생성됩니다. Quiz용 `episodes` 및 Quiz 시즌/참여 관계와 연결되지 않습니다. DB의 `id`는 영구 식별자이고 `sequence`는 시즌을 넘어 증가하는 접근권한 비교값입니다. `season`과 `season_episode`는 고객 표시용입니다. `sequence`와 `(season, season_episode)`는 각각 중복을 허용하지 않습니다.

관리자는 `/admin/audioletters`에서 회차를 확인하고 새로 등록하거나 수정할 수 있습니다. 공개 여부도 편집 화면에서 변경합니다. `audio_storage_key`에는 Railway Private Bucket에 실제 존재하는 MP3의 **object key**만 넣습니다(예: `audioletters/season1/007.mp3`). URL이나 비밀키를 넣지 않습니다. 관리자 업로드 기능과 자동 데이터 삽입 기능은 없습니다. `transcript`는 HTML로 변환하지 않고 SQLite `TEXT`에 줄바꿈을 보존하여 저장합니다.

사용자는 `/audioletters`에서 서버가 권한에 맞게 조회한 회차만 보고, `/audioletters/<id>`에서 오디오와 접힌 전체 스크립트를 볼 수 있습니다. 직접 오디오 주소 `/audioletters/<id>/audio`를 요청해도 `audioletters.accessible_audioletter_episode()`가 기존 `services.can_access_paid_audioletter()`를 재사용하여 `is_active`, `is_paid_subscriber`, `is_published`, `sequence <= accessible_through`를 검사합니다. 서버는 허용된 요청만 Private Bucket에서 스트리밍하며 단일 HTTP Range 요청을 전달합니다. URL 발급 없이 매 요청마다 권한을 확인하므로 구독 종료 후 **새 요청**은 차단됩니다. 이미 시작한 스트림의 중간 종료나 재생된 파일의 복제 방지는 보장하지 않습니다. 오디오 전송량은 Portal 서비스 트래픽에 포함됩니다. 현재 Quiz의 `episodes`는 이 조건을 사용하지 않습니다.

회차 안의 오디오와 정보는 `audioletter_blocks`로 순서를 정합니다. 기존 단일 오디오 회차는 블록이 없을 때 기존 `audio_storage_key`/`transcript`로 계속 표시됩니다. 해당 회차에 처음 블록을 추가할 때 기존 오디오·스크립트를 첫 블록으로 한 번 복사하며, 이후에는 블록이 콘텐츠의 기준입니다. 기존 `/audioletters/<id>/audio` URL은 첫 오디오에 계속 연결됩니다. 각 추가 오디오는 `/audioletters/<id>/blocks/<block_id>/audio`에서 동일한 권한 확인과 프록시·Range 처리 후 제공됩니다. 관리자는 `/admin/audioletters/<id>/edit`에서 블록을 추가·수정·삭제하고 표시 순서를 지정합니다. 상세 페이지의 공통 이용 안내는 `templates/audioletter_disclaimer.html` 한 곳에서 관리합니다.

기존 Quiz의 `episodes` 테이블은 별개입니다. 연결이 검증된 회차에만 관리자 화면에서 기존 Quiz 코드를 선택 입력하며, Quiz가 공개되어 있고 문항이 있을 때만 링크가 보입니다. 피드백 링크는 기존 Quiz 참여 기록이 확인된 경우에만 표시됩니다. Notion 시즌1 42회와 MP3 이전은 아직 수행하지 않았습니다. 파일 출처·권한·중첩 구성 검증 및 import 단계는 `docs/audioletter_migration_plan.md`를 참조하세요.

오디오 플레이어의 `controlsList="nodownload"`와 우클릭 억제는 일반 사용자에게 다운로드 메뉴를 보이지 않게 하는 UX 설정입니다. 파일 저장을 기술적으로 완전히 막는 보안 기능은 아닙니다.

Railway Portal 서비스 Variables에서 아래 값을 같은 환경의 Private Bucket Credentials의 **Variable Reference**로 설정합니다. 실제 인증값을 코드·문서·로그에 기록하지 마세요.

| Portal Variable | Railway Bucket Reference |
| --- | --- |
| `AUDIOLETTER_BUCKET_NAME` | `BUCKET` (S3 API용 이름) |
| `AUDIOLETTER_BUCKET_ENDPOINT` | `ENDPOINT` |
| `AUDIOLETTER_BUCKET_REGION` | `REGION` |
| `AUDIOLETTER_BUCKET_ACCESS_KEY_ID` | `ACCESS_KEY_ID` |
| `AUDIOLETTER_BUCKET_SECRET_ACCESS_KEY` | `SECRET_ACCESS_KEY` |

`AUDIOLETTER_BUCKET_ADDRESSING_STYLE`은 선택사항이며 기본값은 `auto`입니다. 이전 방식의 Bucket에서 Railway Credentials에 path-style이라고 명시되어 있다면 `path`로 설정합니다. 테스트 MP3를 Bucket에 `audioletters/season1/007.mp3`처럼 업로드한 후 `/admin/audioletters/new`의 **오디오 저장 키**에 같은 object key를 입력하고 공개 상태를 선택합니다. 해당 subscriber가 유료·활성이고 `accessible_through >= 7`이어야 합니다. `AUDIOLETTER_BUCKET_*` 설정이 없으면 오디오 제공만 503을 반환하며, 기존 Portal의 다른 기능은 그대로 사용할 수 있습니다.

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

관리자는 `/admin/subscribers`의 `새 구독자 등록` 버튼을 눌러 `/admin/subscribers/new`에서 이메일, 표시 이름, 활성 상태로 실제 subscriber를 등록합니다. 이메일 원문은 저장하지 않으며 `MIGRATION_HASH_SECRET`을 사용한 HMAC 값만 `email_hash`에 저장합니다. 이 secret을 subscriber 등록 뒤 변경하면 기존 이메일과 매칭할 수 없으므로 계속 같은 값을 유지해야 합니다.

### Make subscriber sync API

Make는 `POST /api/subscribers/sync`를 호출해 subscriber를 안전하게 upsert할 수 있습니다. `Authorization: Bearer <SUBSCRIBER_SYNC_API_KEY>`와 `Content-Type: application/json`이 필요합니다.

```json
{
  "email": "member@example.com",
  "display_name": "보호자 이름",
  "active": true
}
```

`email`만 필수입니다. 이메일은 관리자 등록 및 Magic Link와 동일하게 정규화하고 `MIGRATION_HASH_SECRET` HMAC으로 대조하며 원문을 저장하지 않습니다. 기존 subscriber이면 내부 ID와 참여 기록을 유지합니다. 기존 표시 이름은 덮어쓰지 않고 비어 있을 때만 채우며, `active`는 요청에 명시된 경우에만 변경합니다. 같은 요청을 반복해도 새 subscriber가 추가되지 않습니다.

구독자 상세 화면에서 활성 상태를 변경할 수 있습니다. 비활성 subscriber는 새 Magic Link를 받을 수 없고, 이전에 발급된 미사용 링크와 이미 로그인된 장기 세션도 Quiz 접근에 사용할 수 없습니다. `is_active`는 기존 DB에 `DEFAULT 1`로 추가되는 additive migration이므로 기존 subscriber ID와 참여 데이터는 바뀌지 않습니다.

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
4. `PUBLIC_BASE_URL=https://portal.dognutritionlab.com`으로 설정합니다. Magic Link는 이 값을 기준으로 생성되므로 Railway generated domain을 넣지 않습니다.
5. 관리자에서 본인이 받을 수 있는 테스트 이메일을 실제 subscriber로 등록합니다.
6. `ENABLE_TEST_IDENTITY=false`, `SEED_DEMO_DATA=false`인 상태에서 비공개 테스트 회차 URL에 접속해 메일 수신, 원래 회차 복귀, 재사용 차단을 확인합니다.

이 과정은 기존 Brevo Automation이나 contact attribute를 변경하지 않습니다. 앱이 Brevo의 `/v3/smtp/email` Transactional Email endpoint를 직접 호출합니다.

## sample import 검증

### 관리자 퀴즈 CSV Import

관리자 `/admin`에서 `퀴즈 CSV 가져오기`를 선택하고 UTF-8 CSV를 업로드합니다. 필수 열은 `R코드`, `회차`, `Form 제목`, `문항`, `질문`, `선택지(JSON)`, `정답`, `정답 해설`, `배점`, `Form ID`입니다.

1. Preview에서 신규/기존 Episode, 신규/기존 동일 Question, 충돌 문항, 오류 행을 확인합니다.
2. 오류 행이 있으면 Import 버튼이 제공되지 않습니다.
3. 기존 Episode는 제목·공개 상태·연결 데이터를 변경하지 않고 그대로 재사용합니다.
4. 같은 `episode + display_order`의 문항이 완전히 같으면 건너뛰고, 내용이 다르면 충돌로 표시한 뒤 건너뜁니다.
5. 새 Episode는 Season 1에 비공개 상태로 생성되며, 관리자가 내용을 확인한 뒤 별도로 공개합니다.

Preview 확인값은 30분 동안 유효한 서명 payload로 전달됩니다. Import는 Episode·Question·Choice만 추가하며 기존 participation, feedback, subscriber, 인증 데이터는 수정하거나 삭제하지 않습니다. DB schema 변경은 없습니다.

### 기존 sample/CLI import

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
