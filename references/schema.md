# 스키마 요약 (정본: `scripts/claire_core.py` `SCHEMA_SQL`, PRD §5)

| 표 | 역할 | 유니크·핵심 제약 |
|---|---|---|
| `item` | 관리 항목의 현재 상태 | `ref` 유니크(`CLR-0001`, 재사용 금지). `kind` 6종, `status` 8종, `priority`, `owner` CHECK. v2: `due_precision`(date/time), `window_auto`(달력 구간을 마감에서 계산), `attendance`(undecided/attending/declined), `daily_logged_on`(완료 기록이 있는 Daily 날짜) |
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
| `meta` | `schema_version`, `created_by_version`, `daily_done_since`(Daily 완료 기록 시작 시각), `migrated_v2` | |
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

## 시각

모든 시각은 ISO8601 오프셋 포함(`2026-09-11T06:00:00+09:00`). 날짜만인 마감은 `T23:59:59+09:00`(`due_precision=date`).

## 스키마 변환

`SCHEMA_VERSION` 2(0.5.0). 기존 DB는 `connect()`가 열 때 `claire_core.migrate()`로 열만 추가한다(앞으로만, 멱등).
`install.sh`는 버전이 바뀌면 변환 전에 `backups/pre-upgrade-<이전>-to-<새>-<시각>/claire.db`를 남긴다.
v1 코드는 v2 DB를 읽을 수 있다(추가 열만 있음). 완전히 되돌리려면 그 사본을 복원한다(`recovery-policy.md`).
테스트·재현용으로 `CLAIRE_NOW` 환경변수가 현재 시각을 고정한다.

## 데이터 배치

`CLAIRE_DATA_DIR`(기본 `~/ClaireData`) 아래 `claire.db`, `config.json`, `secrets/`(700, 파일 600),
`raw/`, `attachments/`, `exports/`, `backups/`, `logs/`. Dropbox·iCloud 안에 두면 `init`과 `doctor`가 경고·실패로 표시한다.
