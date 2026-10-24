# 데이터 위치·이전·되돌리기 (0.9.0)

이슈 2026-10-24 "SecretaryData 경로 지원 및 안전한 데이터 이전"(SEC-2026-005). 실제 이전은 교수님이 터미널에서 직접 한다.
Claire(에이전트)는 `claire_relocate`를 실행하지 않는다.

## 1. 위치를 정하는 규칙 — 모든 도구·예약 실행·install/update 가 같다 (`scripts/claire_paths.py`)

| 순서 | 근거 | 설명 |
|---|---|---|
| 1 | 환경변수 `CLAIRE_DATA_DIR` | 그 프로세스에만 적용되는 명시 override (시험·격리 사본용). 위치 기록과 다르면 `where`·doctor 가 경고한다. |
| 2 | 위치 기록 `~/.claire-schedule-manager/data-location.json` | `claire_check init`·`claire_relocate`가 쓴다. **예약 실행(OpenClaw cron)이 쓰는 정본.** |
| 3 | 기존 설치 `~/ClaireData` | 위치 기록이 없고 그곳에 장부·설정·secrets 가 있으면 그대로 쓴다 (0.8.x → 0.9.0 업데이트). |
| 3′ | 은퇴 표지 따라가기 | 위치 기록을 잃었고 옛 위치가 은퇴했으면, 표지의 `moved_to`가 **같은 이전 id 로 들어온 살아 있는 장부**일 때만 그곳을 쓴다(경고 `pointer_missing`, `claire_check init`이 위치 기록을 다시 남긴다). |
| 4 | 새 기본 `~/SecretaryData/services/schedule` | 신규 설치. 이전 흔적(이전 기록·은퇴 표지)이 있는데 정본을 못 찾으면 쓰지 않는다(`pointer_missing_relocated`). |

- 홈은 사용자별로 해석한다(`~`). 특정 사용자 경로를 코드에 적지 않는다.
- 기본값이 바뀌었다고 옛 장부를 버리거나 빈 장부를 만들지 않는다. 0.9.0 으로 처음 `init`(설치·업데이트)하면 지금 위치를 기록한다.
- `claire_relocate where` — 지금 위치·근거·백업 루트·막힌 이유 (`--shell`은 install.sh 용).

### 장부를 열지 않는 상태 (모든 Claire 도구가 `ok: false`로 멈춘다)

| 코드 | 뜻 | 할 일 |
|---|---|---|
| `maintenance` | 데이터 이전·되돌리기 중이거나 중단됨 (`relocation.lock`) | `claire_relocate status` → `resume` 또는 `abort` |
| `data_dir_retired` | 그 위치는 은퇴했다 (이전 뒤 옛 경로를 `CLAIRE_DATA_DIR`로 지정 등) | 환경변수를 지우거나 새 위치로 |
| `data_conflict` | 위치 기록 없이 `~/ClaireData`와 새 기본 위치 모두에 장부가 있다 | 어느 쪽이 정본인지 확인 (추측하지 않는다) |
| `pointer_target_missing` | 위치 기록이 가리키는 장부가 없다 (디스크 분리·이름 변경) | 경로 확인. 빈 장부를 새로 만들지 않는다 |
| `pointer_missing_relocated` | 위치 기록이 없고 이전 흔적은 있는데 정본 장부를 찾지 못했다 | `claire_relocate status`로 새 위치 확인 (디스크 연결). 빈 장부를 새로 만들지 않는다 |
| `pointer_unreadable` · `root_mismatch` | 위치 기록이 깨졌거나 폴더 내용이 바뀌었다 | `claire_relocate status` |

install.sh 도 같은 판단을 먼저 하고, 막힌 이유가 있으면 **어떤 파일도 바꾸기 전에** 멈춘다.
예외 하나: 이전이 **중단된**(실행 중 아님) 상태에서 고친 코드로 `resume`·`abort`해야 할 때만
`CLAIRE_INSTALL_CODE_ONLY=1 bash install.sh`(또는 `CLAIRE_INSTALL_CODE_ONLY=1 claire-update`)로 프로그램 파일만 설치한다 —
장부·설정·위치 기록·백업은 건드리지 않고, 업그레이드 전 사본·`init`·무결성 확인도 건너뛴다.

## 2. 무엇이 어디에

| 자리 | 내용 |
|---|---|
| 데이터 위치 | `claire.db`(WAL), `config.json`, `secrets/`(700·600), `raw/`, `attachments/`, `exports/`, `logs/`, `claire-root.json`(데이터 루트 표지), `relocation/`(이전 기록 사본) |
| 백업 루트 `~/SecretaryBackups/schedule` | 정기 백업 `<시각>/`, 이전 전 백업 `<이전 id>/`, 업그레이드 전 사본 `pre-upgrade-*/`, `redaction_manifest.json` |
| 프로그램 쪽 `~/.claire-schedule-manager` | `src/`(설치 전용 클론), `data-location.json`, `relocation.lock`, `relocations/<id>.json`(이전 기록), `skill-prev/`(직전 스킬) |

- 백업 루트: `claire_check backup --out` > `CLAIRE_BACKUP_ROOT` > config `backup_root` > `~/SecretaryBackups/schedule`.
  0.9.0 부터 데이터 폴더 안 `backups/`를 쓰지 않는다. 옛 `~/ClaireData/backups/`는 그 자리에 그대로 둔다(옮기지도 지우지도 않음).
- 보존 정책(일 7·주 4·월 12)은 **Claire 정기 백업(manifest `kind: claire-backup`·0.8.x 정기 백업)에만** 적용한다.
  이전 전 백업·업그레이드 전 사본·다른 작업이 같은 루트에 둔 사본(예: 사전 백업 `20261024T224649/`)은 정리하지 않는다.
- 미러(`backup_mirror`, Dropbox)는 0.8.x 와 같다. 범위·보존을 넓히지 않았다.
- 옮기지 않는 것: OAuth 클라이언트(`google.oauth_client_file` — 그 자리를 계속 참조), Obsidian 볼트, OpenClaw 내부 저장소,
  Discord 첨부 수신 폴더, 백업 미러.

## 3. 이전 절차 (기존 `~/ClaireData` → `~/SecretaryData/services/schedule`)

전제: 0.9.0 이 설치되어 있다(`claire-update`). 업데이트만으로는 데이터가 옮겨지지 않는다.

```bash
S=~/.claude/skills/claire-schedule-manager/scripts
$S/claire_relocate plan --format text          # 1) 계획 — 아무것도 바꾸지 않는다
$S/claire_relocate migrate --confirm --pause-schedules   # 2) 이전
$S/claire_relocate status --format text        # 3) 확인
$S/claire_check doctor --format text
```

**계획(dry-run)이 보여 주는 것:** 원본·대상 실제 경로와 판단 근거, 장부 크기·WAL·표별 건수, 복사할 파일 묶음·크기, 인증정보 개수(값·이름 없음),
옮기지 않는 것, 장부를 연 다른 프로세스(lsof), 진행 중 실행, Claire 예약과 다음 실행 시각, 중단 범위, 백업 위치·복원 명령, 공간·권한,
절대 경로 참조, 과거 기록 속 옛 경로(바꾸지 않음), 확인할 사용자 파일·문서(셸 설정·LaunchAgents·OpenClaw 설정·워크스페이스·
예약 문구·Obsidian 노트 — 줄 번호만, 고치지 않음), registry 현재 값, 막힌 이유·주의.

**중단 범위:** 이전 잠금 동안 Claire 일정·업무 도구 전부(교수님 메시지에 대한 Claire 턴·cron·수동 실행)가 장부를 열지 않는다.
`--pause-schedules`면 OpenClaw 예약 `claire-daily-check`·`claire-missed-check`·`claire-checkin`만 끄고 끝나면 그것만 다시 켠다.
그 밖의 OpenClaw·다른 에이전트·다른 비서 시스템(`skill-collection-review:claire` 포함)은 멈추지 않는다.
`--pause-schedules` 없이 하면 20분(`--quiet-minutes`) 안에 실행될 Claire 예약이 없을 때만 시작한다.

**`migrate`가 하는 일 (단계마다 이전 기록에 남긴다):**
1. 잠금 → 예약 정지(또는 곧 실행 없음 확인) → 장부를 연 다른 프로세스·진행 중 실행이 없음을 확인(있으면 시작하지 않는다).
2. **백업**: SQLite 온라인 백업 API 로 최신 장부(WAL 에만 있는 기록 포함)를 `백업 루트/<id>/`에. 파일 사본·`SHA256SUMS`·`manifest.json`
   (`kind: relocation`)·가림 목록(옛 백업 루트·미러·새 루트의 목록과 합집합). secrets 는 넣지 않는다. 사전 백업에 기대지 않는다.
3. **임시 폴더** `.schedule.staging-<id>`(대상 옆, 700)에 장부·파일·인증정보(보호 복사)를 놓는다. 데이터 폴더 안을 가리키는
   **현재 파일 참조**(`attachment.stored_path`·`source_event.raw_path`·config 값)만 고친다. 과거 감사 기록(`run.steps` 등)의 경로는 그대로.
4. **검증**: `integrity_check`, 모든 표의 행 수·내용 해시(전체 지문)와 업무·질문·메모 번호, 파일 목록·해시, 첨부 해시, 무결성 결과가
   원본과 같아야 한다. 기존 운영 상태(예: outbox 실패 1건)는 "기존 문제"로 따로 보고하고 그대로 옮긴다 — 이전 중 outbox 를 실행하지 않는다.
5. **전환**: 원본이 백업 뒤 바뀌지 않았는지 다시 보고 WAL 을 합친 뒤, 옛 장부를 `claire.db.relocated-<id>`로 이름만 바꾸고 그 자리에
   은퇴 표지(텍스트 파일)를 둔다. 임시 폴더를 대상 이름으로 바꾼다(비어 있는 폴더만 대체, 내용이 있으면 시작 전에 거부).
6. 위치 기록을 새 위치로, `~/SecretaryData/registry.json`은 `operational_roots.schedule`·`cutovers.schedule`만(잠금·원자적 교체·권한 유지).
   registry 가 없거나 못 고치면 `백업 루트/<id>/registry-handoff.json`을 남긴다(Chris 가 반영).
7. **환경변수 없이**(예약 실행과 같은 조건) 위치·무결성을 다시 확인하고, 옛 경로가 열리지 않는지 확인한다. 전환 기준 지문을 남긴다.
8. 멈춘 예약만 다시 켜고 잠금을 푼다. 이전 기록 사본: `<새 위치>/relocation/<id>.json`, `백업 루트/<id>/relocation.json`.

**옛 위치는 자동으로 지우지 않는다.** `~/ClaireData`에는 은퇴 표지·`claire.db.relocated-<id>`·옛 백업·secrets 사본이 남는다.
몇 주 운영을 확인한 뒤 교수님이 직접 정리한다(특히 `secrets/` 사본).

**다시 실행해도 안전하다:** 이미 옮긴 뒤 `migrate`는 `same_location`으로 거부, update 재실행은 위치 기록을 따라 새 위치를 그대로 쓴다.

## 4. 중단·실패

| 상황 | 결과 (`state`) | 할 일 |
|---|---|---|
| 전환 전 실패 (검증 실패·원본이 백업 뒤 바뀜 등) | 임시 폴더를 치우고 예약을 다시 켜고 원래 위치로 계속 운영 (`failed_clean`, 잠금 해제). 원본 지문이 백업 때와 다르면 `undo.source_note`로 알린다(원본이 정본 — 막지 않는다) | 원인 확인 뒤 새로 `plan` |
| 전환 직후 실패, 새 위치에 기록 없음 | 위치 기록·registry·대상·원본을 자동으로 되돌림 (`failed_clean`) | 새로 `plan` |
| 전환 뒤 새 위치에 기록이 생긴 상태에서 실패·`abort` | **아무것도 바꾸지 않고 멈춘다** (`needs_decision`, 잠금 유지). 새 위치를 지우지 않는다 | `resume --id`(옮긴 행이 모두 남아 있으면 새 기록을 포함해 이전을 마침) 또는 `abort --id <id> --keep-changed-target`(새 위치를 `.<이름>.undone-<id>`로 비켜 두고 원래 위치로 — 그 기록은 그 폴더에 남는다) |
| 새 위치 확인(7단계) 뒤 실패 (예약 다시 켜기 등) | 이전은 끝난 것으로 둔다 (`done` + `warnings`). 다시 켜지 못한 예약은 `openclaw cron enable <id>` 명령으로 남긴다(doctor 에도) | 명령 실행 |
| 프로세스가 죽음 (전원·강제 종료) | 잠금이 남아 Claire 도구가 장부를 열지 않는다 | `status` → `resume --id`(이어서) 또는 `abort --id`(원래대로) |
| 되돌리던 중 죽음 | `undoing` — `resume`은 거부한다 | `abort --id` (되돌리기를 마저 함) |
| 되돌리기마저 실패 | `needs_attention` — 잠금을 남겨 장부를 열지 않는다 | 원인을 고친 뒤 `abort --id` |

- 끝난 작업(`done`·`aborted`·`failed_clean`·`not_started`)에 `resume`·`abort`를 하면 `relocation_closed`로 거부한다.
- 예약을 멈출 때는 끄기 **전에** 이전 기록에 적는다 — 중간에 죽어도 다시 켤 목록을 잃지 않는다. 원래 꺼져 있던 예약은 켜지 않는다.
- 다른 이전은 시작하지 않는다. 동시에 두 번 실행해도 두 번째는 잠금에서 멈춘다(잠금 파일이 바뀌는 순간도 다시 확인한다).
- 코드에 문제가 있어 `resume`·`abort`가 실패하면, 고친 버전을 `CLAIRE_INSTALL_CODE_ONLY=1`로 프로그램 파일만 설치한 뒤 다시 실행한다(§1).

## 5. 되돌리기 (이전이 끝난 뒤)

```bash
$S/claire_relocate rollback                 # 계획 — 새 기록 여부·방식·변화 (바꾸지 않음)
$S/claire_relocate rollback --confirm       # 실행
```

- **전환 뒤 새 기록 없음(revert):** 옛 장부 지문이 이전 때와 같은지 확인 → 새 위치를 은퇴(`claire.db.rolled-back-<id>` + 표지) →
  옛 장부를 원래 이름으로 → 위치 기록·registry 를 옛 위치로. 새 위치에서 갱신된 토큰은 옛 위치로 복사하고, 옛 것은 `<이름>.before-<id>`로 남긴다.
- **새 기록 있음(carry-forward):** 옛 장부로 단순 복귀하지 않는다. 옛 장부 대비 변화(추가·변경·사라진 행)를 보여 주고, 지워지지 않는 표에서
  행이 사라졌으면 거부한다. 실행하면 현재 데이터를 **먼저 백업**하고 같은 이전 절차로 옛 경로에 옮긴다. 옛 위치 폴더는 `<옛 경로>.retired-<id>`로
  비켜 두고 지우지 않는다.
- 새 기록 판정: 장부 전체 표 지문 + 파일 목록·크기·수정 시각이 전환 직후 기준과 다른가 (이전 기록 사본 `relocation/`은 제외).

### 코드 되돌리기와 데이터 위치 되돌리기는 따로다

- 데이터 위치를 먼저 되돌리고(`rollback`), 그다음 코드를 내린다(`claire-update --version v0.8.0`).
- 0.9.0 이전 코드는 위치 기록을 모르고 `CLAIRE_DATA_DIR`·`~/ClaireData`만 연다. 그래서
  - `get.sh`는 지금 정본(예약 실행 기준, 그리고 `CLAIRE_DATA_DIR`이 있으면 그것도)이 정확히 그 폴더이고(같은 폴더 판정),
    막힌 이유(이전 중·중단·은퇴·충돌)가 없고, 그 장부가 은퇴 표지가 아닐 때만 0.9.0 이전 버전으로 내린다.
  - 그 검사를 건너뛰어도 옛 위치의 `claire.db`는 은퇴 표지라 옛 코드는 "file is not a database"로 멈춘다 — 옛 장부를 자동으로 열지 않는다.

## 6. 인증정보

- 백업·내보내기·이전 백업에 넣지 않는다. 출력·이전 기록·로그에 값이나 파일 이름을 적지 않는다(개수만).
- 이전 때만 원본 `secrets/` → 새 위치로 보호 복사(폴더 700·파일 600, 해시 대조). 복원(`claire_check restore`)한 사본에는 없다 —
  격리 사본으로 운영하려면 토큰을 다시 발급하거나 보호 복사한다.

## 7. 백업·복원 (0.9.0)

- `claire_check backup [--out <루트>]` — `<루트>/<시각>/`. manifest 에 데이터 위치·지문·번호·파일 해시·`storage_ok`·기존 운영 문제.
- `claire_check restore-test --from <백업>` — 건수·해시·파일 해시·다시 가려야 할 항목 수.
- `claire_check restore --from <백업> --to <빈 폴더>` — **격리 복원**: 장부·파일 복사, 가림 목록(백업 안·백업 루트·미러·현재 장부) 재적용,
  무결성·건수 확인. 운영 위치·위치 기록 대상에는 복원하지 않는다. 확인: `CLAIRE_DATA_DIR=<폴더> claire_search find <검색어>`.
- 정본 위치의 장부를 백업으로 바꾸는 것(마지막 수단)은 `references/recovery-policy.md`의 절차를 따른다. `claire_relocate`는 정본에서만
  옮기므로 복원 사본·사전 사본을 원본으로 쓰지 않는다.
