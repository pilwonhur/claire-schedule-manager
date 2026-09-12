# Changelog

형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/)를 따르고 버전은 SemVer다.
데이터 형식 버전(`SCHEMA_VERSION`)은 따로 관리한다.

## [0.4.1] — 2026-09-12

Discord에서 번호(`CLR-`, `Q-`)를 드래그·타이핑하지 않고 쓰게 하는 응답 규칙. 코드 변경 없음.

### 추가
- SKILL §5 **번호 복사 블록**: 등록 결과·새 질문·후보 되묻기·중복 안내·승인 요청·`show`/`find` 결과에는 본문 끝에 번호를 한 줄 코드 블록(`` ```CLR-0031``` ``)으로 하나씩 붙인다.
  Discord가 코드 블록마다 붙여 주는 "복사" 버튼으로 클릭 한 번에 클립보드에 들어간다. 한 메시지 5개까지. `CLR-0031 번호`로 개별 요청 가능.
- 브리핑 꼬리 "바로 답할 번호"(`references/briefing-format.md`): 확인 필요 `Q-` → 승인 필요 `CLR-` → 진행 확인 `CLR-` 순, 5개까지, 마지막 메시지에.
- SKILL §4: Claire 메시지에 **답장(reply)** 하며 번호 없이 지시하면 그 메시지의 `CLR-` 번호(1건일 때)로 처리. 2건 이상이면 되묻는다.
- `docs/manual.md` §3, `references/usage-guide.md` §0 에 사용법 추가.

### 배경
- Discord 봇은 사용자 클립보드에 직접 쓸 수 없고, OpenClaw 버튼 컴포넌트는 클릭이 Claire에게 메시지로 되돌아오는 방식이라 왕복이 생긴다. 코드 블록 복사 버튼은 클라이언트 자체 기능이라 즉시·무왕복이다.

## [0.4.0] — 2026-09-11

PRD v1.3 §13 **4단계: 외부 반영·운영 안정성**.

### 추가
- `scripts/claire_apply`: outbox 소비자. `obsidian.assign_id`(🆔 부여, D12)·`obsidian.check_task`([x]+✅)·`obsidian.add_task`(Daily `## Tasks`에 규약 형식 줄, 템플릿으로 Daily 생성)·
  `calendar.create`(Prof. Hur, `extendedProperties.private.claire_ref`, popup 알림)·`calendar.update`(patch 후 항목 미러링)·`discord.*`(to_deliver 로 반환).
  지수 백오프(1·5·15·60·240분) 최대 5회, 볼트 파일 변경 시 미루기(TC19), 쓰기 토큰 없으면 시도 소모 없이 건너뜀. `--list/--confirm/--cancel/--dry-run`.
- `claire_ops.plan_external`: D5 정책 — 지시(`등록:`)·답변 확정 항목은 자동, 메일·이미지 발견 항목의 Calendar 등록·Daily 줄은 승인(`requires_confirm=1`).
  교수님의 시각 변경 지시는 Calendar 원본을 거치도록 `calendar.update`로 미룬다(Claire 가 직접 바꾸면 거부).
- `claire_sync`: Calendar 충돌 감지 — 지시가 대기 중인데 Calendar 도 바뀌면 덮어쓰지 않고 `link.conflict` + 질문(§6.2).
- `claire_search review`: `outbox`(done_since_last / awaiting_confirm / pending / failed) → 브리핑 "반영 상태".
- `claire_run end --backup`, `connectors/google_api.py` 쓰기 호출(`calendar_event_insert/patch`, 쓰기 토큰), `connectors/obsidian_tasks.py` 쓰기 헬퍼.
- config `apply` 섹션(정책·백오프·알림 분). 테스트 9개 추가(TC17·TC18·TC19·TC23, 지시 자동 등록, 🆔 부여, 충돌, 쓰기 토큰 없음, 백업·미실행 안내). 총 58건.
- `claire_store resume`(대기·보류 해제), `ignore-sender`(발신자·도메인 제외), `references/usage-guide.md`(활용 시나리오, "사용법 알려줘").

### 공개 준비
- 기본 설정에서 개인값(계정·캘린더 ID·볼트 경로)을 제거하고 `config.example.json` 으로 옮김. doctor 에 `config_values` 검사 추가.
  테스트·픽스처는 가상 계정(`prof@example.com`)으로 교체. 쓰기 토큰에 `userinfo.email` 범위 추가(발급 직후 계정 확인용).

## [0.3.0] — 2026-09-11

PRD v1.3 §13 **3단계: 질문·완료·검색**. 외부 반영은 여전히 없음(완료 시 Obsidian 체크는 outbox 대기).

### 추가
- `scripts/claire_ops.py`: 필드 갱신(형식 검증, Calendar 원본 필드 거부), 상태 전이표(§5.3), 완료(근거 코드 필수, 질문 withdrawn, outbox 정리),
  답변 반영(needs_info→todo), 되돌리기(payload.before 복원, 답변 되돌리기는 전이까지), 관계·연결, 질문 생애 정리(withdrawn/expired).
- `claire_store propose`: `update`(스레드·시리즈·연결 일치 검증, `--force-cross-thread`), `complete`(`criteria_evidence`), `cancel`, `relate`.
- `claire_store` 명령: `update`, `complete`(`--find`로 후보 1건일 때만, TC8), `reopen`, `cancel`, `hold`, `wait`(+3영업일), `progress`, `answer`(`--question` 없으면 열린 질문 1건일 때만, TC7),
  `relate`, `link`, `merge`, `split`, `redact --confirm`, `undo`, `maintain`.
- `claire_search`: `find`(한국어 어간·FTS·최신성 가중합, 근거 표시), `history`, `trace`(원문↔항목↔이력), `waiting`, `today --basis`(완료 이력 90일 밖 표시).
- SKILL.md: 답변 매칭 규칙(§4.1), 완료·진행·대기·되돌리기·검색 처리표, 절차에 `maintain` 추가.
- 테스트 10개 추가 (TC4, TC5·TC6·TC7, TC8·TC9·TC16, TC11, TC14, TC15, wait/hold/cancel/reopen/progress, Calendar 원본 필드 거부·maintain, merge/split/link/redact, answer 자동 매칭·되돌리기). 총 49건.

## [0.2.0] — 2026-09-11

PRD v1.2 §13 **2단계: 읽기 파일럿**. 외부 반영 없음.

### 추가
- `scripts/claire_sync`: `gmail`(최초 30일 → historyId 증분, 404 시 7일 재수집), `calendar`(4개, −7~+90일 → syncToken, 410 시 전체 재동기화·link 대조),
  `obsidian`(mtime 증분, `92 Templates`·`## Routine` 제외, 90일 이전 완료 제외), `all`(소스별 독립 실패), `ingest-discord`(message_id → sha256 → fingerprint+당일 3단 멱등, `참고:`·`등록:` 접두어),
  `pending --format llm`(스레드·파일·이미지 단위 묶음, 관련 기존 항목, `<자료>` 구분자, 이상 문구 경고).
  결정론적 사전 필터: 발신자·도메인 제외, `Auto-Submitted`, `List-Unsubscribe`/`Precedence: bulk`이면서 본인 주소가 To/Cc에 없는 메일.
  overlay 캘린더는 항목 없이 겹침 확인용으로만, HUR Group은 교수님 참석 이벤트만 통과. 포워드 메일의 원 수신 주소(`received_as`) 기록.
  이미 연결된 항목에는 Calendar(시각·제목·취소→`link.missing`)·Obsidian(마감·완료→`tasks_checked`) 원본 필드를 미러링하고 이력을 남긴다.
- `scripts/claire_store propose`: create/ignore 검증(§7.1 규칙 9개)·한 트랜잭션·`CLR-`/`Q-` 발급·link 자동 생성.
- `scripts/claire_search`: `review`(브리핑 재료: 오늘 일정·overlay 겹침·임박·기한 지남·신규/변경·질문·참고·제외 집계·이상 문구·link 주의·소스 상태), `today`, `week`, `overdue`, `questions`, `show`.
- `claire_run brief`: 질문 `asked_count`·`first/last_asked_at`·`next_ask_at`(D6: 2일 매일 → 주 1회 월요일, 마감 48h 이내 매일, 하루 1회) 갱신.
- `connectors/google_api.py`: `CLAIRE_GOOGLE_FIXTURE` 기록 응답 재생 (테스트).
- `references/extraction-schema.md`, `references/briefing-format.md`. SKILL.md 2단계 절차·해석 규칙·Discord 처리표.
- 테스트 15개 추가 (TC1, TC2, TC3, TC12, TC13, TC21, TC24, propose 검증 9종, 미러링, D6 재질문, 조회).

- 실측 후 추가: Gmail 사전 필터 강화(`sent_by_user`, `ignore_sender_patterns`, `never_ignore_domains`, `list_unsubscribe_rule`, `interpret_days`→`initial_backlog`)와 `claire_sync retriage`.
  반복 시리즈 묶음(`pending`)과 `propose create series:true` 회차 확장. calendar 원문 create 는 `start_at` 생략 시 이벤트 값으로 채움.
  doctor 에 `openclaw.workspace_skill`·`openclaw.cron_daily` 검사. `install.sh` 가 `~/.openclaw/workspace-claire/skills/` 에 SKILL.md·references 복사.
  OpenClaw cron `claire-daily-check`(06:00 Asia/Seoul, Claire 채널 announce) 등록.

### 결정 변경
- D1 개정(2026-09-11, 교수님 지시): 기관 계정은 별도 토큰 없이 gmail로 포워드. 수집 계정 `gmail.accounts`는 gmail 하나, 본인 주소 판정용 `gmail.user_addresses`에 두 주소 유지.

## [0.1.0] — 2026-09-11

PRD v1.1 §13 **1단계: 현황 확인·골격**.

### 추가
- `scripts/claire_core.py`: 설정 기본값(§12·§15 확정값, 실측 캘린더 ID), 스키마 DDL 정본(§5.2 13개 표 + FTS5),
  트랜잭션, 번호 발급(`CLR-`, `Q-`), 이력 기록, 근거 코드 검증, 내용 해시, 한국어 어간(Clio 이식), 원자적 쓰기.
- `scripts/claire_check`: `init`(멱등) · `integrity`(§10 8개 항목) · `backup` · `restore-test`(TC20) · `doctor`(§4.3 실측) · `calibrate`(자리).
- `scripts/claire_run`: `begin`(잠금·합류·26h 지연 감지) · `step` · `brief`(content_hash 중복 방지) · `end`(필수 단계 검사) · `status` · `missed`(--notify).
- `scripts/claire_export`: `json` · `md`.
- `connectors/google_api.py`: gog 클라이언트 재사용 + 읽기 전용 범위 loopback OAuth(PKCE), refresh, Gmail/Calendar REST 최소 호출, 계정 일치 검증.
- `connectors/obsidian_tasks.py`: 볼트 스캔, Tasks 줄 파서(📅⏳🛫✅🔁🆔), `92 Templates`·`## Routine` 제외, 원자적 줄 치환.
- `connectors/discord_files.py`: 첨부 sha256 보관·중복 판정, inbound 요약.
- `tests/test_claire.py`: 22개 테스트 (init 멱등, 스키마 제약, run 잠금·지연, TC20, 무결성, doctor 오프라인, 파서, 첨부, 버전 일관성).
- `install.sh`, `SKILL.md`, `references/`.

### 실측 결과 (doctor, 2026-09-11)
- gog v0.12.0은 gmail.modify 고정·증분 API 없음 → 커넥터는 REST 직접 호출, 토큰은 읽기 전용으로 별도 발급.
- 캘린더 ID 4개 확인. Obsidian 볼트 `#tasks` 310줄(미완료 83) 스캔 0.7초. Discord 첨부는 `~/.openclaw/media/inbound/` 로컬 파일.
