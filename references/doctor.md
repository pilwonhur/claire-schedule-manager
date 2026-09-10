# `claire_check doctor` — 진단 항목 (PRD §4.3, §11)

`--offline`은 네트워크 없이 파일·설정만 본다. 기본(live)은 실제 API를 호출한다.
`--format text`는 표, 기본은 JSON(`checks[]`에 `area/name/status/detail/fix`).

| 영역 | 항목 | 녹색 조건 | 빨간색일 때 |
|---|---|---|---|
| env | python | 3.11+ | Python 업그레이드 |
| env | sqlite_fts5 | FTS5 사용 가능 | Homebrew python3 사용 |
| env | gog_cli | 참고용 (없어도 warn) | — |
| data | data_dir | 존재하고 Dropbox·iCloud 밖 | `CLAIRE_DATA_DIR` 이동 |
| data | database | 스키마 있고 무결성 이상 없음 | `claire_check init` / `integrity` |
| data | dir:secrets | 700, 파일 600 | `chmod` |
| data | config, backup_mirror | 존재 | `claire_check init` |
| google | oauth_client | gog 자격증명 파일(`client_id/secret`) 읽힘 | `config.google.oauth_client_file` |
| google | token:<email>:read | 읽기 전용 토큰 존재, 600, 범위 = gmail.readonly+calendar.readonly, 쓰기 범위 없음, 계정 일치 | `google_api.py authorize <email> --kind read` |
| google | gmail:<email> | `users.getProfile`이 같은 계정을 돌려줌 | 토큰 재발급 |
| google | calendar:<이름> | `calendarList.get`이 성공, write 캘린더는 owner/writer | 공유 권한 |
| google | token:<email>:write | 4단계 전에는 warn | — |
| obsidian | vault, daily_folder, navigation_guide, exclude:* | 경로 존재 | `config.obsidian.vault` |
| obsidian | tasks_scan | `#tasks` 줄 1건 이상 | 태그·경로 확인 |
| obsidian | write_access | 볼트 쓰기 가능 (4단계용) | — |
| discord | inbound_media_dir | OpenClaw 첨부 폴더 읽기 가능 | `config.discord.inbound_media_dir` |
| discord | claire_agent_token | 파일 존재만 확인 (내용 안 읽음) | OpenClaw 에이전트 설정 |
| discord | message_id_passthrough | 항상 warn. 2단계 첫 실제 메시지로 닫는다 | — |
| openclaw | workspace_skill | `~/.openclaw/workspace-claire/skills/claire-schedule-manager/SKILL.md`가 설치본과 같음 | `install.sh` |
| openclaw | cron_daily | agent=claire, `0 6 * * *` @ Asia/Seoul, enabled | 아래 cron 등록 명령 |

`all_green`은 fail·skip이 없고 live 모드일 때만 true다. 1단계 완료 기준 "doctor 전 항목 녹색"은 이 값이다.

## 토큰 발급 절차 (교수님이 직접)

```bash
C=~/.claude/skills/claire-schedule-manager/connectors
python3 $C/google_api.py authorize <계정 이메일> --kind read
~/.claude/skills/claire-schedule-manager/scripts/claire_check doctor --format text
```

브라우저가 열리면 **해당 계정**으로 로그인해 동의한다. 다른 계정으로 동의하면 도구가 저장하지 않고
`oauth_wrong_account`로 거부한다. 쓰기 범위가 섞이면 `oauth_scope_too_broad`로 거부한다(D1).
기관 메일이 gmail로 포워드되면 기관 계정 토큰은 필요 없다(D1 개정). 기관 계정을 직접 수집해야 하고 그 계정이 Google Workspace라 조직 정책으로 외부 앱을 막으면 `access_denied`/`admin_policy_enforced`가 나온다.
그 경우 기관 관리자에게 gog OAuth 클라이언트 허용을 요청하거나, 별도 OAuth 클라이언트를 만들어
`config.google.oauth_client_file`에 지정한다.

## OpenClaw 연결 (2단계)

OpenClaw는 `~/.claude/skills`를 보지 않고, 워크스페이스 밖으로 나가는 심볼릭 링크도 거부한다(`symlink-escape`).
그래서 `install.sh`가 `~/.openclaw/workspace-claire/skills/claire-schedule-manager/`에 SKILL.md·references를 **복사**한다.
실행 스크립트는 `~/.claude/skills/claire-schedule-manager/scripts/` 한 곳만 쓴다.

06:00 cron (declaration key `claire-daily-check`. 채널 ID는 `openclaw agents bindings` 로 확인):

```bash
openclaw cron add --agent claire --name "Claire 일일 점검" \
  --cron "0 6 * * *" --tz Asia/Seoul --exact --session isolated \
  --channel discord --account claire --to "channel:<Claire 채널 ID>" --announce --best-effort-deliver \
  --timeout-seconds 3600 --declaration-key claire-daily-check \
  --message "일일 점검을 실행하고 브리핑을 보내세요. claire-schedule-manager 스킬의 '06:00 정기 점검 절차'를 순서대로 따르세요."
openclaw cron list | grep "Claire 일일"      # 확인
openclaw cron run <id>                        # 즉시 실행(디버그)
```
