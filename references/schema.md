# 스키마 요약 (정본: `scripts/claire_core.py` `SCHEMA_SQL`, PRD §5)

| 표 | 역할 | 유니크·핵심 제약 |
|---|---|---|
| `item` | 관리 항목의 현재 상태 | `ref` 유니크(`CLR-0001`, 재사용 금지). `kind` 6종, `status` 8종, `priority`, `owner` CHECK |
| `source_event` | 수집 원문 단위 = 미처리 큐 + 근거 | `(platform, account, external_id, version)` 유니크. `triage` new→proposed/ignored |
| `item_source` | 항목↔원문 (origin/update/cancel/evidence/mention) | PK `(item_id, source_event_id, role)` |
| `attachment` | Discord 이미지 원본 | `(source_event_id, sha256)` 유니크. 파일은 `attachments/<2자>/<sha256>.<ext>` |
| `question` | 확인 질문 | `ref` 유니크(`Q-0001`). open→answered/withdrawn/expired |
| `activity_log` | 변경 이력·undo 근거 | `action='complete'`의 `reason`이 근거 코드 |
| `relation` | 항목 관계 | `(from, to, rel_type)` 유니크, 자기 참조 금지 |
| `link` | 항목↔Calendar 이벤트/Obsidian 줄/Ledger | `(system, external_key)` 유니크. `sync_status` ok/pending/conflict/missing |
| `sync_state` | 소스별 커서 | PK `(platform, account)`. `full_sync_needed` |
| `run` | 실행 기록·잠금 | `status='running'`이 잠금. `delayed`, `steps` JSON |
| `outbox` | 외부 반영·알림 대기열 | `dedupe_key` 유니크. `requires_confirm` |
| `briefing` | 발송 기록 | `content_hash`로 같은 날 중복 발송 방지 |
| `meta` | `schema_version`, `created_by_version` | |
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

## 시각

모든 시각은 ISO8601 오프셋 포함(`2026-09-11T06:00:00+09:00`). 날짜만인 마감은 `T23:59:59+09:00`.
테스트·재현용으로 `CLAIRE_NOW` 환경변수가 현재 시각을 고정한다.

## 데이터 배치

`CLAIRE_DATA_DIR`(기본 `~/ClaireData`) 아래 `claire.db`, `config.json`, `secrets/`(700, 파일 600),
`raw/`, `attachments/`, `exports/`, `backups/`, `logs/`. Dropbox·iCloud 안에 두면 `init`과 `doctor`가 경고·실패로 표시한다.
