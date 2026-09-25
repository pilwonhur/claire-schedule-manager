# 보존·삭제·백업·복구 (PRD §10)

## 삭제 계층
- "지워줘" → `cancelled` (데이터 유지, 번호 재사용 없음).
- 민감 원문 제거 → `claire_store redact --confirm`(3단계). `source_event.excerpt`·`raw/`·첨부 본문을 지우고 `redacted_at`을 남긴다.
- `raw/`의 `ignored` 메일 본문은 30일 후 정리. 헤더·fingerprint는 유지한다.

## 백업 (`claire_check backup`)
- `sqlite3` 온라인 백업 API 스냅샷 + `claire-export.json` + `markdown/` + `attachments/`·`raw/` 사본 + `config.json` + `manifest.json`.
- `secrets/`는 **절대 포함하지 않는다**. `restore-test`가 `secrets_included`로 다시 확인한다.
- 보존: 일 7 · 주 4 · 월 12. 미러 `config.backup_mirror`(기본 `~/Dropbox/ClaireBackup`). DB 원본은 Dropbox에 두지 않는다.
- `manifest.json`의 `counts`·`content_hash`(item, source_event, item_source, question, activity_log, relation, link 표 내용 해시)가 복구 대조 기준이다.

## 복구 시험 (`claire_check restore-test --from <dir>`) — TC20
- 백업 DB를 메모리에 복원하고 `manifest_match`(건수·해시), `restored_healthy`(무결성), `secrets_included`, `db_in_synced_folder`를 낸다.
- 실제 복구: `~/ClaireData/claire.db`를 백업의 `claire.db`로 교체(WAL·SHM 파일 삭제) → `attachments/`·`raw/` 복원 → `redaction_manifest.json` 재적용 → `integrity`.
- 월 1회 실행을 권장한다.

## 무결성 (`claire_check integrity`)
고아 `item_source`·`link`·`attachment`·`question`·`relation`·`activity_log`, 근거 없는 `done`, 열린 질문 없는 `needs_info`,
`outbox` 실패 잔량, 48시간 넘게 성공 없는 `sync_state`, 첨부 sha256 불일치, `ref` 중복·공백, FTS 건수 불일치, `running` 실행 2건 이상, `PRAGMA integrity_check`.

## 업그레이드·되돌리기 (0.5.0)
- `install.sh`는 버전이 바뀌면 스키마 변환 전에 `~/ClaireData/backups/pre-upgrade-<이전>-to-<새>-<시각>/claire.db`를 남기고,
  직전 스킬 폴더를 `~/ClaireData/skill-prev/`에 한 세대 보관한다.
- 코드만 되돌리기: `claire-update --version v0.4.5`. 스키마 v2 의 추가 열은 옛 코드가 무시한다.
- DB까지 되돌리기: Claire 를 멈추고(cron 일시 중지) `claire.db`를 pre-upgrade 사본으로 교체(`-wal`·`-shm` 삭제) → `claire_check integrity`.
  그 사이 쌓인 변경은 사라지므로 마지막 수단이다.

