# claire-schedule-manager

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

## 구성

```
SKILL.md              판단 계층 규칙 (LLM 이 읽는다)
scripts/              결정론적 도구 (Python 3.11+, 표준 라이브러리만)
  claire_sync         수집: gmail | calendar | obsidian | ingest-discord | pending | retriage
  claire_store        저장: propose | update | complete | answer | wait | undo | merge | ...
  claire_search       조회: review | today | week | overdue | find | history | trace | waiting
  claire_apply        외부 반영: outbox 실행·승인 (Obsidian 🆔·체크·Daily 줄, Calendar 등록·변경)
  claire_run          실행 관리: begin | step | brief | end | status | missed
  claire_check        init | doctor | integrity | backup | restore-test
  claire_export       json | md
connectors/           Google OAuth·REST(읽기/쓰기 토큰 분리), Obsidian Tasks 파서·원자적 쓰기, Discord 첨부
references/           브리핑 형식, 제안 JSON 규격, 스키마, 진단, 복구 정책, 활용 시나리오(usage-guide.md)
tests/                59개 테스트 (Google API 는 기록된 fixture 로 대체)
docs/manual.md        사용 안내
```

데이터는 저장소·스킬 폴더 밖 `~/ClaireData/`에 둔다 (Dropbox·iCloud 밖). 인증 토큰은 `~/ClaireData/secrets/`(600)에만 있고
백업·내보내기에 포함되지 않는다.

## 설치

```bash
git clone https://github.com/pilwonhur/claire-schedule-manager.git
cd claire-schedule-manager
./install.sh            # ~/.claude/skills/claire-schedule-manager 에 설치, ~/ClaireData 초기화
```

OpenClaw 에이전트(`claire`)가 있으면 `install.sh`가 `~/.openclaw/workspace-claire/skills/`에 SKILL.md·references 를 복사한다.

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
python3 tests/test_claire.py
```

## 문서

- `docs/manual.md`, `references/usage-guide.md` — 사용 안내와 활용 시나리오
- `CHANGELOG.md` — 단계별 구현 이력 (PRD §13: 1 골격 → 2 읽기 파일럿 → 3 질문·완료·검색 → 4 외부 반영)

## 라이선스

MIT
