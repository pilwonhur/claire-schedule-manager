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
    log_activity, reindex_item, tz_of, validate_evidence, parse_iso,
)

ISO_DT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?([+-]\d{2}:\d{2}|Z)$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 교수님·Claire 가 update 로 바꿀 수 있는 필드. status 는 전이 함수로만 바꾼다.
UPDATABLE = {
    "title": "text", "project": "text", "priority": "priority", "owner": "owner", "next_action": "text",
    "done_criteria": "text", "start_at": "dt", "end_at": "dt", "all_day": "bool", "due_at": "dt_eod",
    "scheduled_on": "date", "tentative": "bool", "waiting_on": "text", "next_check_at": "dt_eod",
    "blocked_reason": "text", "canonical_note": "text", "ledger_note": "text", "kind": "kind",
}
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


def apply_update(conn, cfg, item, changes: dict, *, actor: str, ts: str, reason: str | None,
                 evidence_sid: int | None = None, run_id=None, allow_calendar_owned: bool = False) -> list[dict]:
    """필드 변경. 필드마다 이력 1건. Calendar 연결 항목의 시각 필드는 거부한다(4단계 outbox 로)."""
    if item["deleted_at"]:
        _bad("item_deleted", f"삭제된 항목입니다: {item['ref']}")
    if item["status"] in ("done", "cancelled") and not allow_calendar_owned:
        _bad("item_closed", f"{item['ref']} 는 {item['status']} 상태입니다. 먼저 reopen 하세요")
    cal_linked = conn.execute("SELECT 1 FROM link WHERE item_id=? AND system='calendar' AND sync_status<>'missing'",
                              (item["id"],)).fetchone() is not None
    applied = []
    before = item_snapshot(item)
    deferred = {}
    for field, raw in changes.items():
        value = norm_value(field, raw, cfg)
        if field in CALENDAR_OWNED and cal_linked and not allow_calendar_owned:
            if actor != "user":
                _bad("calendar_owned_field",
                     f"{item['ref']} 의 {field} 는 Calendar 가 원본입니다. Claire 가 직접 바꿀 수 없습니다 (§9)",
                     field=field)
            deferred[field] = value          # 교수님 지시 → Calendar 에 반영 후 동기화로 돌아온다
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
            return applied
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
        return {"changed": False, "status": "done", "ref": item["ref"]}
    if item["status"] == "cancelled":
        _bad("item_cancelled", f"{item['ref']} 는 취소된 항목입니다. 먼저 reopen 하세요")
    sid = None
    m = re.match(r"^criteria_evidence:(\d+)$", evidence)
    if m:
        sid = int(m.group(1))
        if conn.execute("SELECT 1 FROM source_event WHERE id=?", (sid,)).fetchone() is None:
            _bad("evidence_missing", f"근거 source_event {sid} 가 없습니다")
    done_at = completed_at or ts
    if len(done_at) == 10:
        done_at = f"{done_at}T23:59:59{ts[-6:]}"
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
    return {"changed": True, "ref": item["ref"], "status": "done", "completed_at": done_at, "evidence": evidence,
            "action_id": aid, "questions_withdrawn": payload["questions_withdrawn"]}


def _withdraw_questions(conn, item_id: int, ts: str, why: str) -> list[str]:
    refs = [r["ref"] for r in conn.execute("SELECT ref FROM question WHERE item_id=? AND status='open'", (item_id,))]
    if refs:
        conn.execute("UPDATE question SET status='withdrawn', resolution=? WHERE item_id=? AND status='open'",
                     (json.dumps({"withdrawn": why, "at": ts}, ensure_ascii=False), item_id))
    return refs


def _cancel_outbox(conn, item_id: int, ts: str) -> list[int]:
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM outbox WHERE item_id=? AND status IN ('pending','awaiting_confirm') "
        "AND op NOT IN ('obsidian.check_task')", (item_id,))]
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

    if item["kind"] == "meeting" and item["start_at"] and "calendar" not in links and write_cal:
        confirm = 0 if directed else int(ap.get("calendar_create_requires_confirm_for_discovered", 1))
        key = f"calendar.create:{item['ref']}"
        if _enqueue_new(conn, "calendar.create", item["id"], {
                "calendar_id": write_cal["id"], "account": write_cal["account"], "ref": item["ref"],
                "title": item["title"], "start_at": item["start_at"], "end_at": item["end_at"], "all_day": item["all_day"],
                "note": item["canonical_note"]}, key, ts, confirm):
            planned.append({"op": "calendar.create", "requires_confirm": confirm})

    if item["due_at"] and item["kind"] in ("task", "deadline", "reply", "prep") and "obsidian_task" not in links:
        policy = ap.get("obsidian_add_task", "confirm_unless_directed")
        if policy != "never":
            confirm = 0 if (directed or policy == "always") else 1
            key = f"obsidian.add_task:{item['ref']}"
            if _enqueue_new(conn, "obsidian.add_task", item["id"], {
                    "ref": item["ref"], "title": item["title"], "due": item["due_at"][:10], "note": item["canonical_note"],
                    "day": item["due_at"][:10] if ap.get("daily_for_due_date", True) else ts[:10]}, key, ts, confirm):
                planned.append({"op": "obsidian.add_task", "requires_confirm": confirm})

    ol = links.get("obsidian_task")
    if ol and not re.search(r"#clr\d{4,}$", ol["external_key"]):
        key = f"obsidian.assign_id:{item['ref']}:{ol['external_key']}"
        if _enqueue_new(conn, "obsidian.assign_id", item["id"], {"ref": item["ref"], "external_key": ol["external_key"]},
                        key, ts, 0):
            planned.append({"op": "obsidian.assign_id", "requires_confirm": 0})
    return planned


def _enqueue_new(conn, op, item_id, payload, key, ts, requires_confirm) -> bool:
    if conn.execute("SELECT 1 FROM outbox WHERE dedupe_key=?", (key,)).fetchone():
        return False
    _enqueue(conn, op, item_id, payload, key, ts, requires_confirm)
    return True
