# 시즌1 리마스터 오디오레터 — 검증 후 import 계획

이 문서는 **향후 이전 설계**입니다. Production의 42회 데이터·Bucket·Notion 원본은 이 코드 배포만으로 변경되지 않습니다. Portal 기준 회차는 시즌을 넘어 증가하는 `sequence`이며, 시즌1은 1–42입니다. 신규 유료 구독은 7회부터 시작하고 기존 entitlement 정책은 그대로입니다.

## 확인 범위와 미확인 부분

- Portal 코드: `audioletter_episodes`와 분리된 `audioletter_blocks`, 관리자 입력, 기존 Quiz의 독립된 `episodes`, Private Bucket 프록시와 각 요청의 entitlement 검사.
- Notion 시즌1 원본의 8회차 페이지를 읽어 중첩 토글·동기화 블록·정보 카드 및 Google Drive 임베드를 확인했습니다. 일부 이미지도 외부 파일 형태입니다. 이 한 페이지의 구조가 다른 41회에도 동일하다고 가정하지 않습니다.
- Google Drive 임베드가 실제 MP3 원본을 일괄 다운로드할 수 있는 권한을 제공하는지, 모든 42회가 같은 자산 저장 방식을 쓰는지, 토글 안 텍스트·그림·추가 오디오·링크의 정확한 대응은 미확인입니다.
- 현재 Portal의 `info` 블록은 안전한 **일반 텍스트**입니다. 원본의 이미지·임베드·서식은 자동 이전 대상이 아닙니다. 중요한 자료가 있다면 지원할 형식과 권한을 별도로 결정한 뒤 확장해야 하며, 조용히 삭제하거나 `transcript`에 합치면 안 됩니다.
- 공통 이용 안내는 별도 템플릿에 한 번만 둡니다. 현재 템플릿 문구는 요청된 범주를 반영한 초안이며 원본 문구를 그대로 복사한 것인지 확인되지 않았습니다. 공개 전 운영자가 문구를 검토합니다.

## 1. 원본 확인 및 반복 가능한 manifest

1. 운영자가 **실제 배포 기준** Notion 시즌1의 42개 페이지 ID 목록과 읽기 전용 Notion API 접근권 또는 전체 HTML/Markdown export를 제공한다. 복제된 연구용 페이지와 운영용 페이지는 혼합하지 않는다.
2. 각 페이지를 재귀적으로 읽어 자식 블록·synced block 참조·토글 순서와 원본 블록 ID를 수집한다. 조회 범위가 누락되면 import를 중지한다. 임베드·파일의 URL은 영구 ID가 아니라 임시 획득 수단이다.
3. `sequence`, `season`, `season_episode`, `title`, 원본 페이지 ID, 순서 있는 `audio`/`info` 블록, 오디오 원본 자산 ID, 원본 텍스트, Quiz 코드 후보, 처리 불가 블록을 담은 **검토용 manifest JSON**을 생성한다. Notion 공통 footer와 반복 안내는 manifest에서 제외한다.
4. 검토용 출력에서 1–42의 누락·중복, 토글 경계, 메인/추가 오디오의 순서, 텍스트 길이와 줄바꿈, 링크·이미지 누락, 무료 1–6회 표시 정책, Quiz와 Feedback 대응을 회차별로 검증한다. 미확인 항목이 있는 회차는 import에서 보류한다.

## 2. MP3 일괄 이전

1. 각 audio 블록의 원본이 Notion 업로드 파일인지, Google Drive 임베드인지, 다른 서비스의 파일인지 구별한다. Drive 임베드는 Drive 파일 ID와 **읽기 권한** 확인이 필요하다. 프리뷰 링크만으로 원본 MP3를 무단 추출하지 않는다.
2. 허가된 접근 경로로 MP3 바이트를 내려받아 MIME/파일 서명, 파일 크기, 가능하면 재생·seek, SHA-256을 점검한다. 만료되는 원본 URL이나 인증값을 manifest·Portal DB에 저장하지 않는다.
3. 운영자가 확인한 대응에 따라 비공개 Railway Bucket의 결정적 키(예: `audioletters/season1/008/main-<hash-prefix>.mp3`, `.../extra-<hash-prefix>.mp3`)로 업로드한다. Bucket credential은 Railway 변수 또는 실행 시 안전한 환경변수만 사용한다. 업로드한 객체를 다시 확인하고 manifest에 **object key와 checksum만** 저장한다.
4. 업로드 실패·원본 누락·checksum 불일치가 있으면 그 회차의 DB 반영을 중단한다. 재실행 시 동일한 원본과 checksum은 중복 업로드하지 않으며 다른 원본으로 같은 키를 덮어쓰지 않는다. DB 반영 전 생긴 미참조 객체는 목록으로 보고한다.

## 3. DB 반영과 배포 순서

1. 미리보기 검토와 백업을 마친 뒤 별도 작업 창에서만 진행한다. SQLite Production 볼륨에 대한 import의 실행 방식과 동시 쓰기 방지 방법을 먼저 확인한다.
2. DB 반영은 episode별 transaction으로 처리한다. `sequence`와 `(season, season_episode)` 중복, `sort_order` 중복을 검사한다. 기존 운영 중인 7회차는 **덮어쓰지 않는다**. 구조 이전을 원한다면 수동 검토한 manifest와 현재 DB 내용을 diff로 보여주고 별도 승인 후 이전한다.
3. 최초 반영은 `is_published=0`으로 검증하고, 키·순서·텍스트·권한별 렌더링·Bucket Range 재생 검증을 마친 회차만 운영자가 공개한다. 기존 구독자 권한값과 발송 흐름은 import 과정에서 변경하지 않는다.
4. 실패 후 재실행은 확정된 sequence/page ID 및 manifest checksum과 DB 상태를 비교하여 이미 동일한 회차는 건너뛰고, 다른 회차나 변경된 블록은 자동 덮어쓰기 대신 diff 검토로 돌린다. 작업별 성공·실패·보류 목록을 남긴다. Notion 원본은 읽기만 한다.

## Quiz·Feedback

`audioletter_episodes.quiz_episode_code`는 기존 Quiz `episodes.code`의 **명시적 연결**이다. 숫자가 비슷하다고 자동 연결하지 않는다. Quiz가 공개되어 있고 문항이 있을 때 상세 페이지의 이해테스트 링크가 보인다. 기존 참여가 있을 때만 기존 피드백 경로로 연결한다. 실제 42회↔Quiz 코드 대응표는 운영자 확인 후 manifest에 넣는다.

## importer 코드 구현 전에 필요한 자료

- 운영용 Notion 시즌1 42개 원본 페이지 목록·ID와 읽기 가능한 export/API 권한.
- 적어도 7·8회 및 다른 구성을 가진 회차의 원본 블록 트리, MP3 원본의 위치와 파일 읽기 권한. Google Drive 자산이면 대응하는 파일 ID/소유자 접근 방식.
- 그림·표·서식·외부 링크를 Portal에서 어떻게 보일지에 관한 정책. 현재 `info`는 텍스트만 지원하므로 8회차의 이미지가 필수 콘텐츠인지 확인.
- 1–6회 무료 콘텐츠의 Portal 노출 정책(현재 상세는 유료 entitlement를 요구함), 기존 Quiz 코드 대응표, 원본 footer 문구 검수 결과.
- 읽기 전용 dry-run과 DB 백업을 시행할 수 있는 별도 환경. 작업 전후 episode·block 수 및 화면·오디오 검증 기준.

위 자료가 확보되기 전에는 Notion API 파서와 MP3 일괄 업로드 importer를 추측으로 구현하지 않습니다.
