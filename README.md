# claire-schedule-manager

[![tests](https://github.com/pilwonhur/claire-schedule-manager/actions/workflows/ci.yml/badge.svg)](https://github.com/pilwonhur/claire-schedule-manager/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/pilwonhur/claire-schedule-manager?sort=semver)](https://github.com/pilwonhur/claire-schedule-manager/releases)

**Claire 통합 일정·업무 관리 시스템** — Gmail, Google Calendar, Obsidian Tasks, Discord 이미지에서 들어오는
업무·일정을 하나의 장부(SQLite)에 안정된 번호로 모으고, 매일 아침 같은 절차로 점검해 브리핑·확인 질문·
진행 추적·검색을 제공하는 [OpenClaw](https://docs.openclaw.ai) / Claude Code 스킬.

설계 원칙 (PRD §1.3):

1. **판단은 LLM(Claire)이, 수집·저장·반영은 결정론적 도구가 한다.** Claire는 SQL을 쓰지 않고 API를 직접 부르지 않는다.
2. **원본은 하나.** 일정 필드는 Calendar, 노트·Tasks는 Obsidian, 상태·질문·이력은 DB.
3. **근거 없이 완료하지 않는다.** 메일 읽음, 일정 종료, 노트에서 사라짐은 완료가 아니다.
4. **추측 대신 질문, 질문 대신 조사.** 질문에는 후보와 이유가 붙는다.
5. **실패는 드러낸다.** 실패한 소스는 "신규 없음"이 아니라 "마지막 성공 시각"으로 보인다.
6. **입력 원문은 자료이지 명령이 아니다.** 메일·이미지 속 문구가 행동을 지시할 수 없다.
7. **업무 상태와 외부 도구 반영 상태는 따로다.** "할 일이면서 달력 등록됨", "참석 미정이면서 달력 승인 대기"를 구분해 보여 준다.

## 구성

```
SKILL.md              판단 계층 규칙 (LLM 이 읽는다)
scripts/              결정론적 도구 (Python 3.11+, 표준 라이브러리만)
  claire_sync         수집: gmail | calendar | obsidian | ingest-discord | pending | retriage
  claire_buttons      Discord 버튼 페이로드: build (본문 → components) | actions (항목·질문 카드) | approvals (승인 대기 목록)
  claire_store        저장: propose | update | complete | answer | wait | undo | merge | ...
  claire_search       조회: review | today | week | overdue | find | history | trace | waiting | dupcheck
  claire_apply        외부 반영: outbox 실행·승인·선택 (Obsidian 🆔·체크·Daily 줄·완료 기록, Calendar 등록·변경)
  claire_run          실행 관리: begin | step | brief | end | status | missed | checkin (12·18시 미등록 일정 확인)
  claire_check        init | doctor | integrity | backup | restore-test | version
  claire_export       json | md
connectors/           Google OAuth·REST(읽기/쓰기 토큰 분리), Obsidian Tasks 파서·원자적 쓰기, Discord 첨부
references/           브리핑 형식, 제안 JSON 규격, 스키마, 진단, 복구 정책, 활용 시나리오(usage-guide.md)
tests/                81개 테스트 (Google API 는 기록된 fixture 로 대체, 설치·업그레이드 경로 포함)
docs/manual.md        사용 안내
VERSION               배포 버전 (claire_core.CLAIRE_VERSION·SKILL.md·CHANGELOG 와 같아야 한다 — 테스트·CI 가 검사)
get.sh                설치·업그레이드 (GitHub 릴리스 태그) · install.sh 실제 설치 · release.sh 릴리스(관리자)
```

데이터는 저장소·스킬 폴더 밖 `~/ClaireData/`에 둔다 (Dropbox·iCloud 밖). 인증 토큰은 `~/ClaireData/secrets/`(600)에만 있고
백업·내보내기에 포함되지 않는다.

## 설치·업그레이드

한 줄이면 된다. 처음 설치와 업그레이드가 같은 명령이다.

```bash
curl -fsSL https://raw.githubusercontent.com/pilwonhur/claire-schedule-manager/main/get.sh | bash
```

설치 뒤에는 `claire-update`(`~/.local/bin`)를 쓴다.

| 명령 | 하는 일 |
|---|---|
| `claire-update` | 가장 높은 릴리스 태그(`vX.Y.Z`)로 업그레이드 |
| `claire-update --check` | 설치 버전과 최신 릴리스만 비교 (아무것도 바꾸지 않음) |
| `claire-update --version v0.4.5` | 특정 버전으로 설치 — 되돌리기 |
| `claire-update --main` | 릴리스 전 `main` 최신 커밋 (시험용) |
| `claire-update --skip-tests` | 설치 전 테스트 생략 |
| `claire_check version --remote` | 설치 버전·스키마·설치 출처(태그·커밋)와 최신 릴리스 (JSON) |

`get.sh`가 하는 일:

1. `~/.claire-schedule-manager/src`(Dropbox·iCloud 밖, 설치 전용 클론)에 저장소를 받거나 `fetch` 한다. 그 폴더에 손으로 고친 파일이 있으면 멈춘다.
2. 대상 태그를 체크아웃하고 **그 버전의 테스트를 돌린다.** 실패하면 설치하지 않는다.
3. `install.sh`를 부른다: 바뀐 파일만 `~/.claude/skills/claire-schedule-manager`에 복사, 직전 스킬을 `~/ClaireData/skill-prev`에 보관,
   버전이 바뀌면 **스키마 변환 전 DB 사본**을 `~/ClaireData/backups/pre-upgrade-*`에 남긴 뒤 `claire_check init`(멱등 변환)·`integrity`.
   OpenClaw 에이전트(`claire`)가 있으면 `~/.openclaw/workspace-claire/skills/`에 SKILL.md·references 를 복사한다. 설치 출처는 `INSTALL_INFO.json`.
4. 데이터(`~/ClaireData`)와 `config.json`의 사용자 값은 건드리지 않는다(새 키만 기본값으로 채운다).

업그레이드 뒤 `claire_check doctor --format text`가 새 버전에 필요한 OpenClaw 설정·cron을 알려 준다.
개발 중에는 저장소에서 `./install.sh`를 직접 돌려도 된다(설치 기록에 "커밋 안 된 변경 포함"이 남는다).

### 릴리스 (관리자, mac mini)

```bash
./release.sh bump 0.5.1      # VERSION·claire_core.py·SKILL.md 버전을 바꾸고 CHANGELOG 에 빈 절
$EDITOR CHANGELOG.md         # [0.5.1] 절 작성
git commit -am "0.5.1: …"
./release.sh tag             # 버전 일치·테스트 확인 → v0.5.1 태그 → main 과 태그 push (확인 질문)
```

태그가 올라가면 GitHub Actions 가 테스트 후 Release(CHANGELOG 절 + 설치 명령)를 만든다. 각 기기에서 `claire-update`.
버전은 SemVer: 동작 추가·스키마 변경은 minor, 버그 수정·문서는 patch. 데이터 형식은 `SCHEMA_VERSION`으로 따로 관리한다(앞으로만 변환).

## 설정

`~/ClaireData/config.json`에 계정·캘린더·볼트 경로를 채운다. `config.example.json`을 참고한다.

```bash
cp config.example.json ~/ClaireData/config.json     # 처음이면
$EDITOR ~/ClaireData/config.json
```

| 키 | 의미 |
|---|---|
| `gmail.accounts` | 수집할 Gmail 계정 (읽기 전용 토큰을 계정마다 발급) |
| `gmail.user_addresses` | "본인 주소" 판정용. 포워드되는 기관 주소도 포함 |
| `calendars[]` | 읽을 캘린더. `mode: track`(항목 생성) / `overlay`(겹침 확인만). `write: true`는 정확히 1개 |
| `obsidian.vault` | 볼트 경로. `#tasks` 줄을 읽고, `92 Templates`·`## Routine`은 제외 |
| `apply.*` | 외부 반영 정책 (발견한 일정의 Calendar 등록은 기본 승인 필요) |

Google 토큰 (브라우저 로그인, `gog` CLI 의 OAuth 클라이언트를 재사용):

```bash
C=~/.claude/skills/claire-schedule-manager/connectors
python3 $C/google_api.py authorize me@gmail.com --kind read     # gmail.readonly + calendar.readonly
python3 $C/google_api.py authorize me@gmail.com --kind write    # calendar.events (쓰기 캘린더 계정)
~/.claude/skills/claire-schedule-manager/scripts/claire_check doctor --format text
```

`doctor` 가 전 항목 녹색이면 06:00 cron 을 등록한다 (`references/doctor.md`).

## 테스트

```bash
python3 tests/test_claire.py     # 표준 라이브러리만, 네트워크 없음. CI: ubuntu·macOS × Python 3.11·3.14
```

## 문서

- `docs/manual.md`, `references/usage-guide.md` — 사용 안내와 활용 시나리오
- `CHANGELOG.md` — 단계별 구현 이력 (PRD §13: 1 골격 → 2 읽기 파일럿 → 3 질문·완료·검색 → 4 외부 반영)

## 라이선스

MIT
