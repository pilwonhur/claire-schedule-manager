# 보존·삭제·백업·복구 (PRD §10)

## 삭제 계층
- "지워줘" → `cancelled` (데이터 유지, 번호 재사용 없음).
- 민감 원문 제거 → `claire_store redact --confirm`(3단계). `source_event.excerpt`·`raw/`·첨부 본문을 지우고 `redacted_at`을 남긴다.
- `raw/`의 `ignored` 메일 본문은 30일 후 정리. 헤더·fingerprint는 유지한다.

## 백업 (`claire_check backup`)
- 0.9.0: 백업 루트(`--out` > `CLAIRE_BACKUP_ROOT` > config `backup_root` > `~/SecretaryBackups/schedule`)의 `<시각>/`에 만든다.
  데이터 폴더 안 `backups/`는 더 쓰지 않는다(옛 `~/ClaireData/backups/`는 그대로 남긴다). 위치 규칙은 `references/data-location.md`.
- `sqlite3` 온라인 백업 API 스냅샷 + `claire-export.json` + `markdown/` + `attachments/`·`raw/` 사본 + `config.json` + `SHA256SUMS` + `manifest.json`.
- `secrets/`는 **절대 포함하지 않는다**. `restore-test`가 `secrets_included`로 다시 확인한다.
- 보존: 일 7 · 주 4 · 월 12 — Claire 정기 백업(manifest `kind: claire-backup`)에만. 이전 전 백업·업그레이드 전 사본·다른 작업의 사본은 지우지 않는다.
  미러 `config.backup_mirror`(기본 `~/Dropbox/ClaireBackup`). DB 원본은 Dropbox에 두지 않는다.
- `manifest.json`의 `counts`·`content_hash`(item, source_event, item_source, question, activity_log, relation, link 표 내용 해시)가 복구 대조 기준이다.
  0.9.0 은 전체 지문(`fingerprint`, 모든 표)·번호 요약(`refs`)·파일 해시·`storage_ok`·기존 운영 문제(`operational_problems`)도 남긴다.
- 가림 목록 `redaction_manifest.json`(백업 루트·각 백업 안·미러)은 합집합으로만 늘어난다. 지운 개인정보를 옛 백업으로 되살리지 않기 위한 목록이다.

## 복구 시험 (`claire_check restore-test --from <dir>`) — TC20
- 백업 DB를 메모리에 복원하고 `manifest_match`(건수·해시), `restored_healthy`(무결성), `secrets_included`, `db_in_synced_folder`,
  파일 해시(`SHA256SUMS`), 다시 가려야 할 항목 수를 낸다. 월 1회 실행을 권장한다.

## 격리 복원 (`claire_check restore --from <백업> --to <빈 폴더>`, 0.9.0)
- 빈 폴더에만 복원한다(운영 위치·위치 기록 대상은 거부). 장부(백업 API)·첨부·원문·config 를 옮기고, 가림 목록(백업 안·백업 루트·미러·
  현재 장부)을 **다시 적용**한 뒤 무결성·건수를 확인한다. 인증정보는 없다.
- 확인: `CLAIRE_DATA_DIR=<폴더> claire_search find <검색어>`, `CLAIRE_DATA_DIR=<폴더> claire_check integrity`.

## 운영 장부를 백업으로 바꾸기 (마지막 수단 — 그 사이 쌓인 변경이 사라진다)
1. `claire_check restore --from <백업> --to <빈 폴더 R>`로 격리 복원하고 확인한다.
2. OpenClaw Claire 예약을 멈춘다(`openclaw cron disable <claire-daily-check·claire-missed-check·claire-checkin 의 id>`).
   `claire_relocate where`로 정본 위치 D 를 확인한다.
3. D 의 `claire.db`·`-wal`·`-shm`, `attachments/`·`raw/`를 지우지 말고 이름을 바꿔 보관한 뒤(`*.replaced-<시각>`), R 의 것을 D 로 복사한다.
   `secrets/`·`config.json`·`claire-root.json`은 D 의 것을 그대로 둔다.
4. `claire_check integrity`(환경변수 없이) → 예약을 다시 켠다.

## 무결성 (`claire_check integrity`)
고아 `item_source`·`link`·`attachment`·`question`·`relation`·`activity_log`, 근거 없는 `done`, 열린 질문 없는 `needs_info`,
`outbox` 실패 잔량, 48시간 넘게 성공 없는 `sync_state`, 첨부 sha256 불일치, `ref` 중복·공백, FTS 건수 불일치, `running` 실행 2건 이상, `PRAGMA integrity_check`.

## 업그레이드·되돌리기 (0.5.0, 0.9.0 경로)
- `install.sh`는 버전이 바뀌면 스키마 변환 전에 `<백업 루트>/pre-upgrade-<이전>-to-<새>-<시각>/claire.db`(0.8.x 까지는 `~/ClaireData/backups/`)를 남기고,
  직전 스킬 폴더를 `~/.claire-schedule-manager/skill-prev/`(0.8.x 까지는 `~/ClaireData/skill-prev/`)에 한 세대 보관한다.
- 코드만 되돌리기: `claire-update --version v0.4.5`. 스키마 v2 의 추가 열은 옛 코드가 무시한다.
- **데이터를 새 위치로 옮긴 뒤 0.9.0 이전 코드로 내리려면 데이터 위치를 먼저 되돌린다**(`claire_relocate rollback`). `get.sh`가 이를 검사하고,
  옛 위치의 은퇴 표지가 옛 코드를 멈춘다(`references/data-location.md` §5).
- DB까지 되돌리기: 위 "운영 장부를 백업으로 바꾸기" 절차로 pre-upgrade 사본을 쓴다.

