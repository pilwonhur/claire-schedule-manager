"""claire_ops — 항목·질문 변경의 결정론적 규칙 (PRD §5.3, §7.3, §7.4).

claire_store 의 개별 명령과 propose 의 update/complete/cancel 이 모두 이 함수를 쓴다.
모든 함수는 열린 트랜잭션 안에서 호출되며, 스스로 커밋하지 않는다. 이력(activity_log)에는
되돌리기용 이전 상태(payload)를 남긴다.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from claire_core import (
    ClaireError, ITEM_STATUSES, OPEN_STATUSES, PRIORITIES, OWNERS, REL_TYPES, LINK_SYSTEMS,
    log_activity, meta_get, reindex_item, tz_of, validate_evidence, parse_iso,
)

ISO_DT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?([+-]\d{2}:\d{2}|Z)$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 교수님·Claire 가 update 로 바꿀 수 있는 필드. status 는 전이 함수로만 바꾼다.
UPDATABLE = {
    "title": "text", "project": "text", "priority": "priority", "owner": "owner", "next_action": "text",
    "done_criteria": "text", "start_at": "dt", "end_at": "dt", "all_day": "bool", "due_at": "dt_eod",
    "scheduled_on": "date", "tentative": "bool", "waiting_on": "text", "next_check_at": "dt_eod",
    "blocked_reason": "text", "canonical_note": "text", "ledger_note": "text", "kind": "kind",
    "due_precision": "precision", "attendance": "attendance", "window_auto": "bool",
}
ATTENDANCE = ["undecided", "attending", "declined"]
# Calendar 참석 응답 → 항목 attendance (업무 상태와 별개로 브리핑에 보인다)
RESPONSE_TO_ATTENDANCE = {"needsAction": "undecided", "tentative": "undecided", "accepted": "attending",
                          "declined": "declined"}
# 시각이 원본인 필드: Calendar 연결 항목에서는 DB 쪽에서 바꾸지 않는다 (§9 필드별 원본 우선)
CALENDAR_OWNED = {"start_at", "end_at", "all_day"}

TRANSITIONS = {
    # from: allowed targets (§5.3)
    "captured": {"todo", "needs_info", "in_progress", "cancelled", "waiting", "on_hold"},
    "needs_info": {"todo", "in_progress", "cancelled", "waiting", "on_hold"},
    "todo": {"in_progress", "waiting", "on_hold", "cancelled", "done"},
    "in_progress": {"todo", "waiting", "on_hold", "cancelled", "done"},
    "waiting": {"todo", "in_progress", "on_hold", "cancelled", "done"},
    "on_hold": {"todo", "in_progress", "waiting", "cancelled", "done"},
    "done": {"todo"},          # reopen
    "cancelled": {"todo"},     # reopen
}


def _bad(code, msg, **extra):
    raise ClaireError(code, msg, **extra)


def norm_value(field: str, value, cfg):
    """update 값의 형식 검증·정규화. 잘못된 값은 거부한다."""
    kind = UPDATABLE.get(field)
    if kind is None:
        _bad("bad_field", f"바꿀 수 없는 필드입니다: {field}", allowed=sorted(UPDATABLE))
    if value in (None, "", "null"):
        return None
    if kind == "text":
        return str(value).strip() or None
    if kind == "priority":
        if value not in PRIORITIES:
            _bad("bad_value", f"priority 는 {PRIORITIES} 중 하나", value=value)
        return value
    if kind == "owner":
        if value not in OWNERS:
            _bad("bad_value", f"owner 는 {OWNERS} 중 하나", value=value)
        return value
    if kind == "precision":
        if value not in ("date", "time"):
            _bad("bad_value", "due_precision 은 date | time", value=value)
        return value
    if kind == "attendance":
        if value not in ATTENDANCE:
            _bad("bad_value", f"attendance 는 {ATTENDANCE} 중 하나", value=value)
        return value
    if kind == "kind":
        from claire_core import ITEM_KINDS
        if value not in ITEM_KINDS:
            _bad("bad_value", f"kind 는 {ITEM_KINDS} 중 하나", value=value)
        return value
    if kind == "bool":
        if isinstance(value, bool):
            return 1 if value else 0
        if str(value).lower() in ("1", "true", "yes", "y"):
            return 1
        if str(value).lower() in ("0", "false", "no", "n"):
            return 0
        _bad("bad_value", f"{field} 는 true/false", value=value)
    if kind == "date":
        if not ISO_DATE.match(str(value)):
            _bad("bad_value", f"{field} 는 YYYY-MM-DD", value=value)
        return str(value)
    if kind in ("dt", "dt_eod"):
        v = str(value)
        if ISO_DATE.match(v):
            off = datetime.now(tz_of(cfg)).strftime("%z"); off = off[:3] + ":" + off[3:]
            return f"{v}T23:59:59{off}" if kind == "dt_eod" else f"{v}T00:00:00{off}"
        if ISO_DT.match(v):
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(microsecond=0).isoformat()
            except ValueError:
                pass
        _bad("bad_value", f"{field} 는 ISO8601(오프셋 포함) 또는 YYYY-MM-DD", value=value)
    return value


def item_snapshot(item) -> dict:
    return {k: item[k] for k in item.keys()}


def precision_of(raw) -> str | None:
    """마감 입력값의 정밀도: 날짜만(YYYY-MM-DD)이면 date, 시각이 있으면 time."""
    if raw in (None, "", "null"):
        return None
    return "date" if ISO_DATE.match(str(raw)) else "time"


def with_time_of(day_value: str, time_source: str) -> str:
    """`2026-09-26` + 기존 마감 `…T15:00:00+09:00` → `2026-09-26T15:00:00+09:00` (확정된 시각 보존)."""
    return f"{str(day_value)[:10]}{time_source[10:]}"


def deadline_window(cfg, due_at: str) -> tuple[str, str]:
    """마감 시각만 지정된 항목의 달력 표시 구간: 마감 N분 전 ~ 마감 (기본 60분)."""
    minutes = int(cfg.get("apply", {}).get("deadline_window_minutes", 60))
    end = parse_iso(due_at)
    return (end - timedelta(minutes=minutes)).isoformat(), end.isoformat()


def wants_window(item_like) -> bool:
    """달력 표시 구간을 마감에서 만드는 항목인가: 회의가 아니고 마감 시각이 확정됨."""
    return (item_like["kind"] != "meeting" and item_like["due_at"] and item_like["due_precision"] == "time")


def apply_update(conn, cfg, item, changes: dict, *, actor: str, ts: str, reason: str | None,
                 evidence_sid: int | None = None, run_id=None, allow_calendar_owned: bool = False) -> list[dict]:
    """필드 변경. 필드마다 이력 1건. Calendar 연결 항목의 시각 필드는 거부한다(교수님 지시는 outbox 로).

    0.5.0 마감 규칙:
    - 날짜만 온 마감(`due_at=2026-09-26`)은 이미 확정된 시각을 지우지 않는다(날짜만 옮기고 시각 보존).
      시각을 없애려면 `due_precision=date` 를 함께 준다.
    - 마감 시각이 확정되면 달력 표시 구간(start_at·end_at)을 마감 N분 전~마감으로 만든다(window_auto=1).
      교수님이 start_at·end_at 을 따로 지정하면 그 값이 우선이고 window_auto=0.
    """
    if item["deleted_at"]:
        _bad("item_deleted", f"삭제된 항목입니다: {item['ref']}")
    if item["status"] in ("done", "cancelled") and not allow_calendar_owned:
        _bad("item_closed", f"{item['ref']} 는 {item['status']} 상태입니다. 먼저 reopen 하세요")
    cal_linked = conn.execute("SELECT 1 FROM link WHERE item_id=? AND system='calendar' AND sync_status<>'missing'",
                              (item["id"],)).fetchone() is not None
    changes = dict(changes)
    derived: set[str] = set()                # 마감에서 계산한 달력 구간 (Claire 의 판단이 아니라 규칙)
    if "due_at" in changes:
        raw = changes["due_at"]
        prec = changes.get("due_precision") or precision_of(raw)
        if prec == "date" and "due_precision" not in changes and item["due_precision"] == "time" and item["due_at"]:
            norm_value("due_at", raw, cfg)                     # 형식 검증
            changes["due_at"] = with_time_of(raw, item["due_at"])
            prec = "time"
        changes.setdefault("due_precision", prec)
    explicit_window = any(k in changes for k in ("start_at", "end_at"))
    if explicit_window and item["kind"] != "meeting" and "window_auto" not in changes:
        changes["window_auto"] = 0
    # 마감이 바뀌었거나 시각이 새로 확정되면 달력 구간을 다시 만든다
    if not explicit_window and item["kind"] != "meeting" and ("due_at" in changes or "due_precision" in changes):
        new_due = norm_value("due_at", changes["due_at"], cfg) if "due_at" in changes else item["due_at"]
        new_prec = changes.get("due_precision", item["due_precision"])
        if new_due and new_prec == "time" and (item["window_auto"] or not item["start_at"]):
            ws, we = deadline_window(cfg, new_due)
            changes.update({"start_at": ws, "end_at": we, "window_auto": 1})
            derived.update({"start_at", "end_at"})
        elif new_prec == "date" and item["window_auto"] and not cal_linked:
            changes.update({"start_at": None, "end_at": None, "window_auto": 0})
    applied = []
    deferred = {}
    for field, raw in changes.items():
        value = norm_value(field, raw, cfg)
        if field in CALENDAR_OWNED and cal_linked and not allow_calendar_owned:
            if actor != "user" and field not in derived:
                _bad("calendar_owned_field",
                     f"{item['ref']} 의 {field} 는 Calendar 가 원본입니다. Claire 가 직접 바꿀 수 없습니다 (§9)",
                     field=field)
            deferred[field] = value          # Calendar 에 반영 후 동기화로 돌아온다
            continue
        if item[field] == value:
            continue
        log_activity(conn, item_id=item["id"], action="update", actor=actor, ts=ts, field=field,
                     old_value=item[field], new_value=value, reason=reason, evidence_source_event_id=evidence_sid,
                     run_id=run_id, payload={"before": {field: item[field]}})
        conn.execute(f"UPDATE item SET {field}=?, updated_at=? WHERE id=?", (value, ts, item["id"]))
        applied.append({"field": field, "old": item[field], "new": value})
    if deferred:
        link = conn.execute("SELECT * FROM link WHERE item_id=? AND system='calendar' AND sync_status<>'missing' "
                            "ORDER BY id LIMIT 1", (item["id"],)).fetchone()
        cal_id, event_id = link["external_key"].split(":", 1)
        # 같은 값의 지시는 한 번만, 다른 값이면 새 op (이전 대기 op 는 취소)
        sig = ",".join(f"{k}={v}" for k, v in sorted(deferred.items()))
        key = f"calendar.update:{item['ref']}:{sig}"
        if conn.execute("SELECT 1 FROM outbox WHERE dedupe_key=? AND status IN ('pending','awaiting_confirm','done')", (key,)).fetchone():
            applied.append({"field": ",".join(sorted(deferred)), "deferred": "calendar.update", "values": deferred, "note": "이미 예약됨"})
        else:
            conn.execute("UPDATE outbox SET status='cancelled', done_at=? WHERE item_id=? AND op='calendar.update' "
                         "AND status IN ('pending','awaiting_confirm')", (ts, item["id"]))
            _enqueue(conn, "calendar.update", item["id"],
                     {"calendar_id": cal_id, "event_id": event_id, "fields": deferred, "ref": item["ref"], "reason": reason},
                     key, ts, requires_confirm=0)
            conn.execute("UPDATE link SET sync_status='pending' WHERE id=?", (link["id"],))
            log_activity(conn, item_id=item["id"], action="apply", actor=actor, ts=ts, field="calendar.update",
                         new_value=json.dumps(deferred, ensure_ascii=False), reason=reason, run_id=run_id)
            applied.append({"field": ",".join(sorted(deferred)), "deferred": "calendar.update", "values": deferred})
    if applied:
        reindex_item(conn, item["id"])
        refresh_pending_create(conn, cfg, item["id"])
    if evidence_sid:
        conn.execute("INSERT OR IGNORE INTO item_source(item_id,source_event_id,role) VALUES(?,?,'update')",
                     (item["id"], evidence_sid))
    return applied


def set_status(conn, cfg, item, new_status: str, *, actor: str, ts: str, reason: str | None,
               action: str | None = None, evidence_sid: int | None = None, run_id=None,
               extra_fields: dict | None = None) -> dict:
    """상태 전이 (§5.3). done 은 complete_item 으로만 간다."""
    if new_status not in ITEM_STATUSES:
        _bad("bad_status", f"상태 값이 잘못되었습니다: {new_status}")
    if new_status == "done" and action != "complete":
        _bad("use_complete", "done 전이는 complete 로만 할 수 있습니다 (근거 코드 필수)")
    cur = item["status"]
    if new_status == cur:
        return {"changed": False, "status": cur}
    if new_status not in TRANSITIONS.get(cur, set()):
        _bad("bad_transition", f"{item['ref']}: {cur} → {new_status} 전이는 허용되지 않습니다",
             allowed=sorted(TRANSITIONS.get(cur, set())))
    payload = {"before": {"status": cur, "completed_at": item["completed_at"], "cancelled_at": item["cancelled_at"],
                          "waiting_on": item["waiting_on"], "next_check_at": item["next_check_at"],
                          "blocked_reason": item["blocked_reason"]}}
    sets = {"status": new_status}
    if new_status == "cancelled":
        sets["cancelled_at"] = ts
    if cur in ("done", "cancelled") and new_status == "todo":
        sets["completed_at"] = None
        sets["cancelled_at"] = None
    for k, v in (extra_fields or {}).items():
        sets[k] = v
    cols = ", ".join(f"{k}=?" for k in sets)
    conn.execute(f"UPDATE item SET {cols}, updated_at=? WHERE id=?", (*sets.values(), ts, item["id"]))
    log_activity(conn, item_id=item["id"], action=action or "status", actor=actor, ts=ts, field="status",
                 old_value=cur, new_value=new_status, reason=reason, evidence_source_event_id=evidence_sid,
                 run_id=run_id, payload=payload)
    if new_status in ("cancelled",):
        _withdraw_questions(conn, item["id"], ts, f"항목 {new_status}")
        _cancel_outbox(conn, item["id"], ts)
    if cur == "done":
        # 완료 취소(다시 열기): Daily 완료 기록도 지운다
        plan_daily_done(conn, cfg, conn.execute("SELECT * FROM item WHERE id=?", (item["id"],)).fetchone(), ts)
    if evidence_sid:
        conn.execute("INSERT OR IGNORE INTO item_source(item_id,source_event_id,role) VALUES(?,?,?)",
                     (item["id"], evidence_sid, "cancel" if new_status == "cancelled" else "update"))
    reindex_item(conn, item["id"])
    return {"changed": True, "from": cur, "status": new_status}


def complete_item(conn, cfg, item, evidence: str, *, actor: str, ts: str, note: str | None = None,
                  completed_at: str | None = None, run_id=None) -> dict:
    """완료. 근거 코드(§5.3) 없이는 거부한다 (TC9). 연결 질문 withdrawn, 대기 중 outbox 취소."""
    validate_evidence(evidence)
    if item["deleted_at"]:
        _bad("item_deleted", f"삭제된 항목입니다: {item['ref']}")
    if item["status"] == "done":
        # 완료 버튼을 다시 눌러도 기록은 한 번. 완료일을 따로 말하면 정정한다(Daily 기록도 옮긴다).
        if completed_at:
            new_at = _completed_ts(completed_at, ts)
            if new_at != item["completed_at"]:
                aid = log_activity(conn, item_id=item["id"], action="update", actor=actor, ts=ts, field="completed_at",
                                   old_value=item["completed_at"], new_value=new_at, reason=note or "완료일 정정",
                                   run_id=run_id, payload={"before": {"completed_at": item["completed_at"]}})
                conn.execute("UPDATE item SET completed_at=?, updated_at=? WHERE id=?", (new_at, ts, item["id"]))
                fresh = conn.execute("SELECT * FROM item WHERE id=?", (item["id"],)).fetchone()
                daily = plan_daily_done(conn, cfg, fresh, ts)
                return {"changed": True, "status": "done", "ref": item["ref"], "completed_at": new_at,
                        "corrected_from": item["completed_at"], "action_id": aid, "daily": daily,
                        "questions_withdrawn": []}
        return {"changed": False, "status": "done", "ref": item["ref"], "completed_at": item["completed_at"],
                "note": "이미 완료된 항목입니다. 다시 기록하지 않았습니다.", "daily": daily_state(conn, item)}
    if item["status"] == "cancelled":
        _bad("item_cancelled", f"{item['ref']} 는 취소된 항목입니다. 먼저 reopen 하세요")
    sid = None
    m = re.match(r"^criteria_evidence:(\d+)$", evidence)
    if m:
        sid = int(m.group(1))
        if conn.execute("SELECT 1 FROM source_event WHERE id=?", (sid,)).fetchone() is None:
            _bad("evidence_missing", f"근거 source_event {sid} 가 없습니다")
    done_at = _completed_ts(completed_at, ts)
    payload = {"before": {"status": item["status"], "completed_at": item["completed_at"]},
               "questions_withdrawn": [], "outbox_cancelled": []}
    conn.execute("UPDATE item SET status='done', completed_at=?, updated_at=? WHERE id=?", (done_at, ts, item["id"]))
    payload["questions_withdrawn"] = _withdraw_questions(conn, item["id"], ts, "항목 완료")
    payload["outbox_cancelled"] = _cancel_outbox(conn, item["id"], ts)
    aid = log_activity(conn, item_id=item["id"], action="complete", actor=actor, ts=ts, field="status",
                       old_value=item["status"], new_value="done", reason=evidence,
                       evidence_source_event_id=sid, run_id=run_id, payload=payload)
    if note:
        log_activity(conn, item_id=item["id"], action="note", actor=actor, ts=ts, field="note", new_value=note,
                     reason="완료 메모", run_id=run_id)
    if sid:
        conn.execute("INSERT OR IGNORE INTO item_source(item_id,source_event_id,role) VALUES(?,?,'evidence')",
                     (item["id"], sid))
    # 4단계 반영 예약: Obsidian 줄 체크 (requires_confirm=0). 지금은 대기열에만 둔다.
    for l in conn.execute("SELECT external_key FROM link WHERE item_id=? AND system='obsidian_task'", (item["id"],)):
        _enqueue(conn, "obsidian.check_task", item["id"], {"external_key": l["external_key"], "completed_on": done_at[:10]},
                 f"obsidian.check_task:{item['ref']}:{l['external_key']}", ts)
    reindex_item(conn, item["id"])
    daily = plan_daily_done(conn, cfg, conn.execute("SELECT * FROM item WHERE id=?", (item["id"],)).fetchone(), ts)
    return {"changed": True, "ref": item["ref"], "status": "done", "completed_at": done_at, "evidence": evidence,
            "action_id": aid, "questions_withdrawn": payload["questions_withdrawn"], "daily": daily}


def _completed_ts(completed_at: str | None, ts: str) -> str:
    """완료 시각. 날짜만 말하면 그날 23:59:59 (Daily 기록에는 시각 없이 날짜로 남는다)."""
    done_at = completed_at or ts
    if len(done_at) == 10:
        done_at = f"{done_at}T23:59:59{ts[-6:]}"
    return done_at


def _withdraw_questions(conn, item_id: int, ts: str, why: str) -> list[str]:
    refs = [r["ref"] for r in conn.execute("SELECT ref FROM question WHERE item_id=? AND status='open'", (item_id,))]
    if refs:
        conn.execute("UPDATE question SET status='withdrawn', resolution=? WHERE item_id=? AND status='open'",
                     (json.dumps({"withdrawn": why, "at": ts}, ensure_ascii=False), item_id))
    return refs


def _cancel_outbox(conn, item_id: int, ts: str) -> list[int]:
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM outbox WHERE item_id=? AND status IN ('pending','awaiting_confirm') "
        "AND op NOT IN ('obsidian.check_task','obsidian.daily_done')", (item_id,))]
    if ids:
        conn.execute(f"UPDATE outbox SET status='cancelled', done_at=? WHERE id IN ({','.join('?'*len(ids))})", (ts, *ids))
    return ids


def _enqueue(conn, op: str, item_id: int | None, payload: dict, dedupe_key: str, ts: str, requires_confirm: int = 0):
    old = conn.execute("SELECT id, status FROM outbox WHERE dedupe_key=?", (dedupe_key,)).fetchone()
    if old is not None and old["status"] in ("cancelled", "failed"):
        # 같은 키의 취소·실패 op 는 다시 살린다 (재시도 횟수 초기화)
        conn.execute("UPDATE outbox SET status=?, payload=?, requires_confirm=?, confirmed_at=NULL, not_before=NULL, attempts=0, "
                     "last_error=NULL, result=NULL, created_at=?, done_at=NULL WHERE id=?",
                     ("awaiting_confirm" if requires_confirm else "pending", json.dumps(payload, ensure_ascii=False),
                      requires_confirm, ts, old["id"]))
        return
    conn.execute(
        "INSERT OR IGNORE INTO outbox(op,item_id,payload,dedupe_key,requires_confirm,status,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (op, item_id, json.dumps(payload, ensure_ascii=False), dedupe_key, requires_confirm,
         "awaiting_confirm" if requires_confirm else "pending", ts))


def business_days_after(start: datetime, n: int) -> datetime:
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def answer_question(conn, cfg, q, answer: str, resolve: dict | None, *, actor: str, ts: str, run_id=None) -> dict:
    """질문 답변 반영 (§7.3). resolve 의 값은 형식 검증 뒤 항목에 쓰고, 열린 질문이 없으면 needs_info → todo."""
    if q["status"] != "open":
        _bad("question_closed", f"{q['ref']} 는 이미 {q['status']} 상태입니다")
    item = conn.execute("SELECT * FROM item WHERE id=?", (q["item_id"],)).fetchone() if q["item_id"] else None
    applied = []
    if resolve and item is not None:
        applied = apply_update(conn, cfg, item, resolve, actor=actor, ts=ts,
                               reason=f"{q['ref']} 답변: {answer[:80]}", run_id=run_id,
                               allow_calendar_owned=item["status"] == "needs_info")
        item = conn.execute("SELECT * FROM item WHERE id=?", (item["id"],)).fetchone()
    conn.execute("UPDATE question SET status='answered', answered_at=?, answer=?, resolution=? WHERE id=?",
                 (ts, answer, json.dumps(resolve or {}, ensure_ascii=False), q["id"]))
    transition = None
    transition_aid = None
    prev_status = item["status"] if item is not None else None
    if item is not None and item["status"] == "needs_info":
        open_left = conn.execute("SELECT COUNT(*) c FROM question WHERE item_id=? AND status='open'",
                                 (item["id"],)).fetchone()["c"]
        if open_left == 0:
            transition = set_status(conn, cfg, item, "todo", actor=actor, ts=ts,
                                    reason=f"{q['ref']} 답변으로 필드 확정", run_id=run_id)
            transition_aid = conn.execute("SELECT MAX(id) m FROM activity_log WHERE item_id=?", (item["id"],)).fetchone()["m"]
            item = conn.execute("SELECT * FROM item WHERE id=?", (item["id"],)).fetchone()
            transition["planned"] = plan_external(conn, cfg, item, ts, directed=True)
    aid = log_activity(conn, item_id=q["item_id"], action="answer", actor=actor, ts=ts, field=q["field"],
                       new_value=q["ref"], reason=answer, run_id=run_id,
                       payload={"question_id": q["id"], "before": {"status": "open"}, "applied": applied,
                                "transition_action_id": transition_aid, "prev_item_status": prev_status})
    return {"question": q["ref"], "item_ref": item["ref"] if item is not None else None, "applied": applied,
            "transition": transition, "action_id": aid}


def undo_action(conn, cfg, act, *, ts: str, actor: str = "user") -> dict:
    """직전 작업 되돌리기 (§7.4). payload.before 를 복원하고 undone_at 을 남긴다."""
    if act["undone_at"]:
        _bad("already_undone", f"이력 {act['id']} 는 이미 되돌렸습니다")
    payload = json.loads(act["payload"]) if act["payload"] else {}
    before = payload.get("before")
    if act["action"] not in ("update", "status", "complete", "reopen", "cancel", "hold", "wait", "progress", "answer",
                             "merge") or before is None:
        _bad("not_undoable", f"이력 {act['id']}({act['action']}) 는 되돌릴 수 없습니다")
    item = conn.execute("SELECT * FROM item WHERE id=?", (act["item_id"],)).fetchone()
    if item is None:
        _bad("item_not_found", "항목이 없습니다")
    restored = {}
    if act["action"] == "answer":
        q = conn.execute("SELECT * FROM question WHERE id=?", (payload.get("question_id"),)).fetchone()
        if q is not None:
            conn.execute("UPDATE question SET status='open', answered_at=NULL, answer=NULL, resolution=NULL WHERE id=?",
                         (q["id"],))
            restored["question"] = q["ref"]
        for ap in payload.get("applied", []):
            conn.execute(f"UPDATE item SET {ap['field']}=?, updated_at=? WHERE id=?", (ap["old"], ts, item["id"]))
            restored[ap["field"]] = ap["old"]
        if payload.get("transition_action_id"):
            prev = payload.get("prev_item_status") or "needs_info"
            conn.execute("UPDATE item SET status=?, updated_at=? WHERE id=?", (prev, ts, item["id"]))
            conn.execute("UPDATE activity_log SET undone_at=? WHERE id=?", (ts, payload["transition_action_id"]))
            restored["status"] = prev
            # 답변으로 예약된 외부 반영(아직 실행 전)은 취소한다
            n = conn.execute("UPDATE outbox SET status='cancelled', done_at=? WHERE item_id=? AND status IN ('pending','awaiting_confirm') "
                             "AND created_at=?", (ts, item["id"], act["created_at"])).rowcount
            if n:
                restored["outbox_cancelled"] = n
    else:
        for k, v in before.items():
            conn.execute(f"UPDATE item SET {k}=?, updated_at=? WHERE id=?", (v, ts, item["id"]))
            restored[k] = v
        for qref in payload.get("questions_withdrawn", []):
            conn.execute("UPDATE question SET status='open', resolution=NULL WHERE ref=? AND status='withdrawn'", (qref,))
        if act["action"] == "complete":
            conn.execute("UPDATE outbox SET status='cancelled', done_at=? WHERE item_id=? AND op='obsidian.check_task' "
                         "AND status='pending'", (ts, item["id"]))
    conn.execute("UPDATE activity_log SET undone_at=? WHERE id=?", (ts, act["id"]))
    # 완료·완료일 정정·다시 열기 등 무엇을 되돌렸든 Daily 완료 기록을 현재 상태에 맞춘다 (멱등)
    plan_daily_done(conn, cfg, conn.execute("SELECT * FROM item WHERE id=?", (item["id"],)).fetchone(), ts)
    uid = log_activity(conn, item_id=item["id"], action="undo", actor=actor, ts=ts, field=act["field"],
                       old_value=act["new_value"], new_value=act["old_value"],
                       reason=f"이력 {act['id']}({act['action']}) 되돌림", payload={"undone_action_id": act["id"]})
    reindex_item(conn, item["id"])
    return {"undone_action_id": act["id"], "action": act["action"], "restored": restored, "undo_action_id": uid,
            "ref": item["ref"]}


def add_relation(conn, from_id: int, to_id: int, rel_type: str, created_by: str, ts: str) -> bool:
    if rel_type not in REL_TYPES:
        _bad("bad_rel_type", f"관계 유형이 잘못되었습니다: {rel_type}", allowed=REL_TYPES)
    if from_id == to_id:
        _bad("self_relation", "자기 자신과의 관계는 만들 수 없습니다")
    cur = conn.execute("INSERT OR IGNORE INTO relation(from_item_id,to_item_id,rel_type,created_by,created_at) "
                       "VALUES(?,?,?,?,?)", (from_id, to_id, rel_type, created_by, ts))
    return cur.rowcount == 1


def add_link(conn, item_id: int, system: str, key: str, ts: str, version: str | None = None) -> bool:
    if system not in LINK_SYSTEMS:
        _bad("bad_link_system", f"연결 시스템이 잘못되었습니다: {system}", allowed=LINK_SYSTEMS)
    dup = conn.execute("SELECT item_id FROM link WHERE system=? AND external_key=?", (system, key)).fetchone()
    if dup and dup["item_id"] != item_id:
        other = conn.execute("SELECT ref FROM item WHERE id=?", (dup["item_id"],)).fetchone()
        _bad("link_taken", f"{key} 는 이미 {other['ref']} 에 연결되어 있습니다", existing=other["ref"])
    if dup:
        return False
    conn.execute("INSERT INTO link(item_id,system,external_key,version,last_seen_at,last_synced_at,sync_status) "
                 "VALUES(?,?,?,?,?,?,'ok')", (item_id, system, key, version, ts, ts))
    return True


def expire_and_withdraw(conn, cfg, now_s: str) -> dict:
    """질문 생애 정리 (§7.3): 완료·취소 항목의 질문은 withdrawn, 마감 후 7일 지난 질문은 expired."""
    days = int(cfg["followup"].get("question_expire_days_after_due", 7))
    cutoff = (parse_iso(now_s) - timedelta(days=days)).isoformat()
    withdrawn = [r["ref"] for r in conn.execute(
        "SELECT q.ref FROM question q JOIN item i ON i.id=q.item_id WHERE q.status='open' "
        "AND i.status IN ('done','cancelled')")]
    if withdrawn:
        conn.execute("UPDATE question SET status='withdrawn', resolution=? WHERE ref IN (%s)" %
                     ",".join("?" * len(withdrawn)),
                     (json.dumps({"withdrawn": "항목 종료", "at": now_s}, ensure_ascii=False), *withdrawn))
    expired = [r["ref"] for r in conn.execute(
        "SELECT q.ref FROM question q JOIN item i ON i.id=q.item_id WHERE q.status='open' "
        "AND i.due_at IS NOT NULL AND i.due_at < ?", (cutoff,))]
    if expired:
        conn.execute("UPDATE question SET status='expired', resolution=? WHERE ref IN (%s)" %
                     ",".join("?" * len(expired)),
                     (json.dumps({"expired": f"마감 후 {days}일 경과", "at": now_s}, ensure_ascii=False), *expired))
    return {"withdrawn": withdrawn, "expired": expired}


# --------------------------------------------------------------------------
# 외부 반영 예약 (4단계, D5)
# --------------------------------------------------------------------------

def user_directed(conn, item_id: int) -> bool:
    """교수님 지시(등록: 접두어) 또는 답변으로 확정된 항목인가 (D5 a)."""
    for r in conn.execute("SELECT e.platform, e.headers FROM item_source s JOIN source_event e ON e.id=s.source_event_id "
                          "WHERE s.item_id=? AND s.role='origin'", (item_id,)):
        if r["platform"] == "user":
            return True
        if r["platform"] == "discord":
            try:
                if (json.loads(r["headers"] or "{}").get("intent") == "register"):
                    return True
            except json.JSONDecodeError:
                pass
    if conn.execute("SELECT 1 FROM activity_log WHERE item_id=? AND action='answer' AND undone_at IS NULL LIMIT 1",
                    (item_id,)).fetchone():
        return True
    return False


def plan_external(conn, cfg, item, ts: str, *, directed: bool | None = None) -> list[dict]:
    """항목 상태에 맞는 외부 반영을 outbox 에 예약한다. 이미 있으면(dedupe_key) 건너뛴다.

    - Calendar 등록: meeting 이고 start_at 이 있고 calendar link 가 없을 때. 지시·답변 확정 항목은 자동, 그 외는 승인(D5 b).
    - Obsidian Daily 줄 추가: 마감(due_at)이 있고 obsidian link 가 없을 때. 정책은 config.apply.obsidian_add_task.
    - Obsidian 🆔 부여: obsidian link 가 있는데 키가 🆔가 아닐 때 (D12, 항상 자동).
    """
    if item["status"] in ("done", "cancelled", "captured", "needs_info"):
        return []
    ap = cfg.get("apply", {})
    if not ap.get("enabled", True):
        return []
    if directed is None:
        directed = user_directed(conn, item["id"])
    planned = []
    links = {r["system"]: r for r in conn.execute("SELECT * FROM link WHERE item_id=?", (item["id"],))}
    write_cal = next((c for c in cfg["calendars"] if c.get("write")), None)

    if calendar_eligible(cfg, item) and "calendar" not in links and write_cal:
        confirm = 0 if directed else int(ap.get("calendar_create_requires_confirm_for_discovered", 1))
        key = f"calendar.create:{item['ref']}"
        if _enqueue_new(conn, "calendar.create", item["id"], calendar_create_payload(cfg, item, write_cal), key, ts, confirm):
            planned.append({"op": "calendar.create", "requires_confirm": confirm})

    if item["due_at"] and item["kind"] in ("task", "deadline", "reply", "prep") and "obsidian_task" not in links:
        policy = ap.get("obsidian_add_task", "confirm_unless_directed")
        if policy != "never":
            confirm = 0 if (directed or policy == "always") else 1
            key = f"obsidian.add_task:{item['ref']}"
            due_time = (parse_iso(item["due_at"]).astimezone(tz_of(cfg)).strftime("%H:%M")
                        if item["due_precision"] == "time" else None)
            if _enqueue_new(conn, "obsidian.add_task", item["id"], {
                    "ref": item["ref"], "title": item["title"], "due": item["due_at"][:10], "due_time": due_time,
                    "note": item["canonical_note"],
                    "day": item["due_at"][:10] if ap.get("daily_for_due_date", True) else ts[:10]}, key, ts, confirm):
                planned.append({"op": "obsidian.add_task", "requires_confirm": confirm})

    ol = links.get("obsidian_task")
    if ol and not re.search(r"#clr\d{4,}$", ol["external_key"]):
        key = f"obsidian.assign_id:{item['ref']}:{ol['external_key']}"
        if _enqueue_new(conn, "obsidian.assign_id", item["id"], {"ref": item["ref"], "external_key": ol["external_key"]},
                        key, ts, 0):
            planned.append({"op": "obsidian.assign_id", "requires_confirm": 0})
    return planned


def calendar_eligible(cfg, item) -> bool:
    """달력에 올릴 항목: 시작 시각이 있는 회의, 또는 마감 시각이 확정되어 표시 구간이 있는 항목(0.5.0)."""
    if item["kind"] == "meeting":
        return bool(item["start_at"])
    return bool(cfg.get("apply", {}).get("deadline_calendar", True) and wants_window(item) and item["start_at"])


def calendar_create_payload(cfg, item, write_cal) -> dict:
    title = item["title"]
    note = item["canonical_note"]
    if item["kind"] != "meeting":
        title = cfg.get("apply", {}).get("deadline_title_prefix", "[마감] ") + title
        due_local = parse_iso(item["due_at"]).astimezone(tz_of(cfg)).strftime("%m/%d %H:%M")
        note = f"마감 {due_local}" + (f"\n[[{note}]]" if note else "")
    return {"calendar_id": write_cal["id"], "account": write_cal["account"], "ref": item["ref"], "title": title,
            "start_at": item["start_at"], "end_at": item["end_at"], "all_day": item["all_day"], "note": note,
            "due_at": item["due_at"] if item["kind"] != "meeting" else None}


def refresh_pending_create(conn, cfg, item_id: int) -> None:
    """아직 실행 전인 calendar.create 의 시각·제목을 항목 현재 값으로 맞춘다 (승인 목록에 옛 시각이 보이지 않게)."""
    row = conn.execute("SELECT * FROM outbox WHERE item_id=? AND op='calendar.create' AND status IN ('pending','awaiting_confirm')",
                       (item_id,)).fetchone()
    if row is None:
        return
    item = conn.execute("SELECT * FROM item WHERE id=?", (item_id,)).fetchone()
    old = json.loads(row["payload"])
    if not calendar_eligible(cfg, item):
        return
    new = calendar_create_payload(cfg, item, {"id": old["calendar_id"], "account": old["account"]})
    if new != old:
        conn.execute("UPDATE outbox SET payload=? WHERE id=?", (json.dumps(new, ensure_ascii=False), row["id"]))


def _enqueue_new(conn, op, item_id, payload, key, ts, requires_confirm) -> bool:
    if conn.execute("SELECT 1 FROM outbox WHERE dedupe_key=?", (key,)).fetchone():
        return False
    _enqueue(conn, op, item_id, payload, key, ts, requires_confirm)
    return True


# --------------------------------------------------------------------------
# 업무 상태 ↔ 외부 도구 반영 상태 (0.5.0) — 둘은 따로 움직인다
# --------------------------------------------------------------------------

STATUS_KO = {"captured": "포착", "needs_info": "확인 필요", "todo": "할 일", "in_progress": "진행 중",
             "waiting": "대기", "on_hold": "보류", "done": "완료", "cancelled": "취소"}
SYNC_KO = {"linked": "등록됨", "awaiting_confirm": "승인 대기", "pending": "반영 예정", "retrying": "재시도 중",
           "failed": "실패", "declined": "등록 안 함", "missing": "캘린더에서 사라짐", "conflict": "충돌 확인 필요",
           "recorded": "기록됨", "unrecorded": "미반영"}
ATTENDANCE_KO = {"undecided": "미정", "attending": "참석", "declined": "불참"}


def _op_state(row) -> str:
    if row["status"] == "awaiting_confirm":
        return "awaiting_confirm"
    if row["status"] == "pending":
        return "retrying" if row["attempts"] else "pending"
    if row["status"] == "failed":
        return "failed"
    if row["status"] == "cancelled" and (row["result"] or "").find("declined") >= 0:
        return "declined"
    return row["status"]


def sync_state_of(conn, item) -> dict:
    """항목의 외부 반영 상태. 업무 상태(status)와 독립이다.
    calendar/tasks: linked | awaiting_confirm | pending | retrying | failed | declined | missing | conflict | None
    daily(완료 기록): recorded | pending | retrying | failed | None"""
    out = {"calendar": None, "tasks": None, "daily": None}
    for l in conn.execute("SELECT system, sync_status FROM link WHERE item_id=?", (item["id"],)):
        key = {"calendar": "calendar", "obsidian_task": "tasks"}.get(l["system"])
        if key:
            out[key] = {"ok": "linked", "pending": "pending"}.get(l["sync_status"], l["sync_status"])
    ops = conn.execute("SELECT * FROM outbox WHERE item_id=? ORDER BY id", (item["id"],)).fetchall()
    for op, key in (("calendar.create", "calendar"), ("obsidian.add_task", "tasks")):
        rows = [r for r in ops if r["op"] == op]
        if rows and out[key] is None:
            st = _op_state(rows[-1])
            out[key] = None if st in ("done", "cancelled") else st
    for r in ops:
        if r["op"] == "calendar.update" and r["status"] in ("pending", "failed") and out["calendar"] == "linked":
            out["calendar"] = _op_state(r)
    out["daily"] = daily_state(conn, item, ops)
    return out


def daily_state(conn, item, ops=None) -> str | None:
    rows = [r for r in (ops if ops is not None else conn.execute(
        "SELECT * FROM outbox WHERE item_id=? AND op='obsidian.daily_done'", (item["id"],)).fetchall())
        if r["op"] == "obsidian.daily_done"]
    if rows and rows[-1]["status"] in ("pending", "failed"):
        return _op_state(rows[-1])
    if item["daily_logged_on"]:
        return "recorded"
    return None


def describe_state(conn, cfg, item) -> str:
    """브리핑 한 줄용: `업무 할 일 · 달력 승인 대기 · Tasks 등록됨 · 참석 미정`."""
    st = sync_state_of(conn, item)
    parts = [f"업무 {STATUS_KO.get(item['status'], item['status'])}"]
    if st["calendar"]:
        parts.append(f"달력 {SYNC_KO.get(st['calendar'], st['calendar'])}")
    if st["tasks"]:
        parts.append(f"Tasks {SYNC_KO.get(st['tasks'], st['tasks'])}")
    if st["daily"] and st["daily"] != "recorded":
        parts.append(f"Daily {SYNC_KO.get(st['daily'], st['daily'])}")
    if item["attendance"]:
        parts.append(f"참석 {ATTENDANCE_KO.get(item['attendance'], item['attendance'])}")
    return " · ".join(parts)


# --------------------------------------------------------------------------
# 승인 대기 목록 (0.5.0) — Calendar 등록 대기와 Obsidian Tasks 등록 대기를 나눈다
# --------------------------------------------------------------------------

def fmt_when(cfg, start: str | None, end: str | None = None, all_day=False) -> str:
    if not start:
        return ""
    tz = tz_of(cfg)
    wd = "월화수목금토일"
    if len(start) == 10 or all_day:
        d = datetime.strptime(start[:10], "%Y-%m-%d")
        return f"{d.month}/{d.day}({wd[d.weekday()]}) 종일"
    a = parse_iso(start).astimezone(tz)
    s = f"{a.month}/{a.day}({wd[a.weekday()]}) {a:%H:%M}"
    if end and len(end) > 10:
        b = parse_iso(end).astimezone(tz)
        s += f"–{b:%H:%M}" if b.date() == a.date() else f"–{b.month}/{b.day} {b:%H:%M}"
    return s


def _op_is_past(cfg, op: str, p: dict, now) -> bool:
    today = now.strftime("%Y-%m-%d")
    if op == "calendar.create":
        if not p.get("start_at"):
            return False
        if p.get("all_day") or len(p["start_at"]) == 10:
            return (p.get("end_at") or p["start_at"])[:10] < today
        end = parse_iso(p["end_at"]) if p.get("end_at") and len(p["end_at"]) > 10 else parse_iso(p["start_at"]) + timedelta(hours=1)
        return end < now
    if op == "obsidian.add_task":
        return bool(p.get("due")) and p["due"][:10] < today
    return False


def approval_queue(conn, cfg, now, include_past: bool = False) -> dict:
    """승인 대기(awaiting_confirm) 목록. 지난 일정은 목록에서 빼되 기록은 지우거나 바꾸지 않는다."""
    groups = {"calendar": [], "tasks": []}
    past = {"calendar": 0, "tasks": 0}
    past_refs = {"calendar": [], "tasks": []}
    rows = conn.execute("SELECT o.*, i.ref, i.title AS item_title, i.status AS item_status, i.kind, i.attendance, "
                        "i.due_at, i.due_precision FROM outbox o LEFT JOIN item i ON i.id=o.item_id "
                        "WHERE o.status='awaiting_confirm' ORDER BY o.id").fetchall()
    for r in rows:
        key = "calendar" if r["op"].startswith("calendar.") else "tasks" if r["op"] == "obsidian.add_task" else None
        if key is None:
            continue
        p = json.loads(r["payload"])
        is_past = _op_is_past(cfg, r["op"], p, now)
        if is_past:
            past[key] += 1
            past_refs[key].append(r["ref"])
            if not include_past:
                continue
        if key == "calendar":
            when = fmt_when(cfg, p.get("start_at"), p.get("end_at"), bool(p.get("all_day")))
        else:
            when = (fmt_when(cfg, p.get("due")) or "").replace(" 종일", "") + " 마감" if p.get("due") else ""
        groups[key].append({
            "outbox_id": r["id"], "op": r["op"], "item_ref": r["ref"], "title": r["item_title"] or p.get("title"),
            "when": when, "start_at": p.get("start_at"), "end_at": p.get("end_at"), "due": p.get("due") or p.get("due_at"),
            "daily_day": p.get("day"), "work_status": STATUS_KO.get(r["item_status"], r["item_status"]),
            "attendance": ATTENDANCE_KO.get(r["attendance"]) if r["attendance"] else None,
            "selected": bool(r["selected_at"]), "past": is_past, "created_at": r["created_at"]})
    for key in groups:
        groups[key].sort(key=lambda x: (x["start_at"] or x["due"] or "9999", x["outbox_id"]))
    return {"calendar": groups["calendar"], "tasks": groups["tasks"],
            "past_hidden": {} if include_past else {k: v for k, v in past.items() if v},
            "past_hidden_refs": {} if include_past else {k: v for k, v in past_refs.items() if v},
            "selected": [x["item_ref"] for k in groups for x in groups[k] if x["selected"]],
            "counts": {"calendar": len(groups["calendar"]), "tasks": len(groups["tasks"])}}


# --------------------------------------------------------------------------
# 완료 기록 → Obsidian Daily (0.5.0)
# --------------------------------------------------------------------------
#
# 기록 시점: 완료로 바뀌는 즉시 outbox 에 예약하고(claire_store --apply 또는 다음 claire_apply 가 쓴다),
# 매일 점검의 claire_apply 가 누락을 대조한다. 자정 일괄 기록보다 낫다고 판단한 이유:
#  - 교수님이 완료 직후 Daily 를 열면 바로 보인다(자정 전까지 비어 있지 않다).
#  - 다른 Obsidian 쓰기와 같은 outbox 경로라 재시도·백오프·미반영 표시가 그대로 적용된다.
#  - mac mini 가 자정에 잠들어 있거나 OpenClaw 가 멈춰도 기록이 사라지지 않는다(대조가 메운다).
#  - 완료일 정정·완료 취소는 어차피 이미 쓴 기록을 고쳐야 하므로 일괄 방식의 이점이 없다.
# 기록은 항목당 한 줄이고 `%%claire-done:CLR-0031%%` 표식으로 찾는다. 같은 줄은 두 번 쓰지 않는다.

def local_day(cfg, ts: str) -> str:
    return parse_iso(ts).astimezone(tz_of(cfg)).strftime("%Y-%m-%d")


def done_time_label(cfg, completed_at: str) -> str | None:
    """완료 시각 HH:MM. 날짜만 지정한 완료(23:59:59)는 시각을 쓰지 않는다."""
    local = parse_iso(completed_at).astimezone(tz_of(cfg))
    if completed_at[11:19] == "23:59:59":
        return None
    return local.strftime("%H:%M")


def related_note(conn, cfg, item) -> str | None:
    if item["canonical_note"]:
        return item["canonical_note"]
    daily_folder = cfg["obsidian"].get("daily_folder", "50 Daily")
    for l in conn.execute("SELECT external_key FROM link WHERE item_id=? AND system IN ('obsidian_task','obsidian_note')",
                          (item["id"],)):
        f = l["external_key"].split("#", 1)[0]
        if f and not f.startswith(daily_folder + "/"):
            return re.sub(r"\.md$", "", f.rsplit("/", 1)[-1])
    return None


def plan_daily_done(conn, cfg, item, ts: str) -> dict | None:
    """항목 상태에 맞게 Daily 완료 기록을 예약한다: 완료면 완료일 Daily 에 한 줄, 아니면 있던 줄을 지운다.
    같은 키(obsidian.daily_done:CLR)의 op 하나를 원하는 상태로 덮어쓴다 — 버튼을 여러 번 눌러도 한 줄."""
    dd = cfg.get("daily_done", {})
    if not dd.get("enabled", True) or item is None:
        return None
    want_day = local_day(cfg, item["completed_at"]) if item["status"] == "done" and item["completed_at"] else None
    have_day = item["daily_logged_on"]
    key = f"obsidian.daily_done:{item['ref']}"
    row = conn.execute("SELECT * FROM outbox WHERE dedupe_key=?", (key,)).fetchone()
    payload = {"ref": item["ref"], "title": item["title"], "day": want_day,
               "time": done_time_label(cfg, item["completed_at"]) if want_day else None,
               "note": related_note(conn, cfg, item) if want_day else None,
               "remove_from": sorted({d for d in (have_day,) if d and d != want_day})}
    if row is not None and row["status"] in ("pending", "failed") and json.loads(row["payload"]) == payload:
        return {"day": want_day, "remove_from": payload["remove_from"], "state": "pending", "changed": False}
    if want_day == have_day and (row is None or row["status"] not in ("pending", "failed")):
        if want_day is None:
            return None
        if row is None or json.loads(row["payload"]) == payload:
            return {"day": want_day, "state": "recorded", "changed": False}
        # 같은 날 안에서 시각·제목·노트가 바뀌었으면 그 줄을 고쳐 쓴다
    if row is None:
        conn.execute("INSERT INTO outbox(op,item_id,payload,dedupe_key,requires_confirm,status,created_at) "
                     "VALUES('obsidian.daily_done',?,?,?,0,'pending',?)",
                     (item["id"], json.dumps(payload, ensure_ascii=False), key, ts))
    else:
        conn.execute("UPDATE outbox SET status='pending', payload=?, attempts=0, last_error=NULL, not_before=NULL, "
                     "result=NULL, done_at=NULL, created_at=? WHERE id=?",
                     (json.dumps(payload, ensure_ascii=False), ts, row["id"]))
    return {"day": want_day, "remove_from": payload["remove_from"], "state": "pending", "changed": True}


def reconcile_daily_done(conn, cfg, ts: str) -> list[str]:
    """누락 대조: 완료인데 기록이 없거나 다른 날에 있는 항목, 완료가 아닌데 기록이 남은 항목을 다시 예약한다."""
    if not cfg.get("daily_done", {}).get("enabled", True):
        return []
    # 기능 도입(daily_done_since) 뒤에 기록된 완료만 대조한다. 그 전에 끝난 일을 옛 Daily 에 소급해 쓰지 않는다.
    # 기준은 완료일이 아니라 "완료를 기록한 시각"이다 (어제 끝낸 일을 오늘 알려 주면 어제 Daily 에 들어가야 한다).
    since = meta_get(conn, "daily_done_since") or "9999"
    fixed = []
    rows = conn.execute(
        "SELECT * FROM item i WHERE deleted_at IS NULL AND ("
        " (status='done' AND completed_at IS NOT NULL AND (daily_logged_on IS NOT NULL OR EXISTS ("
        "   SELECT 1 FROM activity_log a WHERE a.item_id=i.id AND a.undone_at IS NULL AND a.created_at >= ? "
        "   AND (a.action='complete' OR (a.action='update' AND a.field='completed_at')))))"
        " OR (status<>'done' AND daily_logged_on IS NOT NULL)) "
        "AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.item_id=i.id AND o.op='obsidian.daily_done' "
        "AND o.status IN ('pending','failed'))", (since,)).fetchall()
    for it in rows:
        want = local_day(cfg, it["completed_at"]) if it["status"] == "done" else None
        if want != it["daily_logged_on"]:
            plan_daily_done(conn, cfg, it, ts)
            fixed.append(it["ref"])
    return fixed
