# 스키마 요약 (정본: `scripts/claire_core.py` `SCHEMA_SQL`, PRD §5)

| 표 | 역할 | 유니크·핵심 제약 |
|---|---|---|
| `item` | 관리 항목의 현재 상태 | `ref` 유니크(`CLR-0001`, 재사용 금지). `kind` 6종, `status` 8종, `priority`, `owner` CHECK. v2: `due_precision`(date/time), `window_auto`(달력 구간을 마감에서 계산), `attendance`(undecided/attending/declined), `daily_logged_on`(완료 기록이 있는 Daily 날짜). v3: `request_scope`(direct/group/self/none), `priority_source`(rule/claire/user/unreviewed)·`priority_reason`(분류 근거), `check_every_days`·`review_after`(교수님 지정 주기·다음 확인일), `close_reason`(cancelled/not_needed/not_mine), `last_exposed_at`·`last_asked_at`(전송 성공 기준)·`last_reported_at`(교수님 보고). v4: `short_label`(보고 키워드 1~3단어)·`last_detailed_at`(아침 보고에 상세·버튼으로 전달된 시각 — 돌려 가며 고르기) |
| `source_event` | 수집 원문 단위 = 미처리 큐 + 근거 | `(platform, account, external_id, version)` 유니크. `triage` new→proposed/ignored |
| `item_source` | 항목↔원문 (origin/update/cancel/evidence/mention) | PK `(item_id, source_event_id, role)` |
| `attachment` | Discord 이미지 원본 | `(source_event_id, sha256)` 유니크. 파일은 `attachments/<2자>/<sha256>.<ext>` |
| `question` | 확인 질문 | `ref` 유니크(`Q-0001`). open→answered/withdrawn/expired |
| `activity_log` | 변경 이력·undo 근거 | `action='complete'`의 `reason`이 근거 코드 |
| `relation` | 항목 관계 | `(from, to, rel_type)` 유니크, 자기 참조 금지 |
| `link` | 항목↔Calendar 이벤트/Obsidian 줄/Ledger | `(system, external_key)` 유니크. `sync_status` ok/pending/conflict/missing |
| `sync_state` | 소스별 커서 | PK `(platform, account)`. `full_sync_needed` |
| `run` | 실행 기록·잠금 | `status='running'`이 잠금. `delayed`, `steps` JSON |
| `outbox` | 외부 반영·알림 대기열 | `dedupe_key` 유니크. `requires_confirm`. v2: `selected_at`(선택 일괄 승인), 교수님이 "등록 안 함"이면 `status=cancelled` + `result.declined` |
| `briefing` | 발송 기록 | `content_hash`로 같은 날 중복 발송 방지 |
| `checkin` | 12:00·18:00 미등록 일정 확인 (v2) | `(day, slot)` 유니크 — 하루·슬롯 1회. `message_id`, `result` |
| `delivery` | 보고 전달 기록 (v3) | `(report_key, page)` 유니크. 페이지마다 `item_refs`(상세·버튼)·`check_refs`·`question_refs`(`NOTE:프로젝트` = 문서 위치 질문)·`mention_refs`(v4, 키워드로만 언급)·`payload`(재전송용). `status` generated→sent/failed(→superseded). sent 일 때만 항목 `last_exposed_at`(상세·키워드)·`last_detailed_at`·`last_asked_at`(상세의 진행 확인만)·질문 `asked_count`·`note_map.asked_count` 갱신 |
| `note_map` | 프로젝트 → Obsidian 주제 문서 (v4) | `project` 기본 키, `note`(볼트 기준 경로), `status` pending(처음 봄, 후보 `candidates` JSON)/confirmed(교수님 확인)/inbox(수집함에 두고 묻지 않음), `asked_count`·`last_asked_at`(한 번 묻고 7일 뒤 한 번 더) |
| `meta` | `schema_version`, `created_by_version`, `daily_done_since`(Daily 완료 기록 시작 시각), `migrated_v2`·`migrated_v3`, `tracking_since`(노출 추적 시작), `importance_backfilled`(소급 분류 1회) | |
| `search_fts` | FTS5 (ref, title, project, next_action, waiting_on, excerpts) | rowid = item.id |

## 상태 전이 (§5.3)

```
captured ─(필드 부족)─▶ needs_info ─(답변)─▶ todo
   └─(필드 충분)─▶ todo ─▶ in_progress ─▶ done
                    ├─▶ waiting ─(회신 옴)─▶ todo/in_progress
                    ├─▶ on_hold ─▶ todo
                    └─▶ cancelled
done ─("다시 열어")─▶ todo  (reopen 이력)
```

- `overdue`는 상태가 아니라 `due_at < now AND status NOT IN ('done','cancelled')`.
- `cancelled`는 `close_reason`으로 뜻을 나눈다(v3): `cancelled`(업무 자체 취소) · `not_needed`(처리 불필요) · `not_mine`(내 할 일 아님).
  셋 다 활성 목록·진행 확인에서 빠지고 이력은 남는다. `reopen`이 `close_reason`을 지운다. 달력·Tasks "표시 안 함"은 상태가 아니다(`outbox` declined).
- `done` 전이의 근거 코드: `user_report` | `tasks_checked` | `criteria_evidence:<source_event_id>`.
  `claire_core.validate_evidence`가 그 외 값을 거부하고, `claire_check integrity`가 근거 없는 `done`을 잡는다.
- Calendar에서 사라진 이벤트는 `cancelled`가 아니라 `link.sync_status='missing'`.

## 업무 상태 vs 외부 반영 상태 (v2)

`item.status`는 업무 상태다. 달력·Tasks·Daily 반영은 `link`·`outbox`에서 따로 계산한다(`claire_ops.sync_state_of`):
`calendar`/`tasks` = linked(등록됨) · awaiting_confirm(승인 대기) · pending(반영 예정) · retrying · failed · declined(등록 안 함) · missing · conflict,
`daily` = recorded · pending · retrying · failed. 조회 결과의 `state_line`이 둘을 한 줄로 보여 준다.

## 마감 시각과 달력 구간 (v2)

`due_at`은 마감 시각, `start_at`·`end_at`은 (회의가 아니면) 달력 표시 구간이다. `due_precision=time`이고 구간이 없으면
마감 60분 전~마감을 만든다(`window_auto=1`). Obsidian Tasks(날짜만)는 확정된 시각을 덮지 않는다(날짜만 옮기고 시각 유지).
달력에서 블록을 옮기면 구간만 따라오고 `window_auto=0`, 제목의 `[마감] `은 떼고 저장한다.

## 진행 확인 계획 (v3, `claire_track.check_plan`)

닫힌 업무 제외. 오늘 보고받았으면 묻지 않음. 대기·보류는 `next_check_at`(재확인일)이 되면 `recheck`(답이 없어도 매일), 재확인일이 마감보다
늦으면 마감 전날·당일에 `wait_due`(0.7.0). 재확인일이 없는 옛 대기·보류는 upkeep 이 기본 3일 뒤로 채운다. `captured`는 목록만.
`review_after`(교수님 지정) 전에는 묻지 않음. 지난 회의 → `meeting_passed`, 기한 경과 → `overdue`, 당일 → `d_day`, 전날 → `d_minus_1`.
그 밖에는 마지막 확인·보고·생성일에서 `check_every_days` 또는 중요도별 주기(high 1 · normal 2 · low 7일, normal·low 는 주말 제외).
오늘 이미 물은 업무는 그날 내내 오늘의 확인 목록에 남는다(재실행해도 같은 보고). 추적 시작(`meta.tracking_since`) 전부터 있던 업무의
첫 확인은 주기 안에서 번호 순으로 나눈다(0.6.1 — 업그레이드 첫날 밀린 업무가 한꺼번에 몰리지 않게).

## 아침 보고 (v4, `claire_track.report_data`)

활성 업무를 한 곳에만 넣는다: `today_cal`(오늘 시작하는 회의·작업 예약·마감 표시) → `today_due`(달력 밖 오늘 마감, 아직 안 지남) →
`past_meetings`(끝난 일정 — 보고에서 빼고 건수만) → `captured` → `parked`(재확인일 전 대기·보류) → `snoozed` → `overdue`(기한 지남) →
`progress`(오늘 확인 차례) → `upcoming`(앞으로의 일정) → `scheduled`(확인 차례 아님). `accounting.ok` = 합이 활성 수와 같음.
기한 지남은 상세 10건(높음이 많으면 15건), 진행 확인은 쪽마다 15건 × 최대 2쪽. 상세 점수(`detail_score`): 중요도(300/200/100) +
직접 요청 60 + 내일·마감 임박 120·재확인 90 + 최근에 지난 기한(최대 60) + 상세로 안 보인 날수×8(최대 80). 나머지는 `keyword_block`
(분야별, `report.keyword_chars` 상한, 넘으면 "외 N건").

## 시간 종류 (v3, `time_kind`)

`appointment`(회의·수업·면담) · `work`(마감 아닌 항목에 교수님이 잡은 시간, `window_auto=0`) · `deadline_marker`(마감 전 자동 표시 구간,
`window_auto=1`, 또는 구간이 정확히 마감 N분 전~마감). 충돌(`overlaps`)은 appointment·work·overlay 끼리만. deadline_marker 끼리는
`deadline_clusters`(작업량 안내). 표시가 꺼진 옛 마감 표시 구간은 `claire_track.upkeep`(설치·maintain·업무 현황)이 `window_auto=1`로 보정한다.
Calendar 동기화는 시각을 순간(instant)으로 비교한다(표기만 다른 값을 "교수님이 옮김"으로 보지 않는다).

## 시각

모든 시각은 ISO8601 오프셋 포함(`2026-09-11T06:00:00+09:00`). 날짜만인 마감은 `T23:59:59+09:00`(`due_precision=date`).

## 스키마 변환

`SCHEMA_VERSION` 4(0.7.0: 열 3개·`note_map` 표 추가, 값 변경 없음 — upkeep 이 재확인일 채우기·승인 대기 Tasks 자동 전환). 3(0.6.0). 기존 DB는 `connect()`가 열 때 `claire_core.migrate()`로 열만 추가한다(앞으로만, 멱등).
v2→v3 은 값을 바꾸지 않는다(원본 ID·상태·완료 이력·"등록 안 함" 기록 보존). 기존 활성 업무의 중요도 소급 분류는 설정(본인 주소)이
필요해서 `claire_track.upkeep`이 한다 — 설치(`claire_check init`)·`maintain`·업무 현황 앞에서, 분류 없는 활성 업무만 골라 멱등하게
(Discord 등록 → 높음, 메일 → 잠정값 + 검토 필요, 캘린더·Obsidian → 보통, 이미 높음은 유지).
`install.sh`는 버전이 바뀌면 변환 전에 `backups/pre-upgrade-<이전>-to-<새>-<시각>/claire.db`를 남긴다.
v1 코드는 v2 DB를 읽을 수 있다(추가 열만 있음). 완전히 되돌리려면 그 사본을 복원한다(`recovery-policy.md`).
테스트·재현용으로 `CLAIRE_NOW` 환경변수가 현재 시각을 고정한다.

## 데이터 배치

`CLAIRE_DATA_DIR`(기본 `~/ClaireData`) 아래 `claire.db`, `config.json`, `secrets/`(700, 파일 600),
`raw/`, `attachments/`, `exports/`, `backups/`, `logs/`. Dropbox·iCloud 안에 두면 `init`과 `doctor`가 경고·실패로 표시한다.
