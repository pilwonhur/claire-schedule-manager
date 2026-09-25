# 제안 JSON 규격 (`claire_store propose --file`) — PRD §7.1

Claire는 자유 텍스트로 DB를 바꾸지 않는다. 해석 결과를 이 형식으로 파일에 쓰고 도구에 넘긴다.
도구가 검증·트랜잭션·번호 발급을 하며, 규칙에 어긋나면 **전체를 거부**한다(부분 저장 없음).

```json
{
  "run_id": 143,
  "proposals": [
    {
      "op": "create",
      "source_event_ids": [981],
      "kind": "reply",
      "title": "IROS 워크숍 발표자 명단 회신",
      "project": "IROS 2026 워크숍",
      "due_at": "2026-09-12",
      "done_criteria": "명단 회신 발송",
      "confidence": 0.86,
      "evidence": ["Could you send the final speaker list by Friday, Sept 12?"],
      "unknown_fields": [],
      "questions": []
    },
    {
      "op": "create",
      "source_event_ids": [990],
      "kind": "meeting",
      "title": "○○ 교수 면담 (카카오톡)",
      "confidence": 0.55,
      "evidence": ["이번 금요일 오후에 잠깐 뵐 수 있을까요?"],
      "unknown_fields": ["start_at", "scope"],
      "questions": [
        {"field": "start_at", "question": "금요일 오후 몇 시인가요?", "options": ["14:00","15:00","16:00"],
         "reason": "대화에 시각 없음. 금요일 14–17시는 캘린더가 비어 있음"},
        {"field": "scope", "question": "참석만 하시면 되나요, 준비할 자료가 있나요?", "reason": "면담 목적이 대화에 없음"}
      ]
    },
    {"op": "ignore", "source_event_ids": [983], "reason": "notice", "note": "학과 공지, 요청 없음"}
  ]
}
```

## op 별 필드

| op | 필수 | 선택 | 2단계 |
|---|---|---|---|
| `create` | `source_event_ids`, `kind`, `title`, `evidence`(1개 이상) | `project`, `priority`(high/normal/low), `owner`(user/claire/other), `next_action`, `done_criteria`, `start_at`, `end_at`, `all_day`, `due_at`, `scheduled_on`, `tentative`, `confidence`(0~1), `waiting_on`, `next_check_at`, `canonical_note`, `ledger_note`, `series_key`, `occurrence_at`, `attendance`(undecided/attending/declined), `unknown_fields`, `questions`, `note` | ✅ |
| `ignore` | `source_event_ids`, `reason` | `note` | ✅ |
| `update` | `item_ref`, `source_event_ids`, `changes`(필드→값), `evidence` | `reason` | ✅ 같은 스레드·시리즈·연결 원문만. 아니면 `--force-cross-thread` |
| `complete` | `item_ref`, `source_event_ids`, `evidence` | `reason`, `evidence_code`(기본 `criteria_evidence:<첫 source_event_id>`) | ✅ 보낸 메일·제출 확인 등 완료 증거 |
| `cancel` | `item_ref`, `source_event_ids`, `evidence` | `reason` | ✅ 취소 통지 원문 |
| `relate` | `from`, `to`, `type`(prep_for/subtask_of/blocks/follow_up_of/related/same_thread) | — | ✅ 원문 불필요 |

## 검증 규칙 (도구가 강제)

1. 모든 `source_event_ids`는 존재하고 `triage='new'`여야 한다. 하나라도 아니면 전체 거부.
2. `kind`별 필수 시간 필드(§5.4): `meeting`→`start_at`, `deadline`→`due_at`, `reply`→`due_at` 또는 `next_check_at`,
   `prep`→`due_at`, `waiting`→`waiting_on`+`next_check_at`. 모르면 **`unknown_fields`에 넣고 질문을 붙인다.**
3. `unknown_fields`의 각 필드마다 `questions`에 같은 `field`의 질문이 있어야 한다. 모른다고 하면서 값을 넣으면 거부(추측 금지).
4. 시각은 ISO8601 오프셋 포함(`2026-09-15T10:00:00+09:00`) 또는 날짜(`2026-09-12`). 날짜만이면 `due_at`은 `T23:59:59+09:00`, `start_at`은 `T00:00:00+09:00`로 정규화된다. 오프셋 없는 시각은 거부.
5. `confidence < 0.4`(config `min_confidence`)면 `captured`로만 저장되고 질문은 만들지 않는다. 브리핑 "참고"에 한 번 보인다.
6. `unknown_fields`가 있으면 `needs_info` + `Q-` 번호. 없으면 `todo`.
7. Calendar·Obsidian 원문으로 만든 항목은 자동으로 `link`가 생긴다. 이미 연결된 원문으로 다시 `create`하면 거부.
8. 같은 원문(`source_event_id`)으로 여러 항목을 만들 수 있다(메일 1통에 요청 3개, TC3). 단 Calendar·Obsidian 원문은 항목 하나만.
9. 한 원문을 `ignore`와 `create`에 동시에 넣을 수 없다.
10. **마감 시각 규칙(0.5.0)**: 회의가 아닌 항목에 시각 있는 `due_at`만 주면 도구가 `start_at`·`end_at`을 마감 1시간 전~마감(`apply.deadline_window_minutes`)으로 채운다(달력 표시 구간, `window_auto`).
    `start_at`을 주면 그 값이 우선이고 `end_at`이 없으면 마감까지. 날짜만인 `due_at`은 `due_precision=date`로 저장되고 달력에 올리지 않는다.
    날짜는 알고 시각을 모르면 `unknown_fields: ["due_time"]` + `field: "due_time"` 질문(`due_at`에 날짜를 넣어도 된다). 시각이 이미 있는데 `due_time`을 물으면 거부.
    `kind: deadline`인데 날짜만이고 `due_time` 질문도 없으면 반환 `warnings`에 `due_time_missing`이 붙는다(거부는 아님).
11. `update`의 `changes` 값은 `claire_store update`와 같은 형식 검증을 받는다. `status`는 바꿀 수 없다(전이는 `complete`/`cancel`로).
    Calendar 연결 항목의 `start_at`/`end_at`은 Calendar가 원본이라 거부된다(4단계 outbox).

## 반환

```json
{"created": [{"ref": "CLR-0045", "status": "todo", ...}], "captured": [...],
 "questions": [{"ref": "Q-0007", "item_ref": "CLR-0031", "question": "..."}],
 "ignored": [...], "summary": {...}, "message": "... 이제 번호를 말해도 됩니다."}
```

`created[]`에는 `due_precision`, `start_at`·`end_at`, `calendar_window: "auto"`(자동 구간일 때), `planned`(예약된 외부 반영)가 실린다.

**이 반환 전에는 번호를 말하지 않는다.** `ok: false`면 오류 메시지를 고쳐 다시 제안하거나, 고칠 수 없으면
브리핑에 "해석 미반영 N건"으로 적는다.

## ignore reason 어휘

`notice`(공지·안내), `promotion`, `newsletter`, `receipt`(영수증·자동 알림), `not_for_user`(요청 대상이 다른 사람),
`duplicate`(이미 항목 있음 — 같은 스레드면 3단계 `update`가 맞다), `sent_by_user`(교수님이 보낸 메일, 근거만),
`overlay`(겹침 확인용 캘린더), `no_action`(정보만 있고 할 일 없음), `reference`.
