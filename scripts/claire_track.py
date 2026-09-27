"""claire_track — 완료까지의 노출·진행 확인 추적 (0.6.0, ISSUE 2026-09-27).

핵심 원칙: 등록 여부가 아니라, 완료될 때까지 노출·진행 확인이 보장되는지를 관리한다.

  - 중요도 기본값(R3): 교수님이 Discord 로 직접 등록 → 높음, 메일에서 교수님을 특정한 요청 → 높음,
    불특정 다수 공통 요청 → 보통. 마감이 없다는 이유로 낮추지 않는다. 교수님이 정한 값이 우선.
  - 진행 확인 계획(check_plan): 높음 매일, 보통 2일(주말 제외), 전날·당일·기한 경과·재확인일은 주기와 무관하게.
  - 시간 종류(time_kind): 실제 약속(appointment) · 작업 예약(work) · 자동 마감 표시(deadline_marker).
    자동 마감 표시끼리 겹친 것은 충돌이 아니라 작업량 안내다(R7).
  - 업무 현황(agenda): 활성 미완료 업무 전체를 구역별로. "외 N건"으로 숨기지 않는다(R2).
  - 전달 기록(delivery): 보고 생성과 전송 성공을 나눈다. 전송 성공한 페이지의 업무만 "보여 줌"·"확인함"(R8).

판단하지 않는 규칙만 둔다. 요청 범위(직접/단체)의 판단은 Claire 가 제안에 적는다(request_scope).
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

from claire_core import (
    OPEN_STATUSES, log_activity, meta_get, next_item_ref, parse_iso, reindex_item, sha256_text, tz_of,
)
import claire_ops as ops

PRIORITY_KO = {"high": "높음", "normal": "보통", "low": "낮음"}
SCOPE_KO = {"direct": "직접 요청", "group": "단체 요청", "self": "본인 할 일", "none": "요청 아님"}

# 진행 확인 사유 (정렬 순서 = 목록 순서)
REASON_KO = {"overdue": "기한 지남", "meeting_passed": "지난 일정 — 완료 체크", "d_day": "오늘",
             "d_minus_1": "내일", "recheck": "재확인일", "daily": "매일 확인", "cadence": "{n}일마다 확인"}
REASON_ORDER = ["overdue", "meeting_passed", "d_day", "d_minus_1", "recheck", "daily", "cadence"]


# --------------------------------------------------------------------------
# 날짜
# --------------------------------------------------------------------------

def local_date(cfg, ts: str | None) -> date | None:
    if not ts:
        return None
    if len(ts) == 10:
        return date.fromisoformat(ts)
    try:
        return parse_iso(ts).astimezone(tz_of(cfg)).date()
    except ValueError:
        return date.fromisoformat(ts[:10])


def add_days(start: date, n: int, skip_weekends: bool) -> date:
    if not skip_weekends:
        return start + timedelta(days=n)
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def md(d: date | None) -> str:
    return f"{d.month}/{d.day}" if d else ""


def _get(row, key, default=None):
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


# --------------------------------------------------------------------------
# 중요도 (R3)
# --------------------------------------------------------------------------

def classify_default(cfg, origins: list[dict], scope: str | None, given_priority: str | None,
                     given_reason: str | None) -> dict:
    """새 업무의 중요도·근거. origins = [{"platform", "headers"}] (원문). Claire 가 priority 를 주면 그 판단이 우선."""
    plats = {o["platform"] for o in origins}
    if given_priority:
        return {"priority": given_priority, "priority_source": "claire", "priority_reason": given_reason,
                "request_scope": scope}
    if plats & {"discord", "user"}:
        return {"priority": "high", "priority_source": "rule", "priority_reason": "교수님이 Discord 로 직접 등록",
                "request_scope": scope or "self"}
    if scope == "direct":
        return {"priority": "high", "priority_source": "rule", "priority_reason": "교수님을 특정한 요청",
                "request_scope": scope}
    if scope in ("group", "none", "self"):
        return {"priority": "normal", "priority_source": "rule", "priority_reason": SCOPE_KO[scope],
                "request_scope": scope}
    if "gmail" in plats:
        return {"priority": "normal", "priority_source": "unreviewed",
                "priority_reason": "요청 범위(직접/단체) 미분류 — 검토 필요", "request_scope": None}
    label = "캘린더 일정" if "calendar" in plats else "Obsidian Tasks 줄" if "obsidian" in plats else "출처 규칙 없음"
    return {"priority": "normal", "priority_source": "rule", "priority_reason": label, "request_scope": scope}


def origins_of(conn, item_id: int) -> list[dict]:
    out = []
    for r in conn.execute("SELECT e.platform, e.headers FROM item_source s JOIN source_event e ON e.id=s.source_event_id "
                          "WHERE s.item_id=? AND s.role='origin'", (item_id,)):
        try:
            h = json.loads(r["headers"] or "{}")
        except json.JSONDecodeError:
            h = {}
        out.append({"platform": r["platform"], "headers": h})
    return out


def backfill_importance(conn, cfg, ts: str) -> dict:
    """기존 활성 업무의 중요도 소급 분류 (한 번, maintain 이 부른다). 원본 상태·이력은 바꾸지 않는다.
    - Discord 직접 등록 → 높음(규칙)
    - 메일: 교수님 주소가 유일한 받는 사람(To)이면 잠정 높음, 아니면 보통. 둘 다 '검토 필요'로 남겨 Claire 가 확정한다
      (수신자 수만으로 정하지 않는다 — 잠정값일 뿐이고 근거를 적는다).
    - 캘린더·Obsidian → 보통(규칙)"""
    if meta_get(conn, "importance_backfilled"):
        return {"done": False, "note": "이미 소급 분류함"}
    me = {a.lower() for a in cfg["gmail"].get("user_addresses", [])}
    changed, review = [], []
    rows = conn.execute("SELECT * FROM item WHERE deleted_at IS NULL AND priority_source IS NULL AND status IN (%s)"
                        % ",".join("?" * len(OPEN_STATUSES)), OPEN_STATUSES).fetchall()
    for it in rows:
        origins = origins_of(conn, it["id"])
        plats = {o["platform"] for o in origins}
        if plats & {"discord", "user"}:
            pri, src, why, scope = "high", "rule", "교수님이 Discord 로 직접 등록 (소급)", "self"
        elif "gmail" in plats:
            h = next(o["headers"] for o in origins if o["platform"] == "gmail")
            to = [a.lower() for a in h.get("to") or []]
            sole = bool(to) and all(a in me for a in to)
            pri = "high" if sole else "normal"
            src, scope = "unreviewed", None
            why = ("교수님만 받는 사람(To)인 메일 — 잠정 높음, 검토 필요" if sole
                   else "여러 사람에게 온 메일 — 잠정 보통, 직접 요청·개인 의무인지 검토 필요")
            review.append(it["ref"])
        elif plats & {"calendar", "obsidian"}:
            pri, src, scope = "normal", "rule", None
            why = "캘린더 일정 (소급)" if "calendar" in plats else "Obsidian Tasks 줄 (소급)"
        else:
            pri, src, why, scope = "normal", "unreviewed", "출처 없음 — 검토 필요", None
            review.append(it["ref"])
        if it["priority"] == "high" and pri != "high":
            pri = "high"                         # 이미 높음으로 둔 것은 낮추지 않는다
            why += " (기존 높음 유지)"
        if pri != it["priority"]:
            log_activity(conn, item_id=it["id"], action="update", actor="claire", ts=ts, field="priority",
                         old_value=it["priority"], new_value=pri, reason=f"중요도 소급 분류: {why}",
                         payload={"before": {"priority": it["priority"], "priority_source": None,
                                             "priority_reason": None, "request_scope": it["request_scope"]}})
            changed.append(it["ref"])
        conn.execute("UPDATE item SET priority=?, priority_source=?, priority_reason=?, "
                     "request_scope=COALESCE(request_scope, ?) WHERE id=?", (pri, src, why, scope, it["id"]))
    conn.execute("INSERT INTO meta(key,value) VALUES('importance_backfilled',?) ON CONFLICT(key) DO NOTHING", (ts,))
    return {"done": True, "items": len(rows), "changed": changed, "needs_review": review}


# --------------------------------------------------------------------------
# 시간 종류 (R7)
# --------------------------------------------------------------------------

def time_kind(item) -> str | None:
    """appointment(실제 약속: 회의·수업·면담) · work(교수님이 잡은 작업 시간) · deadline_marker(마감 전 자동 표시 구간)."""
    start = _get(item, "start_at")
    if item["kind"] == "meeting":
        return "appointment" if start else None
    if start and len(start) > 10:
        return "deadline_marker" if _get(item, "window_auto") else "work"
    return None


def _span(start: str | None, end: str | None):
    if not start or len(start) == 10:
        return None
    try:
        a = parse_iso(start)
        b = parse_iso(end) if end and len(end) > 10 else a + timedelta(hours=1)
        return a, b
    except ValueError:
        return None


def overlay_on(conn, cfg, day: str) -> list[dict]:
    """overlay 캘린더(Family·HUR Group 등)의 그날 이벤트. 항목은 없고 겹침 확인용."""
    out = []
    for r in conn.execute("SELECT id, headers FROM source_event WHERE platform='calendar' "
                          "AND ignore_reason='overlay' ORDER BY id"):
        h = json.loads(r["headers"] or "{}")
        if h.get("status") == "cancelled":
            continue
        start = h.get("start")
        d = local_date(cfg, start) if start else None
        if d and d.isoformat() == day:
            out.append({"source_event_id": r["id"], "calendar": h.get("calendar"), "summary": h.get("summary"),
                        "start": start, "end": h.get("end"), "all_day": h.get("all_day"), "location": h.get("location")})
    dedup = {}
    for o in out:                                   # 같은 이벤트의 여러 버전(etag) 중 마지막만
        dedup[(o["calendar"], o["summary"], o["start"])] = o
    return sorted(dedup.values(), key=lambda x: x["start"] or "")


def conflicts(blocks: list[dict]) -> list[dict]:
    """실제 약속·작업 예약·overlay 일정끼리의 겹침. 자동 마감 표시는 넣지 않는다(호출자가 거른다)."""
    found = []
    for i in range(len(blocks)):
        for j in range(i + 1, len(blocks)):
            a, b = blocks[i]["span"], blocks[j]["span"]
            if a and b and a[0] < b[1] and b[0] < a[1]:
                found.append({"a": blocks[i]["ref"], "a_title": blocks[i]["title"], "a_kind": blocks[i]["kind"],
                              "b": blocks[j]["ref"], "b_title": blocks[j]["title"], "b_kind": blocks[j]["kind"]})
    return found


def deadline_clusters(cfg, items: list) -> list[dict]:
    """1시간 안에 몰린 마감(자동 표시 구간끼리 겹침). 충돌이 아니라 작업량 안내다."""
    need = int(cfg.get("tracking", {}).get("deadline_cluster_min", 2))
    marks = sorted(((parse_iso(i["due_at"]), i) for i in items
                    if time_kind(i) == "deadline_marker" and i["due_at"]), key=lambda x: x[0])
    out, group = [], []
    for due, it in marks:
        if group and (due - group[0][0]) > timedelta(hours=1):
            if len(group) >= need:
                out.append(group)
            group = []
        group.append((due, it))
    if len(group) >= need:
        out.append(group)
    tz = tz_of(cfg)
    return [{"refs": [it["ref"] for _, it in g], "titles": [it["title"] for _, it in g],
             "from": g[0][0].astimezone(tz).strftime("%H:%M"), "to": g[-1][0].astimezone(tz).strftime("%H:%M"),
             "count": len(g)} for g in out]


def day_conflicts(conn, cfg, day: date, overlays: list[dict] | None = None) -> dict:
    """그날의 실제 충돌(약속·작업 예약·overlay)과 마감 몰림(작업량 안내)."""
    rows = [r for r in conn.execute(
        "SELECT * FROM item WHERE deleted_at IS NULL AND start_at IS NOT NULL AND status IN (%s)"
        % ",".join("?" * len(OPEN_STATUSES)), OPEN_STATUSES) if local_date(cfg, r["start_at"]) == day]
    blocks = [{"ref": r["ref"], "title": r["title"], "kind": time_kind(r), "span": _span(r["start_at"], r["end_at"])}
              for r in rows if time_kind(r) in ("appointment", "work")]
    for o in overlays or []:
        blocks.append({"ref": f"overlay:{o['calendar']}", "title": o["summary"], "kind": "overlay",
                       "span": _span(o.get("start"), o.get("end"))})
    return {"overlaps": conflicts(blocks), "deadline_clusters": deadline_clusters(cfg, rows)}


# --------------------------------------------------------------------------
# 진행 확인 계획 (R3)
# --------------------------------------------------------------------------

def interval_days(cfg, item) -> int:
    if _get(item, "check_every_days"):
        return int(item["check_every_days"])
    return int(cfg.get("tracking", {}).get("interval_days", {}).get(item["priority"], 2))


def check_plan(cfg, item, now: datetime) -> dict:
    """오늘 이 업무의 진행을 물어야 하는가. {due, reason, label, next_on}.
    답이 없다고 완료·취소·중요도 하향하지 않는다. 같은 날 보고받았으면 그날 다시 묻지 않는다."""
    tr = cfg.get("tracking", {})
    today = now.astimezone(tz_of(cfg)).date()
    tomorrow = today + timedelta(days=1)
    out = {"due": False, "reason": None, "label": None, "next_on": None}
    st = item["status"]
    if st in ("done", "cancelled"):
        return {**out, "reason": "closed"}
    if local_date(cfg, _get(item, "last_reported_at")) == today:
        return {**out, "reason": "reported_today", "label": "오늘 보고받음"}
    if st in ("waiting", "on_hold"):
        nc = local_date(cfg, item["next_check_at"])
        if nc is None and st == "on_hold":       # 0.6.0 전 보류(재확인일 없음): 기본 N일 뒤
            nc = (local_date(cfg, item["updated_at"]) or today) + timedelta(days=int(tr.get("hold_default_days", 7)))
        if nc and nc <= today:
            return {"due": True, "reason": "recheck", "label": REASON_KO["recheck"], "next_on": nc}
        return {**out, "reason": "waiting" if st == "waiting" else "on_hold", "next_on": nc}
    if st == "captured":
        return {**out, "reason": "captured"}          # 신뢰도 낮은 참고 항목: 목록에는 있고 진행 질문은 하지 않는다
    ra = local_date(cfg, _get(item, "review_after"))
    if ra and ra > today:
        return {**out, "reason": "snoozed", "label": f"{md(ra)}부터 다시 확인", "next_on": ra}
    kind = item["kind"]
    start_d = local_date(cfg, item["start_at"]) if kind == "meeting" or time_kind(item) == "work" else None
    due_d = local_date(cfg, item["due_at"])
    if kind == "meeting" and item["start_at"] and start_d and start_d < today:
        sp = _span(item["start_at"], item["end_at"])
        if sp is None or sp[1] < now:
            return {"due": True, "reason": "meeting_passed", "label": REASON_KO["meeting_passed"], "next_on": today}
    if item["due_at"] and parse_iso(item["due_at"]) < now:
        return {"due": True, "reason": "overdue", "label": REASON_KO["overdue"], "next_on": today}
    if today in (due_d, start_d):
        return {"due": True, "reason": "d_day", "label": REASON_KO["d_day"], "next_on": today}
    if tomorrow in (due_d, start_d):
        return {"due": True, "reason": "d_minus_1", "label": REASON_KO["d_minus_1"], "next_on": today}
    if kind == "meeting" and start_d and start_d > tomorrow:
        # 앞으로의 약속 자체는 전날·당일에 확인한다 (준비 업무는 별도 항목으로 추적)
        return {**out, "reason": "upcoming", "next_on": start_d - timedelta(days=1)}
    n = interval_days(cfg, item)
    skip = item["priority"] in tr.get("skip_weekends_for", ["normal", "low"])
    asked = local_date(cfg, _get(item, "last_asked_at"))
    if asked == today:
        # 오늘 이미 물었다: 오늘의 확인 목록은 하루 동안 그대로 (재실행해도 같은 보고, 재전송 없음)
        reason = "daily" if n == 1 else "cadence"
        return {"due": True, "reason": reason, "label": REASON_KO[reason].format(n=n), "next_on": today,
                "asked_today": True}
    base = max(d for d in (asked, local_date(cfg, _get(item, "last_reported_at")), local_date(cfg, item["created_at"])) if d)
    nxt = add_days(base, n, skip)
    if nxt <= today:
        reason = "daily" if n == 1 else "cadence"
        return {"due": True, "reason": reason, "label": REASON_KO[reason].format(n=n), "next_on": today}
    return {**out, "reason": "scheduled", "next_on": nxt}


# --------------------------------------------------------------------------
# 업무 현황 (R2, 아침 보고 §5)
# --------------------------------------------------------------------------

def _due_text(cfg, it) -> str:
    if not it["due_at"]:
        return "기한 없음"
    d = local_date(cfg, it["due_at"])
    if it["due_precision"] == "time":
        return f"{md(d)} {parse_iso(it['due_at']).astimezone(tz_of(cfg)):%H:%M} 마감"
    return f"{md(d)} 마감"


def _hm(cfg, ts: str) -> str:
    return parse_iso(ts).astimezone(tz_of(cfg)).strftime("%H:%M")


def _ext_note(conn, it) -> str:
    """외부 반영 상태 중 교수님이 알아야 할 것만 (승인 대기·실패·표시 안 함). '등록됨'은 생략."""
    st = ops.sync_state_of(conn, it)
    bits = []
    for key, label in (("calendar", "달력"), ("tasks", "Tasks")):
        v = st[key]
        if v and v not in ("linked", "pending"):
            bits.append(f"{label} {ops.SYNC_KO.get(v, v)}")
    if st["daily"] in ("retrying", "failed"):
        bits.append(f"Daily {ops.SYNC_KO.get(st['daily'])}")
    return " · ".join(bits)


def _entry(conn, cfg, it, plan) -> dict:
    return {"id": it["id"], "ref": it["ref"], "title": it["title"], "kind": it["kind"], "status": it["status"],
            "status_label": ops.status_label(it), "priority": it["priority"],
            "priority_label": PRIORITY_KO.get(it["priority"]), "priority_source": it["priority_source"],
            "request_scope": it["request_scope"], "scope_label": SCOPE_KO.get(it["request_scope"] or ""),
            "due_at": it["due_at"], "due_text": _due_text(cfg, it), "start_at": it["start_at"], "end_at": it["end_at"],
            "time_kind": time_kind(it), "next_action": it["next_action"], "waiting_on": it["waiting_on"],
            "next_check_at": it["next_check_at"], "blocked_reason": it["blocked_reason"],
            "check": {**plan, "next_on": plan["next_on"].isoformat() if plan.get("next_on") else None},
            "external": _ext_note(conn, it)}


def agenda_data(conn, cfg, now: datetime, overlays: list[dict] | None = None) -> dict:
    """활성 미완료 업무 전체를 구역으로 나눈다. 한 업무는 주된 구역 한 곳에만 (중복 질문 방지).
    1 today   오늘 일정·마감·작업 예약 (+ 준비·후속 업무 상태, 실제 충돌, 마감 몰림)
    2 check   오늘 진행 확인 (기한 지남·지난 일정·내일·재확인일·매일/주기)
    3 open    그 외 진행 중·할 일·확인 필요·참고 (마감 없는 업무 포함) + 앞으로의 일정
    4 parked  답변 대기·보류 (다음 확인일 표시, 그 전에는 묻지 않는다)"""
    tz = tz_of(cfg)
    tr = cfg.get("tracking", {})
    today = now.astimezone(tz).date()
    rows = conn.execute("SELECT * FROM item WHERE deleted_at IS NULL AND status IN (%s) ORDER BY id"
                        % ",".join("?" * len(OPEN_STATUSES)), OPEN_STATUSES).fetchall()
    entries = {r["id"]: _entry(conn, cfg, r, check_plan(cfg, r, now)) for r in rows}
    by_id = {r["id"]: r for r in rows}

    def rel(item_id, rel_type, outgoing: bool):
        col_self, col_other = ("to_item_id", "from_item_id") if not outgoing else ("from_item_id", "to_item_id")
        return [{"ref": x["ref"], "title": x["title"], "status": x["status"], "status_label": ops.status_label(x)}
                for x in conn.execute(f"SELECT i.* FROM relation rl JOIN item i ON i.id=rl.{col_other} "
                                      f"WHERE rl.rel_type=? AND rl.{col_self}=? AND i.deleted_at IS NULL ORDER BY i.id",
                                      (rel_type, item_id))]

    today_list = []
    for r in rows:
        e = entries[r["id"]]
        tk = e["time_kind"]
        if r["kind"] == "meeting" and local_date(cfg, r["start_at"]) == today:
            e["role"] = "meeting"
            e["when"] = "종일" if r["all_day"] else f"{_hm(cfg, r['start_at'])}" + (f"–{_hm(cfg, r['end_at'])}" if r["end_at"] and len(r["end_at"]) > 10 else "")
            e["preps"] = rel(r["id"], "prep_for", outgoing=False)
            e["followups"] = rel(r["id"], "follow_up_of", outgoing=False)
            e["sort"] = r["start_at"]
            today_list.append(e)
        elif r["kind"] != "meeting" and r["due_at"] and local_date(cfg, r["due_at"]) == today:
            e["role"] = "due"
            e["when"] = f"{_hm(cfg, r['due_at'])} 마감" if r["due_precision"] == "time" else "오늘 마감"
            if tk == "work":
                e["when"] += f" (작업 {_hm(cfg, r['start_at'])}–{_hm(cfg, r['end_at'])})" if r["end_at"] else ""
            e["sort"] = r["due_at"]
            today_list.append(e)
        elif tk == "work" and local_date(cfg, r["start_at"]) == today:
            e["role"] = "work"
            e["when"] = f"{_hm(cfg, r['start_at'])}" + (f"–{_hm(cfg, r['end_at'])}" if r["end_at"] else "") + " 작업"
            e["sort"] = r["start_at"]
            today_list.append(e)
    today_list.sort(key=lambda x: x["sort"] or "")
    used = {e["id"] for e in today_list}

    check_list = [e for i, e in entries.items() if i not in used and e["check"]["due"]]
    check_list.sort(key=lambda e: (REASON_ORDER.index(e["check"]["reason"]), e["due_at"] or e["start_at"] or "9999",
                                   0 if e["priority"] == "high" else 1, e["id"]))
    used |= {e["id"] for e in check_list}

    horizon = today + timedelta(days=int(tr.get("upcoming_meeting_days", 7)))
    upcoming, later = [], 0
    parked = {"waiting": [], "on_hold": []}
    groups = {"in_progress": [], "todo": [], "needs_info": [], "captured": []}
    for i, e in entries.items():
        if i in used:
            continue
        r = by_id[i]
        if r["status"] in ("waiting", "on_hold"):
            parked[r["status"]].append(e)
            continue
        if r["kind"] == "meeting" and r["start_at"] and local_date(cfg, r["start_at"]) > today:
            if local_date(cfg, r["start_at"]) <= horizon:
                e["when"] = ops.fmt_when(cfg, r["start_at"], r["end_at"], bool(r["all_day"]))
                upcoming.append(e)
            else:
                later += 1
            continue
        groups.get(r["status"], groups["todo"]).append(e)
    for g in groups.values():
        g.sort(key=lambda e: (0 if e["priority"] == "high" else 1, e["due_at"] or "9999", e["id"]))
    upcoming.sort(key=lambda e: e["start_at"] or "")
    for g in parked.values():
        g.sort(key=lambda e: (e["next_check_at"] or "9999", e["id"]))

    day = day_conflicts(conn, cfg, today, overlays)
    check_refs = [e["ref"] for e in today_list if e["check"]["due"]] + [e["ref"] for e in check_list]
    counts = {"active": len(rows), "today": len(today_list), "check": len(check_refs),
              "open": sum(len(g) for g in groups.values()), "upcoming": len(upcoming), "later_meetings": later,
              "waiting": len(parked["waiting"]), "on_hold": len(parked["on_hold"])}
    return {"today_date": today.isoformat(), "weekday": "월화수목금토일"[today.weekday()], "counts": counts,
            "today": today_list, "overlay": overlays or [], "overlaps": day["overlaps"],
            "deadline_clusters": day["deadline_clusters"], "check": check_list, "open": groups,
            "upcoming": upcoming, "later_meetings": later, "parked": parked, "check_refs": check_refs}


def _block(ref: str) -> str:
    return f"```{ref}```"


def _line_bits(e, *, show_due=True) -> str:
    bits = []
    if show_due:
        bits.append(e["due_text"])
    if e["priority"] == "high":
        bits.append("중요")
    if e.get("scope_label") and e["request_scope"] in ("direct", "group"):
        bits.append(e["scope_label"])
    if e["next_action"]:
        bits.append(f"다음: {e['next_action']}")
    if e["external"]:
        bits.append(e["external"])
    return " · ".join(b for b in bits if b)


def agenda_text(cfg, data: dict) -> str:
    """업무 현황 본문 (번호 복사 블록 = 번호 버튼 자리). claire_buttons 가 페이지로 나눈다."""
    c = data["counts"]
    d = date.fromisoformat(data["today_date"])
    lines = [f"[업무 현황 {md(d)}({data['weekday']})] 활성 업무 {c['active']}건 · 오늘 확인 {c['check']}건"]
    # 1. 오늘
    lines.append(f"**1. 오늘 일정·마감 ({c['today']})**")
    if not data["today"] and not data["overlay"]:
        lines.append("  없음")
    for e in data["today"]:
        extra = []
        if e["role"] == "meeting":
            for p in e.get("preps", []):
                extra.append(f"준비: {p['title']}({p['ref']} {p['status_label']})")
            for p in e.get("followups", []):
                extra.append(f"후속: {p['title']}({p['ref']} {p['status_label']})")
        elif e["role"] == "due":
            bits = _line_bits(e, show_due=False)
            if bits:
                extra.append(bits)
        if e["check"]["due"]:
            extra.append("진행 확인")
        lines.append(f"  {e['when']}  {e['title']} ({e['ref']})" + (f" · {' · '.join(extra)}" if extra else ""))
        lines.append(_block(e["ref"]))
    for o in data["overlay"]:
        when = "종일" if o.get("all_day") else (_hm(cfg, o["start"]) + (f"–{_hm(cfg, o['end'])}" if o.get("end") and len(o["end"]) > 10 else ""))
        lines.append(f"  {when}  ({o['calendar']}) {o['summary']}")
    for ov in data["overlaps"]:
        a = ov["a"] if not ov["a"].startswith("overlay:") else ov["a"].split(":", 1)[1]
        b = ov["b"] if not ov["b"].startswith("overlay:") else ov["b"].split(":", 1)[1]
        lines.append(f"  ⚠ 겹침: {ov['a_title']}({a})와 {ov['b_title']}({b})")
    for cl in data["deadline_clusters"]:
        span = cl["from"] if cl["from"] == cl["to"] else f"{cl['from']}–{cl['to']}"
        lines.append(f"  ⏱ {span} 마감 {cl['count']}건({'·'.join(cl['refs'])}) — 약속 충돌이 아니라 작업량 안내입니다")
    # 2. 진행 확인
    lines.append(f"**2. 오늘 진행 확인 ({len(data['check'])})** — 번호를 누르면 완료·진행 중·보류·대기·처리 불필요")
    if not data["check"]:
        lines.append("  없음")
    for e in data["check"]:
        label = e["check"]["label"]
        if e["check"]["reason"] == "recheck":
            label += f" · {e['status_label']}" + (f"({e['waiting_on']})" if e["waiting_on"] else "")
        lines.append(f"  {label}  {e['title']} ({e['ref']})" + (f" · {_line_bits(e)}" if _line_bits(e) else ""))
        lines.append(_block(e["ref"]))
    # 3. 그 외
    lines.append(f"**3. 그 외 활성 업무 ({c['open']})**")
    if not c["open"]:
        lines.append("  없음")
    for key, label in (("in_progress", "진행 중"), ("todo", "할 일"), ("needs_info", "확인 필요(질문 대기)"),
                       ("captured", "참고(신뢰도 낮음 — 업무인지 확인)")):
        g = data["open"][key]
        if not g:
            continue
        lines.append(f"  {label} {len(g)}")
        for e in g:
            nxt = e["check"].get("next_on")
            tail = _line_bits(e)
            if nxt and key in ("todo", "in_progress"):
                tail += f" · 다음 확인 {md(date.fromisoformat(nxt))}"
            if e["check"]["reason"] == "reported_today":
                tail += " · 오늘 보고받음"
            elif e["check"]["reason"] == "snoozed":
                tail += f" · {e['check']['label']}"
            lines.append(f"  {e['title']} ({e['ref']})" + (f" · {tail.lstrip(' ·')}" if tail.strip(' ·') else ""))
            lines.append(_block(e["ref"]))
    if data["upcoming"] or data["later_meetings"]:
        lines.append(f"  앞으로의 일정 {len(data['upcoming'])}"
                     + (f" (그 뒤 {data['later_meetings']}건은 \"다음 주 일정\"으로 조회)" if data["later_meetings"] else ""))
        for e in data["upcoming"]:
            lines.append(f"  {e['when']}  {e['title']} ({e['ref']})")
            lines.append(_block(e["ref"]))
    # 4. 대기·보류
    parked = data["parked"]
    n4 = len(parked["waiting"]) + len(parked["on_hold"])
    lines.append(f"**4. 답변 대기·보류 ({n4})** — 다음 확인일 전에는 묻지 않습니다")
    if not n4:
        lines.append("  없음")
    for key in ("waiting", "on_hold"):
        for e in parked[key]:
            nc = local_date(cfg, e["next_check_at"])
            who = f"{e['waiting_on']} 답변 대기" if key == "waiting" else "보류" + (f"({e['blocked_reason']})" if e["blocked_reason"] else "")
            lines.append(f"  {e['title']} ({e['ref']}) · {who} · 다음 확인 {md(nc) if nc else '미정'}"
                         + (f" · {e['due_text']}" if e["due_at"] else ""))
            lines.append(_block(e["ref"]))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 전달 기록 (R8)
# --------------------------------------------------------------------------

def delivery_issues(conn, cfg, now: datetime, days: int = 2, exclude_kind_today: str | None = None) -> dict:
    """전송 실패·전송 확인 없는 페이지 (재전송 대상). 같은 보고의 다른 페이지는 다시 보내지 않는다.
    exclude_kind_today: 오늘의 그 종류 보고는 빼고 (업무 현황이 자기 자신의 실패를 본문에 넣으면 내용이 바뀌어
    '실패한 쪽만 재전송'이 아니라 새 보고가 되어 버린다)."""
    confirm = int(cfg.get("tracking", {}).get("delivery_confirm_minutes", 60))
    since = (now - timedelta(days=days)).isoformat()
    cutoff = (now - timedelta(minutes=confirm)).isoformat()
    failed, unconfirmed = [], []
    for r in conn.execute("SELECT * FROM delivery WHERE created_at >= ? AND status IN ('generated','failed') "
                          "ORDER BY id", (since,)):
        if exclude_kind_today and r["kind"] == exclude_kind_today and r["created_at"][:10] == now.isoformat()[:10]:
            continue
        d = {"delivery_id": r["id"], "report_key": r["report_key"], "kind": r["kind"], "page": r["page"],
             "pages": r["pages"], "items": json.loads(r["item_refs"] or "[]"), "error": r["error"],
             "attempts": r["attempts"], "created_at": r["created_at"]}
        if r["status"] == "failed":
            failed.append(d)
        elif r["created_at"] <= cutoff:
            unconfirmed.append(d)
    return {"failed": failed, "unconfirmed": unconfirmed, "count": len(failed) + len(unconfirmed)}


def exposure_gaps(conn, cfg, now: datetime) -> list[dict]:
    """누락 감시: 중요한 미완료 업무가 보고에 실리지 않았거나(전송 성공 기준), 진행 확인 주기를 넘겼다."""
    tr = cfg.get("tracking", {})
    grace = float(tr.get("exposure_grace_hours", 30))
    since = meta_get(conn, "tracking_since") or now.isoformat()
    since_d = local_date(cfg, since)
    today = now.astimezone(tz_of(cfg)).date()
    gaps = []
    for it in conn.execute("SELECT * FROM item WHERE deleted_at IS NULL AND status IN ('todo','in_progress','needs_info',"
                           "'waiting','on_hold') ORDER BY id"):
        plan = check_plan(cfg, it, now)
        if it["priority"] == "high" and plan["reason"] not in ("waiting", "on_hold", "snoozed"):
            last = max(x for x in (it["last_exposed_at"], since, it["created_at"]) if x)
            hrs = (now - parse_iso(last)).total_seconds() / 3600
            if hrs > grace:
                gaps.append({"ref": it["ref"], "title": it["title"], "why": "not_delivered",
                             "message": f"{int(hrs)}시간 동안 보고에 실려 전달되지 않음",
                             "last_exposed_at": it["last_exposed_at"]})
                continue
        if plan["due"] and plan["reason"] in ("daily", "cadence", "recheck"):
            last_ask = local_date(cfg, it["last_asked_at"])
            base = max(d for d in (last_ask, local_date(cfg, it["last_reported_at"]), local_date(cfg, it["created_at"]),
                                   since_d) if d)
            n = interval_days(cfg, it)
            late = (today - add_days(base, n, it["priority"] in tr.get("skip_weekends_for", ["normal", "low"]))).days
            if late >= 1:
                gaps.append({"ref": it["ref"], "title": it["title"], "why": "check_missed",
                             "message": f"진행 확인이 {late}일 늦음 (마지막 확인 {md(last_ask) if last_ask else '없음'})",
                             "last_asked_at": it["last_asked_at"]})
    return gaps


def health(conn, cfg, now: datetime, exclude_kind_today: str | None = None) -> dict:
    failed_ops = [{"item_ref": r["ref"], "title": r["title"], "op": r["op"], "error": r["last_error"]}
                  for r in conn.execute("SELECT o.op, o.last_error, i.ref, i.title FROM outbox o LEFT JOIN item i "
                                        "ON i.id=o.item_id WHERE o.status='failed' ORDER BY o.id")]
    retrying = [{"item_ref": r["ref"], "title": r["title"], "op": r["op"], "attempts": r["attempts"]}
                for r in conn.execute("SELECT o.op, o.attempts, i.ref, i.title FROM outbox o LEFT JOIN item i "
                                      "ON i.id=o.item_id WHERE o.status='pending' AND o.attempts > 0 ORDER BY o.id")]
    unreviewed = [r["ref"] for r in conn.execute(
        "SELECT ref FROM item WHERE deleted_at IS NULL AND priority_source='unreviewed' AND status IN (%s) ORDER BY id"
        % ",".join("?" * len(OPEN_STATUSES)), OPEN_STATUSES)]
    return {"failed_ops": failed_ops, "retrying_ops": retrying,
            "delivery": delivery_issues(conn, cfg, now, exclude_kind_today=exclude_kind_today),
            "exposure_gaps": exposure_gaps(conn, cfg, now), "importance_unreviewed": unreviewed}


OP_KO = {"calendar.create": "달력 등록", "calendar.update": "달력 변경", "obsidian.add_task": "Tasks 등록",
         "obsidian.check_task": "Tasks 체크", "obsidian.assign_id": "Tasks 🆔", "obsidian.daily_done": "Daily 완료 기록"}


def health_text(h: dict) -> str | None:
    lines = []
    for f in h["failed_ops"]:
        lines.append(f"  반영 실패: {f['item_ref']} {f['title']} — {OP_KO.get(f['op'], f['op'])} ({(f['error'] or '')[:60]})")
    for f in h["retrying_ops"]:
        lines.append(f"  반영 재시도 중: {f['item_ref']} {f['title']} — {OP_KO.get(f['op'], f['op'])} {f['attempts']}회")
    d = h["delivery"]
    for x in d["failed"]:
        lines.append(f"  보고 전송 실패: {x['report_key']} {x['page']}/{x['pages']}쪽 — 그 쪽 업무 {len(x['items'])}건은 "
                     "전달된 것으로 보지 않았습니다 (\"보고 다시\" 하시면 그 쪽만 다시 보냅니다)")
    for x in d["unconfirmed"]:
        lines.append(f"  전송 확인 없음: {x['report_key']} {x['page']}/{x['pages']}쪽 (업무 {len(x['items'])}건)")
    for g in h["exposure_gaps"]:
        lines.append(f"  누락 감지: {g['title']} ({g['ref']}) — {g['message']}")
    if h["importance_unreviewed"]:
        lines.append(f"  중요도 검토 필요 {len(h['importance_unreviewed'])}건 (직접 요청/단체 요청 분류 전 — Claire 가 확인 중)")
    if not lines:
        return None
    return "\n".join(["**6. 반영·전달 점검**"] + lines)


def _marks(refs) -> str:
    return json.dumps(sorted(set(refs)), ensure_ascii=False)


def register_delivery(conn, cfg, kind: str, messages: list[dict], ts: str, *, report_key: str | None = None,
                      check_refs=(), question_refs=()) -> dict:
    """보고 페이지를 '생성됨'으로 기록하고 messages[] 에 delivery_id 를 붙인다. 전송 성공은 mark_delivery 가 기록한다.
    같은 날 같은 종류·같은 내용이 이미 전부 전송됐으면 duplicate. 일부만 실패했으면 그 페이지만 돌려준다."""
    day = ts[:10]
    body_hash = sha256_text("\n␞\n".join(m.get("message", "") for m in messages))
    if report_key is None:
        prev = conn.execute("SELECT report_key, content_hash FROM delivery WHERE kind=? AND substr(created_at,1,10)=? "
                            "AND page=1 AND status<>'superseded' ORDER BY id DESC LIMIT 1", (kind, day)).fetchone()
        if prev is not None and prev["content_hash"] == body_hash:
            rows = conn.execute("SELECT * FROM delivery WHERE report_key=? ORDER BY page", (prev["report_key"],)).fetchall()
            unsent = [r for r in rows if r["status"] != "sent"]
            if not unsent:
                return {"report_key": prev["report_key"], "duplicate": True, "messages": [],
                        "message": "같은 내용의 보고를 오늘 이미 모두 보냈습니다. 다시 보내지 마세요."}
            return {"report_key": prev["report_key"], "duplicate": False, "resend": True,
                    "messages": [{**json.loads(r["payload"]), "delivery_id": r["id"], "index": r["page"], "of": r["pages"]}
                                 for r in unsent],
                    "message": f"같은 보고의 전송되지 않은 {len(unsent)}쪽만 다시 보냅니다."}
        seq = conn.execute("SELECT COUNT(DISTINCT report_key) c FROM delivery WHERE kind=? AND substr(created_at,1,10)=?",
                           (kind, day)).fetchone()["c"] + 1
        report_key = f"{kind}:{day}#{seq}"
        # 같은 종류로 만들어 놓고 보내지 못한 옛 보고(생성됨·실패)는 새 보고로 대체된다 — 새 보고가 현재 상태 전체를 싣는다
        conn.execute("UPDATE delivery SET status='superseded' WHERE kind=? AND status IN ('generated','failed')", (kind,))
    else:
        conn.execute("DELETE FROM delivery WHERE report_key=? AND status IN ('generated','superseded')", (report_key,))
        if conn.execute("SELECT 1 FROM delivery WHERE report_key=?", (report_key,)).fetchone():
            n = conn.execute("SELECT COUNT(DISTINCT report_key) c FROM delivery WHERE report_key LIKE ?",
                             (report_key + "%",)).fetchone()["c"]
            report_key = f"{report_key}#{n + 1}"
    checks, qs = set(check_refs), set(question_refs)
    out = []
    for i, m in enumerate(messages, 1):
        nums = m.get("numbers") or []
        text = m.get("message", "")
        page_qs = [q for q in re.findall(r"\bQ-\d+\b", text) if q in qs] if qs else re.findall(r"\bQ-\d+\b", text)
        items = [n for n in nums if n.startswith("CLR-")] or re.findall(r"\bCLR-\d+\b", text)
        payload = {"message": text, "components": m.get("components")}
        did = conn.execute(
            "INSERT INTO delivery(report_key,kind,page,pages,item_refs,check_refs,question_refs,payload,content_hash,"
            "problems,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?, 'generated', ?)",
            (report_key, kind, i, len(messages), _marks(items), _marks(r for r in items if r in checks), _marks(page_qs),
             json.dumps(payload, ensure_ascii=False), body_hash if i == 1 else sha256_text(text),
             json.dumps(m.get("problems") or [], ensure_ascii=False) if m.get("problems") else None, ts)).lastrowid
        out.append({**m, "delivery_id": did})
    return {"report_key": report_key, "duplicate": False, "messages": out}


def mark_delivery(conn, cfg, ids: list[int], status: str, ts: str, *, message_ids: list[str] | None = None,
                  error: str | None = None, via: str | None = None) -> dict:
    """전송 결과 기록. sent → 그 페이지 업무의 last_exposed_at, 진행 확인 업무의 last_asked_at, 질문 asked_count.
    failed → 업무에는 아무것도 기록하지 않는다(보여 준 것으로 치지 않는다)."""
    exposed, asked, qs, done, failed = set(), set(), [], [], []
    for n, did in enumerate(ids):
        r = conn.execute("SELECT * FROM delivery WHERE id=?", (did,)).fetchone()
        if r is None:
            ops._bad("delivery_not_found", f"전달 기록 {did} 가 없습니다")
        if status == "sent":
            if r["status"] == "sent":
                done.append({"delivery_id": did, "note": "이미 전송 기록됨"})
                continue
            mid = (message_ids[n] if message_ids and n < len(message_ids) else None)
            conn.execute("UPDATE delivery SET status='sent', sent_at=?, message_id=COALESCE(?, message_id), via=?, "
                         "error=NULL, attempts=attempts+1 WHERE id=?", (ts, mid, via or "message", did))
            items = json.loads(r["item_refs"] or "[]")
            checks = json.loads(r["check_refs"] or "[]")
            if items:
                conn.execute("UPDATE item SET last_exposed_at=? WHERE ref IN (%s)" % ",".join("?" * len(items)), (ts, *items))
                exposed.update(items)
            for ref in checks:
                cur = conn.execute("UPDATE item SET last_asked_at=? WHERE ref=? AND (last_asked_at IS NULL OR "
                                   "substr(last_asked_at,1,10) <> ?)", (ts, ref, ts[:10])).rowcount
                if cur:
                    asked.add(ref)
            qs += mark_questions_asked(conn, cfg, json.loads(r["question_refs"] or "[]"), ts)
            done.append({"delivery_id": did, "page": r["page"], "pages": r["pages"], "items": len(items)})
        else:
            conn.execute("UPDATE delivery SET status='failed', error=?, via=?, attempts=attempts+1 WHERE id=? AND status<>'sent'",
                         (error, via, did))
            failed.append({"delivery_id": did, "page": r["page"], "pages": r["pages"],
                           "items": json.loads(r["item_refs"] or "[]")})
    return {"sent": done, "failed": failed, "exposed": sorted(exposed), "asked": sorted(asked), "questions": qs}


WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def next_question_ask(cfg, now: datetime, asked_count: int, due_at: str | None) -> str:
    """D6: 첫 질문 뒤 두 번의 아침까지 매일, 이후 주 1회(월). 마감 48h 이내면 매일."""
    fu = cfg["followup"]
    tomorrow = (now + timedelta(days=1)).replace(hour=int(cfg["run"]["daily_hour"]), minute=0, second=0)
    urgent = False
    if due_at:
        try:
            urgent = (parse_iso(due_at) - now).total_seconds() <= fu["reask_urgent_hours"] * 3600
        except ValueError:
            urgent = False
    if urgent or asked_count <= int(fu["reask_daily_days"]):
        return tomorrow.isoformat()
    wd = WEEKDAYS.get(str(fu.get("reask_weekday", "mon")).lower(), 0)
    days = (wd - now.weekday()) % 7 or 7
    return (now + timedelta(days=days)).replace(hour=int(cfg["run"]["daily_hour"]), minute=0, second=0).isoformat()


def mark_questions_asked(conn, cfg, q_refs: list[str], now_s: str) -> list[dict]:
    """질문이 실제로 전달됐을 때만 부른다. 같은 질문을 하루에 두 번 세지 않는다."""
    now = parse_iso(now_s)
    out = []
    for ref in q_refs:
        q = conn.execute("SELECT q.*, i.due_at FROM question q LEFT JOIN item i ON i.id=q.item_id WHERE q.ref=?",
                         (ref,)).fetchone()
        if q is None or q["status"] != "open":
            continue
        if q["last_asked_at"] and q["last_asked_at"][:10] == now_s[:10]:
            out.append({"ref": ref, "asked_count": q["asked_count"], "note": "오늘 이미 질문함"})
            continue
        count = q["asked_count"] + 1
        nxt = next_question_ask(cfg, now, count, q["due_at"])
        conn.execute("UPDATE question SET asked_count=?, first_asked_at=COALESCE(first_asked_at,?), "
                     "last_asked_at=?, next_ask_at=? WHERE id=?", (count, now_s, now_s, nxt, q["id"]))
        out.append({"ref": ref, "asked_count": count, "next_ask_at": nxt})
    return out


# --------------------------------------------------------------------------
# 준비·후속 업무 (R1)
# --------------------------------------------------------------------------

def create_linked(conn, cfg, meeting, *, role: str, title: str, ts: str, kind: str = "prep", due_at: str | None = None,
                  due_precision: str | None = None, next_action: str | None = None, actor: str = "user",
                  priority: dict | None = None, reason: str | None = None) -> str:
    """일정(회의·수업)에 연결된 별도 업무를 만든다. 일정 완료가 이 업무를 완료시키지 않는다.
    role: prep(준비 → prep_for) | followup(후속 → follow_up_of)."""
    rel_type = {"prep": "prep_for", "followup": "follow_up_of"}.get(role)
    if rel_type is None:
        ops._bad("bad_value", "role 은 prep | followup")
    pri = priority or ({"priority": "high", "priority_source": "rule", "priority_reason": "교수님 지시로 연결 업무 등록",
                        "request_scope": "self"} if actor == "user" else
                       {"priority": "normal", "priority_source": "rule", "priority_reason": reason or "연결 업무",
                        "request_scope": "self"})
    ref = next_item_ref(conn, cfg)
    iid = conn.execute(
        "INSERT INTO item(ref,title,kind,status,project,owner,next_action,done_criteria,due_at,due_precision,tentative,"
        "priority,priority_source,priority_reason,request_scope,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?)",
        (ref, title, kind, "todo", meeting["project"], "user", next_action,
         {"prep": "자료 준비 완료"}.get(kind), due_at, due_precision if due_at else None,
         pri["priority"], pri["priority_source"], pri["priority_reason"], pri["request_scope"], ts, ts)).lastrowid
    for r in conn.execute("SELECT source_event_id FROM item_source WHERE item_id=? AND role='origin'", (meeting["id"],)).fetchall():
        conn.execute("INSERT OR IGNORE INTO item_source(item_id,source_event_id,role) VALUES(?,?,'mention')",
                     (iid, r["source_event_id"]))
    ops.add_relation(conn, iid, meeting["id"], rel_type, "user" if actor == "user" else "claire", ts)
    log_activity(conn, item_id=iid, action="create", actor=actor, ts=ts, field="status", new_value="todo",
                 reason=reason or f"{meeting['ref']} {'준비' if role == 'prep' else '후속'} 업무",
                 payload={"linked_to": meeting["ref"], "rel_type": rel_type})
    reindex_item(conn, iid)
    return ref


def apply_prep_templates(conn, cfg, now: datetime, ts: str, horizon_days: int = 14) -> list[dict]:
    """승인된 준비 템플릿(config.prep_templates)만으로 다가오는 회차의 준비 업무를 만든다. 템플릿이 없으면 아무것도 안 한다.
    이미 준비 업무(prep_for)가 연결된 회차는 건너뛴다 (교수님이 따로 만든 것 포함). 근거 없는 의무를 지어내지 않는다."""
    tpls = cfg.get("prep_templates") or []
    if not tpls:
        return []
    made = []
    end = (now + timedelta(days=horizon_days)).isoformat()
    meetings = conn.execute("SELECT * FROM item WHERE deleted_at IS NULL AND kind='meeting' AND start_at > ? AND start_at <= ? "
                            "AND status IN (%s) ORDER BY start_at" % ",".join("?" * len(OPEN_STATUSES)),
                            (now.isoformat(), end, *OPEN_STATUSES)).fetchall()
    for m in meetings:
        if conn.execute("SELECT 1 FROM relation WHERE rel_type='prep_for' AND to_item_id=?", (m["id"],)).fetchone():
            continue
        for t in tpls:
            key = (t.get("match") or "").strip()
            if not key or key.lower() not in (m["title"] or "").lower():
                continue
            if m["start_at"] and len(m["start_at"]) > 10:
                due = parse_iso(m["start_at"]) - timedelta(days=int(t.get("days_before", 1)))
                if t.get("due_time"):
                    hh, mm = (int(x) for x in str(t["due_time"]).split(":"))
                    due = due.replace(hour=hh, minute=mm, second=0)
                due_s, prec = due.isoformat(), "time"
            else:
                due_s, prec = None, None
            title = str(t.get("title") or "{title} 준비").format(title=m["title"], date=md(local_date(cfg, m["start_at"])))
            ref = create_linked(conn, cfg, m, role="prep", title=title, ts=ts, kind=t.get("kind", "prep"), due_at=due_s,
                                due_precision=prec, actor="claire",
                                priority={"priority": t.get("priority", "normal"), "priority_source": "rule",
                                          "priority_reason": f"준비 템플릿 '{key}'", "request_scope": "self"},
                                reason=f"준비 템플릿 '{key}' — {m['ref']} {m['title']}")
            made.append({"ref": ref, "for": m["ref"], "title": title, "template": key})
            break
    return made


# --------------------------------------------------------------------------
# 메시지 검증 (R8 — 빈 본문·버튼 누락)
# --------------------------------------------------------------------------

def validate_message(m: dict, max_components: int = 40, max_chars: int = 4000) -> list[str]:
    problems = []
    text = (m.get("message") or "").strip()
    if not text:
        problems.append("empty_message")
    comp = m.get("components")
    if comp is not None:
        blocks = comp.get("blocks") or []
        if not blocks and not (comp.get("text") or "").strip():
            problems.append("empty_components")
        body_chars = len(comp.get("text") or "")
        count = 2
        labels = []
        for b in blocks:
            if b["type"] == "text":
                count += 1
                body_chars += len(b.get("text") or "")
                if not (b.get("text") or "").strip():
                    problems.append("empty_text_block")
            elif b["type"] == "section":
                count += 3
                body_chars += len(b.get("text") or "")
                if not (b.get("text") or "").strip():
                    problems.append("section_without_text")
                btn = ((b.get("accessory") or {}).get("button") or {}).get("label")
                if not btn:
                    problems.append("section_without_button")
                labels.append(btn or "")
            elif b["type"] == "actions":
                count += 1 + len(b.get("buttons") or [])
                if not b.get("buttons"):
                    problems.append("empty_button_row")
                labels += [x.get("label", "") for x in b.get("buttons") or []]
        if count > max_components:
            problems.append(f"too_many_components:{count}")
        if body_chars > max_chars:
            problems.append(f"too_many_chars:{body_chars}")
        for n in m.get("numbers") or []:
            if not any(lbl == n or lbl.startswith(n + " ") for lbl in labels):
                problems.append(f"missing_button:{n}")
    return problems
