# Changelog

형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/)를 따르고 버전은 SemVer다.
데이터 형식 버전(`SCHEMA_VERSION`)은 따로 관리한다.

## [0.7.0] — 2026-09-29

교수님 개선 요청 반영(`claire-briefing-simplification-issue-2026-09-29.md`, Epic: 아침 브리핑 간소화 및 대기·Obsidian Tasks 관리 개선).
원칙: **전체 업무는 계속 추적하되, 교수님께는 필요한 내용만 제한된 분량으로 전달한다.** 스키마 v4.
선행 이슈(09-27)의 "매일 모든 미완료 업무 상세 노출"을 분량 제한·요약·요청 시 상세 조회로 바꾼다 — 추적 자체·요약된 업무의 상태는 그대로다.

이슈 §6 의 세부는 교수님이 정했다(2026-09-29): 스레드 = **채널 메시지 1개**(연체 1개·기한 전 최대 2개), 대기·보류 재확인은
**재확인일 06:00 보고, 무응답이면 매일**, 임시 수집 노트 **`01 Inbox/Claire 업무 수집함.md`**, Tasks 는 **승인 없이 자동·기존 Daily 는 그대로**.

### 1. 아침 보고 (`claire_buttons report`)
- 06:00 절차 9단계가 업무 현황(전체) 대신 아침 보고를 보낸다: **오늘**(달력 일정 + 달력 밖 오늘 마감) → **기한 지남 한 메시지** →
  **진행 확인 최대 두 메시지** → **확인·상태 한 메시지**. 빈 구역은 메시지를 만들지 않고 상태 쪽에 한 줄.
- 오늘: 같은 업무의 작업 예약과 실제 마감을 한 줄에(`10:00–12:00 작업 · 마감 9/30 15:00`). 달력에 작업 블록이 있다고 실제 마감이 빠지지 않는다.
  마감 표시끼리 겹친 것은 작업량 안내(⏱)일 뿐 충돌이 아니다. 오늘 마감(달력 밖)은 앞 구역에 없는 것만, 이미 지난 것은 기한 지남에만.
- 기한 지남: 전체 건수, **상세 10건(높음이 더 많으면 15건까지)** + 번호 버튼, 나머지는 **1~3단어 키워드와 짧은 번호**(`RiTA 회신(764)`)를
  분야(project)별로, 글자 상한을 넘으면 "외 N건"과 `기한 지남 전체 보기`. 재확인일 전인 대기·보류는 빼고 건수만.
- 진행 확인: 기한 전·마감 없는 업무 중 오늘 확인할 차례(기존 중요도·주기 규칙), 메시지마다 15건 버튼, 넘치면 키워드와 `진행 확인 전체 보기`.
- 상세 선정(결정론적): 중요도 → 직접 요청 → 내일 마감·재확인일 → 최근에 기한이 지난 것 → **오래 상세로 보이지 않은 것**
  (`item.last_detailed_at`) — 같은 업무만 반복되지 않고 키워드로 줄인 업무가 차례로 올라온다. 버튼으로 보인 업무만 "진행을 물어봄"
  (`last_asked_at`), 키워드는 "보여 줌"(`last_exposed_at`)만.
- 확인·상태: 질문 **최대 3건**(나머지는 계속 추적), Tasks 문서 위치 질문, `완료 반영 N건` 한 줄(완료 200건이어도 한 줄), 승인 대기·
  지난 일정·대기·보류 건수와 보기 버튼, 운영 요약(정상이면 한 줄, 누락 감지·진행 확인 늦음은 건수로 줄임), `전체 목록`.
- **지난 일정은 기본 보고에서 뺀다**(건수만). 시간이 지났다고 참석·완료로 보지 않는다. 미래 정기·단발 일정도 기본 보고에서 빠진다
  (전날·당일에 진행 확인). 누락 감지는 일부러 뺀 것을 누락으로 보지 않는다.
- 전체 보기: `claire_buttons agenda --view overdue|progress|past|waiting|done|questions`. `claire_buttons agenda`(업무 현황)는 "전체 목록"
  요청 때만(0.6.1 형식 그대로). `accounting`으로 모든 활성 업무가 한 구역에만 셈에 들어갔는지 확인한다.
- 키워드 이름: 제안의 `short_label`(Claire 가 1~3단어로), 기존 업무는 `claire_search labels-missing` → `claire_store labels --file`.
  없으면 제목 앞부분. 검색(FTS)에도 들어간다. 키워드의 숫자를 적으면(`764`) 그 업무 카드.
- 브리핑(Claire)은 짧게: 머리글 · 중요한 신규·변경 최대 5줄 · 나머지 건수 · 참고 한 줄. 질문·완료·승인 대기·점검은 아침 보고가 맡는다.

### 2. 답변 대기·보류 재확인
- 모든 대기·보류에 재확인일. 날짜를 말하지 않으면 **기본 3일 뒤(달력)를 바로 적용하고 "언제 다시 확인할까요? 지정하지 않으시면
  10/2(목)에 확인하겠습니다."와 날짜 버튼**(내일·기본·1주 뒤)을 결과에 준다(`ask`·`components`). 보류 기본값 7일 → 3일.
- 마감이 더 빠르면 마감 전날로 당기고, 이미 마감이 지났으면 다음 날. 대기가 마감을 늦추지 않는다. 교수님이 마감 뒤 날짜를 말하면
  그 날짜를 쓰고 안내하며, 마감 전날·당일에는 "마감 임박(대기 중)"으로 올린다(`wait_due`).
- 재확인일 전에는 묻지 않고, 그날부터 답이 없어도 **매일** 진행 확인에 올린다. 완료·영구 숨김 없음. 카드 버튼: 대기 `답 왔어`·`계속 대기`,
  보류 `재개`·`계속 보류`. 계속 대기는 상대(`waiting_on`)·기다리는 내용(`--about`)을 두고 다음 확인일만 새로.
- `claire_store wait --until`(옛 `--next-check`도 됨), 이미 대기 중이면 `--waiting-on` 생략 가능. upkeep 이 재확인일 없는 옛 대기·보류에 날짜를 채운다.

### 3. Obsidian Tasks — 주제 문서는 업무의 자리, Daily 는 하루 활동 기록
- 새 Tasks 줄은 **승인 없이**(`apply.obsidian_add_task: always`) 업무의 정본 노트 → 프로젝트 연결 문서(`note_map`) → 임시 수집 노트
  (`01 Inbox/Claire 업무 수집함.md`, 없으면 만든다)의 `## Tasks`에(섹션이 없으면 문서 끝에). Daily 에 미완료 Tasks 를 만들지 않는다.
  자리는 반영할 때 정한다. 설정 `obsidian.tasks_placement: daily`로 0.6.x 방식(마감일 Daily)으로 되돌릴 수 있다.
- 처음 보는 프로젝트는 볼트(10 Projects·20 Areas·30 Resources)에서 문서 후보를 찾아 두고 아침 보고에서 **한 번** 묻는다
  (`IROS 2026 → 문서` / `IROS 2026 → 수집함` 버튼, 답이 없으면 7일 뒤 한 번 더, 그 뒤로는 묻지 않음). 제목 유사성만으로 고르지 않는다.
  `claire_store note-map --project P --note 문서`로 확인하면 연결을 보존하고, **수집함에 있던 그 프로젝트의 줄을 옮긴다**(`obsidian.move_task`,
  🆔 유지, 수집함 줄 삭제). 업무 하나는 `--item CLR --note`. 교수님 문서·Daily 의 줄은 옮기지 않는다.
- 완료 동기화 양방향: Discord 완료 → 원래 줄 `[x] ✅`, **완료 취소 → 체크 해제**, **완료일 정정 → ✅ 날짜 수정**, 되돌리기도 맞춘다
  (`obsidian.check_task`가 원하는 상태로). Obsidian 에서 **체크 → 완료, 체크 해제 → 다시 열기(Daily 기록 삭제), ✅ 날짜 수정 → 완료일 정정**.
  Claire 가 체크하러 가는 중이면 동기화가 되돌리지 않는다.
- 문서 이동·이름 변경은 🆔 로 연결을 옮긴다(동기화·반영 모두). 같은 🆔 줄이 둘이면 복사본은 연결하지 않고, 반영은 실패로 드러낸다.
- 12:00·18:00 확인 때도 `claire_sync obsidian` → `claire_apply`(Obsidian 직접 체크 반영 하루 세 번).
- 업그레이드: 승인을 기다리던 Tasks 등록은 upkeep 이 자동으로 풀어 위치 규칙대로 쓴다. 기존 Daily Tasks 는 옮기지 않고 체크 동기화만.

### 고친 것
- Obsidian 줄이 **예전에 본 모양으로 돌아가면**(체크를 풀어 원래 줄과 같아짐 등) 이벤트 중복 제거 때문에 반영되지 않던 빈틈 —
  연결된 줄이 마지막으로 본 상태와 다르면 옛 이벤트여도 반영한다.
- `🆔` 줄의 문구가 바뀌면 연결이 둘로 늘던 것 → 옛 줄이 없으면 연결을 옮긴다.

### 스키마 v4
- `item.short_label`, `item.last_detailed_at`, `delivery.mention_refs`, 표 `note_map`. 값은 바꾸지 않는다(열·표 추가만).

### 테스트
- 120건(+15, 이슈 §7 수용 기준): 오늘 일정·마감 중복 없음과 작업+마감 한 줄, 연체 100건 한 메시지(10~15 버튼·나머지 셈·다음 날 돌려 가며),
  진행 확인 두 메시지·각 10건 이상, 완료 200건 한 줄, 지난 일정 제외·미완료 유지, 대기 날짜 질문·3일·매일 재확인·계속 대기·마감 전 재확인·
  마감 뒤 날짜, 옛 대기 재확인일 채우기, 질문 3건, 주제 문서·수집함·위치 질문·옮기기, 양방향 완료·취소·정정, 문서 이동·복사 줄,
  승인 대기 Tasks 전환, 키워드·검색, 보고 중복 방지.
- 기존 테스트 중 0.6.x Tasks 정책(마감일 Daily·Tasks 승인)을 전제로 한 것은 그 설정을 고정해(`legacy_tasks`) 그대로 확인한다.

### 적용 (mac mini)
- `claire-update` 하나면 된다(설치 중 스키마 v4 변환·upkeep). OpenClaw 설정 변경 없음. 다음 06:00 부터 아침 보고 형식으로 온다.
  바로 보려면 Discord 에서 `지금 전체 점검해줘`. 전체가 필요하면 `전체 목록`.

## [0.6.1] — 2026-09-27

0.6.0 설치 직후 Claire 의 전체 점검(9/27 13:20)이 짚은 네 가지를 고쳤다. 스키마 변경 없음(v3).

### 1. 앞으로의 정기 일정: 시리즈마다 한 줄 (교수님 결정)
- 0.6.0 은 7일 뒤의 일정을 건수로만 접었다("활성 580건 중 먼 미래 일정 313건을 숫자로만 접음" — R2 위반). 전부 하나씩 싣는 안(하루 약 24개 메시지)과
  비교해 교수님이 **시리즈마다 한 줄**을 골랐다: `MC2103 수업 · 매주 화·목 09:00 · 다음 9/29(화) 09:00 (CLR-0801) · 앞으로 24회` — 버튼은 다음 회차.
  **단발 일정은 기간과 관계없이 모두 하나씩.** 건수만 있는 줄은 없다. (`tracking.upcoming_meeting_days` 설정은 없앴다.)
- **빠짐 검사**(`coverage`): 활성 업무마다 어느 구역에 한 번, 또는 시리즈 줄에 들어 있는지 도구가 센다(`missing`·`duplicated`). 업무 현황 결과와
  `claire_search agenda`에 싣고, 비어 있지 않으면 본문에 경고. 테스트가 일반 할 일만 보던 빈틈("현재 테스트는 일반 할 일만 검사")을 메운다.
- **촘촘한 배치**: 2~4구역은 다섯 줄마다 번호 버튼 한 줄(메시지당 약 25건, 줄마다 버튼이면 약 11건). 1구역(오늘)은 줄마다 오른쪽 버튼 그대로.
  미리보기 25쪽 → 같은 내용이 대략 절반 이하.
- **밀린 업무의 첫 확인 분산**: 추적 시작(업그레이드) 전부터 있던 업무의 첫 진행 확인을 주기 안에서 번호 순으로 나눈다(보통: 이틀에 절반씩).
  업그레이드 첫날 수백 건이 "오늘 진행 확인"에 한꺼번에 몰리고, 그 뒤에도 같은 날끼리 묶여 반복되던 것을 막는다. 높음(매일)은 그대로 첫날부터.

### 2. 기존 업무 중요도 소급 분류가 적용되지 않음 (Aaron Young 업무가 보통·확인 대상 아님)
- 소급 분류가 매일 점검의 `maintain`에서만 돌아, 설치 뒤 첫 06:00 전까지는 분류 전(보통) 상태였다. 이제 `claire_track.upkeep`이
  **설치(`claire_check init`, install.sh 가 부른다)·`maintain`·업무 현황(`claire_buttons agenda`)** 앞에서 돈다.
- 한 번만 도는 표시를 없앴다: 분류 없는(`priority_source` 없음) 활성 업무만 골라 멱등하게 분류한다(떼어낸 업무·옛 데이터 모두).
- `importance-review`가 분류 전 업무도 포함하고(0.6.0 은 "검토 목록에도 없음"), **회신 요청 → 잠정 높음 → 최근** 순으로 낸다. SKILL: 한 번에 20건.
- `split`(떼어낸 업무)은 원래 업무의 중요도를 이어받는다.

### 3. 일본 출장 보고서·전남고 일정 변경이 여전히 "실제 약속"으로 겹침 경고
- 자동 마감 표시를 `window_auto` 표시로만 알아봤다. 0.5.0 전에 Claire 가 직접 넣은 17–18시 구간, 제안에 구간을 함께 적은 경우, Calendar 동기화의
  문자열 비교가 꺼 버린 경우 모두 "교수님이 잡은 작업 시간"이 되어 충돌 경고가 났다.
  - 구간이 **정확히 마감 N분 전~마감**이면 자동 마감 표시로 본다(`time_kind`, 제안·`update`에서도 `window_auto=1`).
  - `upkeep`이 기존 데이터의 꺼진 표시를 보정한다(이력 남김).
  - Calendar 동기화가 시각을 **순간(instant)** 으로 비교한다: `…T17:00:00+09:00`과 `…T08:00:00Z`처럼 표기만 다르면 옮김으로 보지 않고 변경 이력도 남기지 않는다.
  - 교수님이 다른 구간(예: 16:30–17:45)을 잡으면 작업 예약이라 실제 약속과 겹칠 때 계속 알린다.

### 4. 정기 일정 준비 업무 (템플릿이 비어 있음)
- 설계대로 템플릿 없이 의무를 만들지 않는다. 대신 교수님이 Discord 에서 정할 수 있게 `claire_store prep-template --add "로봇공학" --days-before 1
  --due-time 18:00 [--title "{title} 준비"]` / `--remove` / 목록. 다음 `maintain`부터 14일 안의 회차에 준비 업무가 생긴다.

### 그 밖에
- `claire_store complete --past-meetings [--before YYYY-MM-DD]`: "지난 일정 다 끝났어" — 끝났는데 열린 일정을 한 번에 완료(교수님 말이 있을 때만,
  근거 `user_report`). 밀린 지난 수업·회의가 "지난 일정 — 완료 체크"로 매일 쌓이는 것을 한 번에 정리한다.
- 확인: 이 MacBook 의 `~/Dropbox/ClaireBackup` 파일이 0바이트로 보이는 것은 Dropbox **온라인 전용 자리표시자**다(열면 받는다). 백업 자체의 문제는 아니다.

### 테스트
- 105건(+7): 시리즈 한 줄·50일 뒤 단발 일정도 개별·빠짐 없음, 24건이 한 메시지, 구간을 직접 적은 마감·옛 데이터 보정·Calendar UTC 표기,
  밀린 업무 첫 확인 분산과 누락 오탐 없음, 지난 일정 일괄 완료, 준비 템플릿 명령, 설치 때 소급 분류·검토 순서.
- 바뀐 기존 테스트: "17–18시(18시 마감)"를 작업 예약으로 쓰던 겹침 테스트는 16:30–17:45 로, 쪽 나눔 테스트는 60건으로.

### 적용 (mac mini)
- `claire-update` 하나면 된다(설치 중 `claire_check init`이 소급 분류·마감 표시 보정까지 한다). 그 뒤 Discord 에서 `지금 전체 점검해줘`.
- 밀린 지난 일정이 많으면 `지난 일정 다 끝났어`(또는 어제까지만이면 "어제까지 지난 일정 다 끝났어"). 수업 준비 업무가 필요하면 수업 이름으로
  `로봇공학 수업마다 전날 18시까지 준비 업무 만들어줘`.

## [0.6.0] — 2026-09-27

교수님 개선 요청 반영(`claire-follow-through-issue-2026-09-27.md`, Epic: 일정·할 일의 지속 노출 및 완료까지의 추적 강화).
핵심 원칙: **등록 여부가 아니라, 완료될 때까지 노출·진행 확인이 보장되는지를 관리한다.** 스키마 v3.

계기: 9/25 신규로 보고된 마감 없는 직접 회신 요청(CLR-0764 Aaron Young RiTA 2026 방한 안내)이 장부에 미완료로 있었지만 이후 보고에서
보이지 않아, 9/27 교수님이 "이메일 온 것은 등록되었어? 안 보이네?"라고 따로 물어야 했다.

### P0 — 등록되어 있으나 사라지는 업무 방지
- **업무 현황**(`claire_buttons agenda`, `claire_search agenda`, 새 모듈 `scripts/claire_track.py`): 매일 아침 브리핑 뒤에
  ① 오늘 일정·마감(준비·후속 상태) ② 오늘 진행 확인 ③ 그 외 활성 업무(진행 중·할 일·확인 필요·참고, **기한 없음** 표시, 다음 확인일, 앞으로의 일정)
  ④ 답변 대기·보류(다음 확인일) ⑤ 승인 대기 목록 ⑥ 반영·전달 점검. 활성 미완료 업무는 "외 N건"으로 접지 않고 모두 번호 버튼과 함께 쪽으로 나눈다(R2).
  한 업무는 주된 구역 한 곳에만. 브리핑은 신규·변경·질문·반영 상태·참고만 쓰고 목록을 반복하지 않는다(`briefing-format.md`).
- **중요도와 진행 확인 주기**(R3): Discord 직접 등록 → 높음, 메일에서 교수님을 특정한 요청(`request_scope: direct`) → 높음,
  단체 공통 요청(`group`) → 보통, 단체라도 개인 의무 → Claire 가 근거와 함께 높음. **마감 유무는 보지 않는다.**
  높음 매일 · 보통 2영업일 · 낮음 7일, 전날·당일·기한 경과(정리될 때까지 매일)·보류/대기 재확인일은 주기와 무관.
  같은 날 보고받은 업무는 다시 묻지 않고, 오늘 이미 물은 업무는 그날 내내 오늘의 확인 목록에 남는다(재실행해도 같은 보고).
  교수님이 정한 중요도·주기(`check_every_days`)·다음 확인일(`review_after`)이 우선이고 Claire 가 덮지 못한다(`priority_source=user`).
  분류 근거는 `priority_reason`, 누가 정했는지는 `priority_source`(rule/claire/user/unreviewed).
- **외부 등록 상태와 무관한 추적**(R6): 달력·Tasks 미등록·승인 대기·표시 안 함·반영 실패여도 업무 현황과 진행 확인에 그대로 나온다.
- **전달 기록**(R8, 표 `delivery`): 보고 생성과 전송 성공을 나눈다. 브리핑·업무 현황·승인 대기·12/18시 확인이 쪽마다 `delivery_id`를 갖고,
  `claire_run deliver --status sent|failed`로 결과를 남긴다. **전송 성공한 쪽의 업무만** `last_exposed_at`(보여 줌)·`last_asked_at`(진행 확인함),
  질문 `asked_count`가 갱신된다 — `claire_run brief`는 더 이상 질문 횟수를 세지 않는다. 실패·확인 없는 쪽은 `claire_buttons resend`로
  그 쪽만 다시 보내고, 12·18시 확인이 먼저 재전송한다. 같은 날 같은 내용이면 `duplicate`(다 보냄) 또는 `resend`(안 간 쪽만). 새 보고가 옛 실패 쪽을 대체한다.
- **누락 감시**: 높은 중요도 업무가 30시간(`tracking.exposure_grace_hours`) 넘게 전송 성공한 보고에 실리지 않았거나 진행 확인 주기를 넘기면
  업무 현황 ⑥ "누락 감지"와 `review.tracking.exposure_gaps`에 나온다. `claire_check integrity`에 `delivery_failed`.
- **메시지 검증**: 쪽마다 빈 본문·빈 블록·버튼 없는 섹션·번호 버튼 누락·Discord 한도 초과를 `problems`로 낸다(있으면 본문만 보낸다).

### P1 — 상태·일정 모델 명확화
- **동작의 뜻을 나눔**(R5): 카드 버튼 `완료`·`진행 중`·`보류`·`답변 대기`·`상세` / `처리 불필요`·`내 할 일 아님`·`취소`.
  - `claire_store dismiss --why not_needed|not_mine` — 활성 목록·진행 확인에서 빠지고 사유·이력은 남는다(`status=cancelled` + `close_reason`,
    `find --status all`로 검색, 표시 "처리 불필요"/"내 할 일 아님"). `reopen`·`undo`로 되돌린다. `cancel`은 "업무 자체 취소"(`close_reason=cancelled`).
  - `claire_store hold --until YYYY-MM-DD` — 보류는 재확인일에 다시 묻는다(기본 7일 뒤, `tracking.hold_default_days`). 그 전에는 묻지 않는다.
  - 승인 버튼 `등록 안 함` → **`달력에 표시 안 함` / `Tasks에 표시 안 함`**: 외부 표시만 빼고 업무 추적은 계속. 반영 상태 표기도
    "등록 안 함" → "표시 안 함(교수님 제외)". 옛 `등록 안 함` 버튼·기록은 그대로 동작하고 업무 취소로 해석하지 않는다.
- **일정과 준비·후속 업무**(R1): `claire_store link-task --item <일정> --role prep|followup` — 일정에 연결된 별도 업무(`prep_for`/`follow_up_of`).
  일정 완료가 준비·후속을 완료시키지 않는다. 업무 현황 오늘 일정 줄에 "준비: …(CLR 상태) · 후속: …". 정기 일정 회차의 준비 업무는
  교수님이 승인한 **준비 템플릿**(`prep_templates`, 기본 비어 있음)으로만 `maintain`이 만든다(이미 준비 업무가 있는 회차는 건너뜀, 멱등).
  지난 일정은 자동 완료하지 않고 ② "지난 일정 — 완료 체크"로 묻는다. `complete --item A,B`로 여러 건 완료.
- **마감 표시와 실제 시간 예약 분리**(R7): 항목마다 `time_kind` — `appointment`(회의·수업) · `work`(교수님이 잡은 작업 시간) ·
  `deadline_marker`(마감 1시간 전 자동 표시). 충돌 경고(`overlaps`)는 약속·작업 예약·가족 일정끼리만. 자동 마감 표시끼리 겹친 것은
  `deadline_clusters` → "⏱ 18:00 마감 2건 — 약속 충돌이 아니라 작업량 안내". (9/27 일본 출장 결과 보고서·전남고 강의 일정 변경 확정 사례)
  마감 전 1시간 표시 규칙과 날짜만인 Obsidian 동기화가 확정 시각을 지우지 않는 0.5.0 동작은 그대로.
- **메일 행동 요청은 기본 등록**(R4, SKILL §3): 행동이 필요한 요청은 확신이 약해도 업무로 만든다(교수님이 `처리 불필요`로 뺀다).
  광고·단순 안내·영수증만 `ignore`. 내부 등록은 외부 반영 승인과 별개.

### 전달·표시 실패 수정 (ISSUE §2.4)
- **cron 턴 전송 대상 미지정**: 12·18시 확인·06시 보고는 "지금 대화 중인 채널"이 없어 버튼 전송이 실패했다. config `discord.send_to`
  (`channel:<id>`)를 두고 모든 보고 출력에 `send_to`를 싣는다. `claire_check channel-target [--write]`가 OpenClaw Claire 계정의 채널에서
  찾아 저장한다. doctor `discord.send_to`.
- **승인 대기 후속 메시지가 제목만 보임**: 나뉜 메시지의 `message`(fallback 본문)가 머리글뿐이었다. 쪽마다 그 쪽 항목의 본문 전체를 싣는다.
  (실제 클라이언트 렌더링은 mac mini 에서 확인 필요 — 아래 적용 3)

### 조회·도구
- `claire_search agenda`(업무 현황 재료·점검), `importance-review`(중요도 검토 필요 업무와 받는 사람·발췌).
  조회 결과 항목마다 `status_label`·`time_kind`·`tracking`(중요도·근거·오늘 확인 여부·다음 확인일)·`close_reason` 등.
- `claire_search review`: `overlaps`(실제 충돌만), `deadline_clusters`, `tracking`(건수·누락·전송 문제·검토 필요).
- 항목 카드: 중요도·근거·다음 확인일 한 줄, 승인 대기면 "표시 안 함을 골라도 업무 추적은 계속됩니다".
- `propose`: `request_scope`·`priority_reason` 필드, 경고 `request_scope_missing`·`priority_reason_missing`.

### 스키마 v3 (기존 DB 는 열려 있을 때 자동 변환, 값은 바꾸지 않음)
- `item`: `request_scope`, `priority_source`, `priority_reason`, `check_every_days`, `review_after`, `close_reason`,
  `last_exposed_at`, `last_asked_at`, `last_reported_at`. 표 `delivery`. `meta.tracking_since`.
- 원본 ID·상태·완료 이력·기존 "등록 안 함" 기록 보존, 중복 생성 없음. 기존 활성 업무(마감 없는 것 포함)는 첫 `maintain`이 **한 번** 중요도를
  소급 분류한다: Discord 등록 → 높음, 메일 → 교수님만 받는 사람이면 잠정 높음·아니면 잠정 보통 + "검토 필요"(Claire 가 원문을 보고 확정),
  캘린더·Obsidian → 보통, 이미 높음은 유지, 닫힌 업무는 건드리지 않음.

### 구현 전 미결정 정책(ISSUE §8)에 둔 기본값 — 모두 설정으로 바꿀 수 있다
| 항목 | 기본값 | 설정 |
|---|---|---|
| 보통 중요도 확인 간격·주말 | 2영업일(주말 제외). 높음·전날·당일·기한 경과는 주말 포함 | `tracking.interval_days`, `tracking.skip_weekends_for` |
| 전체 목록 페이지 | Discord 한도(컴포넌트 38·3,800자)로 자동 분할, 구역 순서 고정 | `claire_buttons` 기본값 |
| 보류·대기 중 전날·당일 | 교수님의 명시적 보류·대기가 우선: 진행 질문은 재확인일까지 하지 않는다. 당일이면 ① 오늘 일정·마감에(질문 표시 없이), 그 외에는 ④에 기한과 함께 보인다 | — |
| 준비 템플릿·준비 마감 | 템플릿 없음(자동 생성 안 함). 템플릿은 `days_before`(기본 1)·`due_time` | `prep_templates` |
| 일정 업무의 Tasks 자동 반영 | 하지 않음(회의는 달력만, 기존 승인 규칙 유지) | — |
| 작업 시간 예약 전환 | `update --set start_at --set end_at`(교수님 지시) 또는 달력에서 블록을 옮기면 작업 예약(`work`)으로 보고 충돌 판정에 넣는다 | — |
| 기존 업무 소급 분류 | 규칙으로 잠정값 + "검토 필요" → Claire 가 `importance-review`로 확정, 애매하면 보통으로 두되 목록·확인에서 빠지지 않음 | — |
| 보류 기본 재확인 | 7일 | `tracking.hold_default_days` |
| 누락 판정 | 높음 업무가 30시간 동안 전송 성공 보고에 없음 · 진행 확인이 주기보다 하루 이상 늦음 | `tracking.exposure_grace_hours` |

### 테스트
- 98건(+17). 수용 기준 §6: 마감 없는 직접 요청 매일 확인(Aaron 사례)·Discord 직접 등록 매일·단체 요청 목록은 매일/질문은 2영업일(주말 제외)·
  개인 의무 높음과 근거·전날·당일·기한 경과·30건 숫자 접기 없음·준비/후속 분리와 자동 완료 없음·준비 템플릿·표시 제외 후 추적 유지/처리 불필요 제외와
  이력·되돌리기·보류 재확인일·자동 마감 표시 겹침은 작업량 안내/실제 약속×작업 예약은 충돌·실패 쪽 미기록과 그 쪽만 재전송·재실행 중복 없음·
  누락 감지·지난 일정 자동 완료 없음·소급 분류와 교수님 지정 우선·전송 대상·반복 회차 독립·v2→v3 업그레이드 보존(ID·상태·완료·등록 안 함).
- 바뀐 기존 테스트: 질문 횟수는 전송 성공 뒤(`deliver`), 카드·승인 버튼 이름, 교수님 우선순위 변경의 `priority_source`, v1→v3 변환.

### 적용 (mac mini)
1. `git status`로 Dropbox 동기화 확인 → 커밋·push → `./release.sh tag` → `claire-update`.
2. `claire_check channel-target --write`(Claire 채널을 `discord.send_to`에) → `claire_check doctor --format text`가 `send_to` ok 인지.
3. 수동 점검: `claire_buttons agenda --no-record`로 CLR-0764 가 ②에 "매일 확인 · 기한 없음 · 직접 요청"으로 나오는지(첫 `maintain` 뒤 소급 분류가
   "검토 필요"면 `importance-review`로 direct 확정). 승인 대기 목록이 두 메시지 이상일 때 iPhone 에서 둘째 메시지 본문·버튼이 보이는지
   (§2.4 후자는 원인을 확정하지 않았으므로 실제 클라이언트로 재현 확인).
4. 다음 06:00 보고 뒤 `claire_run deliver --pending`이 비어 있는지, 12:00 확인이 `send_to`로 전송되는지 확인.

### 범위 밖
이메일 발송·참석 수락·불필요 업무 일괄 취소·전체 일정 재등록은 하지 않는다. 중요도·주기 조정 전용 UI(P2 일부)는 채널 지시(`이건 3일마다만 물어봐`)로 대신한다.

## [0.5.1] — 2026-09-25

mac mini 첫 설치(`get.sh`)가 테스트 단계에서 멈춘 문제. 코드 동작 변경 없음.

### 수정
- 테스트 도우미 `db()`가 부를 때마다 SQLite 연결을 새로 열고 닫지 않았다. Python 3.14 는 버린 연결을 늦게 닫아
  macOS 기본 열린 파일 한도(256)에서 `OSError: [Errno 24] Too many open files`로 18~21건이 실패했다(3.11 에서는 재현 안 됨).
  테스트마다 연결 하나를 재사용하고 tearDown 에서 닫는다. Python 3.14 + `ulimit -n 256`/`128`에서 81건 통과 확인.
- `get.sh`: 테스트 하위 셸에서만 열린 파일 한도를 4096 으로 올린다. 로그 파일 이름이 macOS `mktemp -t`에서
  `XXXXXX`가 그대로 남던 것 수정.

## [0.5.0] — 2026-09-25

교수님 개선 요청 정식 반영(ISSUE_20260925). 운영 중 Claire 가 스킬을 직접 고치려다 거부됐던 항목을 도구·규칙으로 편입했다.
설계 핵심: **업무 상태와 외부 도구 반영 상태를 분리**한다. 스키마 v2.

### 1. 마감 시각을 달력에 표시하는 규칙
- 마감 시각만 있으면 마감 1시간 전~마감을 달력에 `[마감] 제목`으로 표시한다(`apply.deadline_window_minutes`, 기본 60). 별도 시작·기간 지시가 우선.
  마감 시각(`due_at`)과 달력 표시 구간(`start_at`·`end_at`)을 별도 필드로 관리하고 `item.due_precision`(date/time)·`window_auto`로 구분한다.
- **버그 수정: 동기화가 마감 시각을 23:59 로 덮던 문제.** Obsidian Tasks 줄(날짜만)의 미러링이 확정된 시각을 덮지 않는다 — 같은 날이면 그대로, 날짜가 바뀌면 날짜만 옮기고 시각 유지(달력 블록도 outbox 로 이동).
  `update --set due_at=YYYY-MM-DD`도 확정 시각을 지우지 않는다(지우려면 `due_precision=date`).
- 날짜만 있으면 확인 질문: `unknown_fields: ["due_time"]` 질문 형식 추가, 답하면 구간 생성·달력 예약. `deadline`이 날짜만이면 propose 결과 `warnings.due_time_missing`.
- 교수님이 달력에서 블록을 옮기면 구간만 따라오고(자동 계산 중지) 마감·제목은 그대로(`[마감] ` 접두어 제거).
- Tasks 줄에 `(15:00 마감)` 표기.

### 2. 승인 대기 조회·승인과 아침 보고
- `claire_apply --approvals` / `claire_buttons approvals`: 달력 등록 대기와 Tasks 등록 대기를 나눈 전체 목록(제목·일시·업무 상태·참석). 아침 브리핑 직후 별도 메시지로 보낸다(SKILL §2 8단계, §5.1).
- 지난 일정은 목록에서 빼고 건수만 보인다. 기록은 지우거나 완료·취소하지 않는다(`--include-past`로 조회).
- 개별 승인(`CLR 달력 등록`/`Tasks 등록`), **선택 일괄 승인**(`선택` → `선택한 것 등록`, `outbox.selected_at`), `전부 등록`(보이는 것만), `등록 안 함`(declined 로 표시).
  `--confirm`이 여러 번호·`selected`·`--only calendar|tasks`를 받는다. `--request CLR`: 교수님이 "달력에 넣어줘" 한 항목을 바로 예약(등록 안 함 이후에도).
- 업무 상태 ↔ 반영 상태 분리: `claire_ops.sync_state_of`·`describe_state` → 조회 결과마다 `sync`·`state_line`(`업무 할 일 · 달력 승인 대기 · 참석 미정`). `item.attendance`(Calendar 참석 응답에서 채움).

### 3. 갑작스러운 일정·구두 약속 수집
- `claire_run checkin --slot 12:00|18:00`: 하루·슬롯 1회 확인 메시지(오늘 등록분 표시, `미등록 일정 없음` 버튼), `checkin-mark`로 결과 기록. 표 `checkin`.
- 답변 처리: `ingest-discord --intent register --checkin …`(접두어 없이 지시로 저장), `claire_search dupcheck --title --date`로 기존 기록과 중복 확인 후 등록, 부족한 정보는 질문(SKILL §2.1).
- doctor `openclaw.cron_checkin`(`0 12,18 * * *`, declaration key `claire-checkin`)과 등록 명령(`references/doctor.md`).

### 4. 모바일 버튼 UI
- 항목 카드 버튼: `완료`·`진행 중`·`보류`·`취소`·`상세`(+ `등록`/`등록 안 함` 또는 `달력 등록`). 카드에 업무 상태와 반영 상태를 따로 적는다. `actions --detail`(출처·완료 기준·최근 이력).
- 버튼 만료(24h, OpenClaw 최대) 뒤: 짧은 번호 입력(`31 완료`, `clr31`, `q7`)을 도구가 정식 번호로 읽는다(`claire_core.normalize_ref`). "버튼 다시"로 새 버튼. 옛 `끝냈어` 버튼도 계속 동작.

### 5. 완료 기록의 Obsidian Daily 연동
- 완료하면 **실제 완료일** Daily 의 `## 완료한 일`에 `- ✅ HH:MM 업무명 (CLR-0031) · [[관련 노트]]` 한 줄(`%%claire-done:CLR%%` 표식). 별도 완료일을 말하면 그 날짜(시각 없이).
- **기록 시점 결정: 완료 즉시(outbox, `claire_store complete --apply`) + 매일 점검에서 누락 대조.** 자정 일괄보다 나은 이유 — 완료 직후 Daily 에 바로 보이고, 다른 Obsidian 쓰기와 같은 재시도·미반영 표시 경로를 쓰며, mac mini 가 자정에 잠들어도 기록이 빠지지 않고, 완료일 정정·취소는 어차피 기록 수정이 필요하다.
- 중복 없음(버튼 여러 번), 완료일 정정은 줄 이동, 완료 취소·되돌리기는 줄 삭제. 저장 실패는 백오프 재시도, `review.daily_unsynced`·`state_line`에 "Daily 재시도 중/실패". Obsidian 에서 직접 체크한 완료도 기록.
- 도입 전(`meta.daily_done_since`)에 기록된 완료는 옛 Daily 에 소급하지 않는다.

### 버전 관리·설치
- `get.sh`: `curl … | bash` 한 줄 설치·업그레이드. Dropbox 밖 설치 전용 클론, 최신 릴리스 태그(또는 `--version`/`--main`), 설치 전 테스트, `--check`.
- `install.sh`: `claire-update` 명령 설치(`~/.local/bin`), `INSTALL_INFO.json`(버전·태그·커밋), 버전이 바뀌면 스키마 변환 전 DB 사본(`backups/pre-upgrade-*`), Python 3.11 확인.
- `VERSION` 파일, `release.sh bump|check|tag`, GitHub Actions(ci: ubuntu·macOS × 3.11·3.14 테스트·설치, release: 태그 → Release 노트).
- `claire_check version [--remote]`. 스키마 v1→v2 자동 변환(`claire_core.migrate`, 열 추가만·멱등).

### 수정
- `claire_export md`: f-string 안 역슬래시로 Python 3.11 에서 SyntaxError → 백업(`claire_check backup`, `claire_run end --backup`)이 3.11 에서 실패하던 문제.

### 테스트
- 81건(+16): 마감 구간·명시 우선·날짜만 질문·Obsidian 시각 보존·달력 드래그, 승인 목록·선택·등록 안 함·지난 일정·버튼 메시지·요청, 미등록 확인·중복 확인, 짧은 번호·상세 카드,
  Daily 기록·중복·정정·취소·되돌리기·실패 재시도·대조·Tasks 체크, v1→v2 변환, install.sh(기록·사본)·get.sh(태그 설치·고정·확인·거부).

### 적용 (mac mini)
1. 커밋·push 후 `./release.sh tag` → 처음 한 번 `curl … get.sh | bash` (이후 `claire-update`).
2. `claire_check doctor --format text` → `cron_checkin` 안내대로 Claire 가 만든 임시 12·18시 자동화를 지우고 `claire-checkin` cron 등록.
3. iPhone 에서 확인: 06:00 브리핑 뒤 승인 대기 목록 버튼, 12·18시 확인 메시지, `완료` 버튼 뒤 Daily `## 완료한 일`.

## [0.4.5] — 2026-09-16

번호 버튼이 며칠 만에 다시 사라지고 텍스트(코드 블록)로만 오던 퇴행(교수님 피드백: "어제는 버튼이 왔는데 오늘은 예전처럼 보인다").

### 원인
- OpenClaw(2026.9.2) `buildGroupChatContext`가 그룹 채널 **매 턴** 시스템 프롬프트에 "For ordinary text, do not use the message tool … unless the current-turn context asks for visible output via message(action=send). Use message(action=send) only when you need to send files, images, or other attachments"를 넣는다. SKILL §5(버튼은 message 도구로)와 매 턴 충돌해 모델이 어느 쪽을 따르느냐가 달라졌다(버튼 전송 9/13 7건 → 9/14 17건 → 9/15 2건 → 9/16 0건). 스킬 문구만으로는 못 고친다.
- 그 문장이 열어 둔 예외("current-turn context 가 요구하면")를 선언하는 정식 자리는 Discord **채널별 `systemPrompt`** 설정이다. Discord 플러그인이 이를 `GroupSystemPrompt`로 넘기고 OpenClaw 가 그룹 지침 바로 뒤 같은 블록에 붙인다. dist 수정·재빌드 불필요, 업그레이드에도 유지.

### 추가
- `references/discord-channel-prompt.txt` — 채널 systemPrompt 원문(단일 출처).
- `claire_check channel-prompt [--guild --channel]` — 위 원문으로 `openclaw config patch --stdin` 에 넣을 패치 JSON 출력. guild·channel 은 OpenClaw 설정에서 자동 탐지. 설정을 직접 바꾸지는 않는다(교수님이 실행).
- doctor `openclaw.channel_prompt` — Claire 채널 systemPrompt 가 원문과 같으면 ok, 문구는 있으나 다르면 warn(구버전), 없으면 warn + fix 명령.
- `references/doctor.md` — OpenClaw 설정 3건 표와 채널 systemPrompt 절(원인·명령·확인법).
- SKILL §5 — 이 채널에서는 그룹 지침의 제한이 버튼 응답에 적용되지 않는다는 한 줄.
- 테스트 3건(channel-prompt 패치 형태, 상태 판정 ok/구버전/없음, 채널 탐지).

### 적용(9/16)
- 교수님이 채널 systemPrompt 를 `openclaw config patch --stdin` 으로 넣고 `openclaw gateway restart`. iPhone 에서 버튼 재확인됨.

## [0.4.4] — 2026-09-13

번호 버튼이 실제로는 한 번도 전송되지 않던 문제(교수님 피드백: iPhone에서 번호를 눌러도 아무 일도 없음).

### 원인
- Claire 에이전트(`openai/gpt-6-astra`, Codex 하네스)에서 OpenClaw `message` 도구는 초기 도구 목록에 들어가지 않고 `openclaw` 네임스페이스의 **지연 로딩(searchable) 도구**로만 제공된다. SKILL §5의 "message 도구가 도구 목록에 없으면 본문을 그대로 답한다"는 fallback 규칙이 매번 발동해 `claire_buttons build`·message 도구 호출을 건너뛰었다(9/13 06:00 브리핑·12:22 `등록:` 응답 모두 코드 블록만 전송, `message_tool_run_outcomes` 0건). tool search로 `message`를 찾아 로드한 뒤 호출하면 정상 전송됨을 실측했다(메시지 1548540630399983698).

### 변경
- SKILL §5 대상 확대: 조회 결과(`today`·`week`·`overdue`·`waiting`)의 항목 줄에도 번호 버튼을 붙인다(교수님 요청: 오늘 할 일을 처리한 직후 바로 완료하고 싶음). 긴 목록은 8줄 단위로 나뉜다. 번호 없는 짧은 대화만 예외.
- SKILL §2 8단계·§5: message 도구는 **tool search로 찾아 로드한 뒤** 호출한다. 검색해도 없거나 전송이 실패할 때만 본문 그대로(사유 한 줄 첨부).
- cron `Claire 일일 점검` 문구: "브리핑 본문은 채널에 그대로 전송" → 8단계(번호 버튼, 보낸 뒤 `NO_REPLY`)대로.
- doctor `openclaw.message_tool` 안내에 지연 로딩 사실 표기.
- 코드 변경 없음(버전 문자열만).

## [0.4.3] — 2026-09-13

Discord **번호 버튼**(교수님 피드백: iPhone·iPad Discord에는 코드 블록 복사 버튼이 없어 0.4.2 방식이 모바일에서 동작하지 않음).

### 추가
- `scripts/claire_buttons` — `build --file 본문.md`: 본문의 번호 복사 블록(```CLR-0031```)이 있던 줄을 "줄 + 오른쪽 번호 버튼"(Discord Components V2 section)으로 바꾼 message 도구 `components` 페이로드를 만든다. 블록이 없는 본문은 번호가 처음 나온 줄마다 버튼. Discord 한도(컴포넌트 40개·4,000자)에 맞춰 여러 메시지로 나누고 fallback 본문을 함께 준다. `actions --item CLR | --question Q`: 버튼을 눌렀을 때 보낼 카드(한 줄 요약 + `끝냈어`·`진행 중`·`보류`·`취소`·`재개`·`다시 열어`·`등록`/`등록 안 함`·질문 후보 답 버튼).
- SKILL §4: 버튼 입력 처리 — OpenClaw가 넣어 주는 `Clicked "라벨".`을 번호만이면 카드 응답, 지시가 있으면 그 문장을 타이핑한 것으로 처리. §5: 보내는 법(message 도구, 보낸 뒤 `NO_REPLY`), fallback(도구 없음·실패 시 본문 그대로), 24시간 만료 안내. §2 8단계.
- doctor: `openclaw.message_tool`(Claire 에이전트에 message 도구 허용 여부), `openclaw.button_ttl`(버튼 콜백 수명 ≥ 12시간) 점검과 고치는 명령.
- 테스트 5건(build 블록 모드·번호 모드·분할·fallback, actions 카드).

### 변경
- 번호 복사 블록 규칙은 그대로 두되 역할이 바뀐다: 버튼 위치 표시 + 버튼을 못 쓸 때의 fallback. 브리핑 형식·매뉴얼·활용 안내 갱신.
- OpenClaw 설정 2건이 필요하다(install.sh가 건드리지 않음, doctor가 안내): `agents.entries.claire.tools.alsoAllow: ["message"]`, `channels.discord.accounts.claire.agentComponents.ttlMs: 86400000`.

## [0.4.2] — 2026-09-12

번호 복사 블록의 위치 조정(교수님 피드백). 코드 변경 없음.

### 변경
- 복사 블록을 메시지 끝에 모으지 않고 **번호가 나온 줄 바로 다음 줄**에 붙인다(SKILL §5). 항목과 복사 버튼이 멀어 번호를 외워야 했던 문제 해결.
  한 줄에 번호가 둘이면 순서대로 둘, 같은 번호는 처음 나온 자리에만, 부가 언급(괄호 안 참고·겹침 상대·반영 상태 줄)에는 붙이지 않는다. "한 메시지 5개" 상한 삭제.
- 브리핑: "바로 답할 번호" 꼬리를 없애고 오늘 일정·우선 처리·신규·변경·진행 확인·확인 필요의 각 줄 아래에 블록을 둔다(`references/briefing-format.md` 예시 갱신).
- 번호 옆에 작은 복사 아이콘을 두는 방식은 불가(Discord 인라인 코드에는 복사 버튼이 없고 코드 블록은 한 줄을 차지함). 문서에 명시.

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
