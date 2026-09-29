"""claire_track — 완료까지의 노출·진행 확인 추적 (0.6.0, ISSUE 2026-09-27).

핵심 원칙: 등록 여부가 아니라, 완료될 때까지 노출·진행 확인이 보장되는지를 관리한다.

  - 중요도 기본값(R3): 교수님이 Discord 로 직접 등록 → 높음, 메일에서 교수님을 특정한 요청 → 높음,
    불특정 다수 공통 요청 → 보통. 마감이 없다는 이유로 낮추지 않는다. 교수님이 정한 값이 우선.
  - 진행 확인 계획(check_plan): 높음 매일, 보통 2일(주말 제외), 전날·당일·기한 경과·재확인일은 주기와 무관하게.
  - 시간 종류(time_kind): 실제 약속(appointment) · 작업 예약(work) · 자동 마감 표시(deadline_marker).
    자동 마감 표시끼리 겹친 것은 충돌이 아니라 작업량 안내다(R7).
  - 업무 현황(agenda): 활성 미완료 업무 전체를 구역별로. "외 N건"으로 숨기지 않는다(R2).
  - 전달 기록(delivery): 보고 생성과 전송 성공을 나눈다. 전송 성공한 페이지의 업무만 "보여 줌"·"확인함"(R8).
  - 아침 보고(report, 0.7.0 ISSUE 2026-09-29): 전체 업무는 계속 추적하되 보고에는 필요한 만큼만 — 오늘 · 기한 지남(한 쪽) ·
    진행 확인(최대 두 쪽) · 확인·상태. 버튼과 함께 보일 업무는 점수로 고르고 나머지는 1~3단어 키워드(번호 포함)로 줄인다.
    표시를 줄여도 업무 상태·마감·추적 정보는 그대로다. 전체는 요청하면 본다(업무 현황 agenda / --view).

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
             "d_minus_1": "내일", "recheck": "재확인일", "wait_due": "마감 임박(대기 중)", "daily": "매일 확인",
             "cadence": "{n}일마다 확인"}
REASON_ORDER = ["overdue", "meeting_passed", "d_day", "d_minus_1", "recheck", "wait_due", "daily", "cadence"]


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
    """중요도가 분류되지 않은(priority_source 없음) 활성 업무의 소급 분류. 원본 상태·이력은 바꾸지 않는다.
    0.6.1: 한 번만 도는 표시를 두지 않는다 — 분류 없는 업무만 고르므로 여러 번 불러도 같고(멱등), 업그레이드 뒤 설치·점검·업무 현황
    어느 쪽이 먼저 불러도 된다. 결과 done 은 이번에 분류한 업무가 있었는지.
    - Discord 직접 등록 → 높음(규칙)
    - 메일: 교수님 주소가 유일한 받는 사람(To)이면 잠정 높음, 아니면 보통. 둘 다 '검토 필요'로 남겨 Claire 가 확정한다
      (수신자 수만으로 정하지 않는다 — 잠정값일 뿐이고 근거를 적는다).
    - 캘린더·Obsidian → 보통(규칙)"""
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
    if rows:
        conn.execute("INSERT INTO meta(key,value) VALUES('importance_backfilled',?) ON CONFLICT(key) DO NOTHING", (ts,))
    return {"done": bool(rows), "items": len(rows), "changed": changed, "needs_review": review}


def repair_deadline_markers(conn, cfg, ts: str) -> list[str]:
    """0.6.1: 구간이 정확히 '마감 1시간 전~마감'인데 자동 표시(window_auto)가 꺼진 열린 업무를 자동 마감 표시로 되돌린다.
    0.5.0 전에 직접 넣은 구간·제안에 구간을 함께 준 경우·Calendar 동기화의 표기 차이로 꺼진 경우. 멱등."""
    fixed = []
    for it in conn.execute("SELECT * FROM item WHERE deleted_at IS NULL AND kind<>'meeting' AND window_auto=0 "
                           "AND due_precision='time' AND start_at IS NOT NULL AND end_at IS NOT NULL AND status IN (%s)"
                           % ",".join("?" * len(OPEN_STATUSES)), OPEN_STATUSES).fetchall():
        if ops.is_default_window(cfg, it):
            conn.execute("UPDATE item SET window_auto=1 WHERE id=?", (it["id"],))
            log_activity(conn, item_id=it["id"], action="update", actor="claire", ts=ts, field="window_auto", old_value=0,
                         new_value=1, reason="마감 1시간 전~마감 구간 → 자동 마감 표시로 보정 (작업 예약 아님)",
                         payload={"before": {"window_auto": 0}})
            fixed.append(it["ref"])
    return fixed


def backfill_recheck_dates(conn, cfg, ts: str) -> list[str]:
    """0.7.0: 모든 대기·보류 업무에 재확인 날짜를 둔다. 날짜가 없는 것(0.6.x 전 기록)은 마지막 변경 + 기본 일수, 이미 지났으면 오늘.
    답이 없어도 무기한 기다리지 않게 한다. 멱등(날짜가 있는 업무는 건드리지 않는다)."""
    fixed = []
    today = parse_iso(ts).astimezone(tz_of(cfg)).date()
    for it in conn.execute("SELECT * FROM item WHERE deleted_at IS NULL AND status IN ('waiting','on_hold') "
                           "AND (next_check_at IS NULL OR next_check_at='')").fetchall():
        base = local_date(cfg, it["updated_at"]) or today
        d = max(today, base + timedelta(days=default_wait_days(cfg, it["status"])))
        nxt = at_daily_hour(cfg, d)
        conn.execute("UPDATE item SET next_check_at=? WHERE id=?", (nxt, it["id"]))
        log_activity(conn, item_id=it["id"], action="update", actor="claire", ts=ts, field="next_check_at", old_value=None,
                     new_value=nxt, reason="재확인일이 없는 대기·보류 → 기본 재확인일 (0.7.0)",
                     payload={"before": {"next_check_at": it["next_check_at"]}})
        fixed.append(it["ref"])
    return fixed


def release_task_approvals(conn, cfg, ts: str) -> list[str]:
    """0.7.0: Tasks 등록은 승인 없이 자동(교수님 결정 2026-09-29). 승인을 기다리던 Tasks 등록을 바로 반영 대기로 돌린다.
    줄의 자리는 반영할 때 정한다(업무의 정본 노트 → 프로젝트 연결 문서 → 임시 수집 노트). "표시 안 함"으로 뺀 것은 그대로 둔다."""
    if cfg.get("apply", {}).get("obsidian_add_task", "always") != "always":
        return []
    rows = conn.execute("SELECT o.id, i.ref FROM outbox o JOIN item i ON i.id=o.item_id WHERE o.op='obsidian.add_task' "
                        "AND o.status='awaiting_confirm' AND i.deleted_at IS NULL AND i.status IN (%s)"
                        % ",".join("?" * len(OPEN_STATUSES)), OPEN_STATUSES).fetchall()
    for r in rows:
        conn.execute("UPDATE outbox SET status='pending', requires_confirm=0, confirmed_at=?, selected_at=NULL WHERE id=?",
                     (ts, r["id"]))
    return [r["ref"] for r in rows]


def upkeep(conn, cfg, ts: str) -> dict:
    """설치(claire_check init)·매일 점검(maintain)·업무 현황(agenda)·아침 보고(report) 앞에서 도는 멱등 정리.
    기존 업무 중요도 소급 분류, 자동 마감 표시 보정, 대기·보류 재확인일 채우기, 승인 대기 Tasks 자동 전환(0.7.0)."""
    return {"importance_backfill": backfill_importance(conn, cfg, ts),
            "markers_repaired": repair_deadline_markers(conn, cfg, ts),
            "recheck_dates_set": backfill_recheck_dates(conn, cfg, ts),
            "task_approvals_released": release_task_approvals(conn, cfg, ts)}


# --------------------------------------------------------------------------
# 대기·보류 재확인일 (0.7.0, ISSUE 2026-09-29 §4)
# --------------------------------------------------------------------------

def default_wait_days(cfg, status: str) -> int:
    if status == "on_hold":
        return int(cfg.get("tracking", {}).get("hold_default_days", 3))
    return int(cfg.get("followup", {}).get("wait_default_days", 3))


def at_daily_hour(cfg, d: date) -> str:
    return datetime(d.year, d.month, d.day, int(cfg["run"]["daily_hour"]), 0, 0, tzinfo=tz_of(cfg)).isoformat()


def recheck_plan(cfg, now: datetime, status: str, until: date | None, due_at: str | None) -> dict:
    """대기·보류의 재확인일. {"date", "defaulted", "note"}.
    - 교수님이 날짜를 말하면 그 날짜(마감보다 늦어도 존중하되, 마감은 그대로이고 마감 전날·당일에는 알린다는 안내).
    - 날짜가 없으면 기본 N일 뒤(달력 기준). 마감이 더 빠르면 마감 전날로 당기고, 이미 마감이 지났으면 다음 날.
    대기 설정으로 업무 마감을 연장하지 않는다."""
    today = now.astimezone(tz_of(cfg)).date()
    due_d = local_date(cfg, due_at) if due_at else None
    if until:
        note = None
        if due_d and until > due_d:
            note = (f"마감 {md(due_d)}이 재확인일 {md(until)}보다 먼저입니다. 마감은 그대로이고 마감 전날·당일에는 알려 드립니다.")
        return {"date": until, "defaulted": False, "note": note}
    n = default_wait_days(cfg, status)
    d = today + timedelta(days=n)
    note = None
    if due_d and due_d - timedelta(days=1) < d:
        if due_d <= today:
            d = today + timedelta(days=1)
            note = f"마감({md(due_d)})이 이미 지나 내일({md(d)}) 다시 확인합니다."
        else:
            d = max(today + timedelta(days=1), due_d - timedelta(days=1))
            note = f"마감 {md(due_d)} 전에 확인하도록 {md(d)}로 당겼습니다."
    return {"date": d, "defaulted": True, "note": note, "default_days": n}


def recheck_ask(cfg, ref: str, status: str, plan: dict, now: datetime) -> dict:
    """날짜 없이 대기·보류를 고르면 반드시 언제까지 기다릴지 묻는다(기본값은 이미 적용). 날짜 버튼을 붙인다.
    버튼 라벨 `CLR-0031 10/2까지 대기`는 그대로 교수님 지시로 읽힌다(claire_store wait|hold --until)."""
    today = now.astimezone(tz_of(cfg)).date()
    verb = "대기" if status == "waiting" else "보류"
    d = plan["date"]
    choices = []
    for c in (today + timedelta(days=1), d, today + timedelta(days=7)):
        if c not in choices and c > today:
            choices.append(c)
    choices.sort()
    labels = [f"{ref} {md(c)}까지 {verb}" + (" (기본)" if c == d else "") for c in choices]
    text = (f"언제 다시 확인할까요? 지정하지 않으시면 {md(d)}({WEEKDAY_KO[d.weekday()]})에 확인하겠습니다."
            + (f" {plan['note']}" if plan.get("note") else ""))
    return {"text": text, "labels": labels,
            "components": {"reusable": True, "text": text,
                           "blocks": [{"type": "actions", "buttons": [{"label": l, "style": "secondary"} for l in labels]}]}}


# --------------------------------------------------------------------------
# 시간 종류 (R7)
# --------------------------------------------------------------------------

def time_kind(item, cfg=None) -> str | None:
    """appointment(실제 약속: 회의·수업·면담) · work(교수님이 잡은 작업 시간) · deadline_marker(마감 전 자동 표시 구간).
    0.6.1: 구간이 정확히 '마감 1시간 전~마감'이면 window_auto 표시가 없어도 자동 마감 표시로 본다 (0.5.0 전에 직접 넣은 구간 등)."""
    start = _get(item, "start_at")
    if item["kind"] == "meeting":
        return "appointment" if start else None
    if start and len(start) > 10:
        return "deadline_marker" if (_get(item, "window_auto") or ops.is_default_window(cfg, item)) else "work"
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
                    if time_kind(i, cfg) == "deadline_marker" and i["due_at"]), key=lambda x: x[0])
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
    blocks = [{"ref": r["ref"], "title": r["title"], "kind": time_kind(r, cfg), "span": _span(r["start_at"], r["end_at"])}
              for r in rows if time_kind(r, cfg) in ("appointment", "work")]
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


def tracking_since(conn, cfg) -> date | None:
    """노출·진행 확인 추적을 시작한 날 (스키마 v3 변환 시각)."""
    return local_date(cfg, meta_get(conn, "tracking_since"))


def check_plan(cfg, item, now: datetime, since: date | None = None) -> dict:
    """오늘 이 업무의 진행을 물어야 하는가. {due, reason, label, next_on}.
    답이 없다고 완료·취소·중요도 하향하지 않는다. 같은 날 보고받았으면 그날 다시 묻지 않는다.
    since(추적 시작일)를 주면, 그 전부터 있던 업무의 첫 확인을 주기 안에서 번호 순으로 나눈다(0.6.1) —
    업그레이드 첫날 밀린 업무 전체가 한꺼번에 '오늘 진행 확인'에 몰리고, 이후에도 같은 날끼리 묶여 반복되지 않게."""
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
        if nc is None:                           # 재확인일 없는 옛 기록(upkeep 이 채운다): 기본 N일 뒤
            nc = (local_date(cfg, item["updated_at"]) or today) + timedelta(days=default_wait_days(cfg, st))
        if nc and nc <= today:
            # 재확인일부터 답을 받을 때까지 매일 (무응답이어도 완료·숨김 없음)
            return {"due": True, "reason": "recheck", "label": REASON_KO["recheck"], "next_on": nc}
        due_d = local_date(cfg, item["due_at"])
        if due_d and today in (due_d - timedelta(days=1), due_d):
            # 재확인일이 마감보다 늦어도 마감 전날·당일에는 알린다 (대기가 마감을 늦추지 않는다)
            return {"due": True, "reason": "wait_due", "label": REASON_KO["wait_due"], "next_on": today}
        return {**out, "reason": "waiting" if st == "waiting" else "on_hold", "next_on": nc}
    if st == "captured":
        return {**out, "reason": "captured"}          # 신뢰도 낮은 참고 항목: 목록에는 있고 진행 질문은 하지 않는다
    ra = local_date(cfg, _get(item, "review_after"))
    if ra and ra > today:
        return {**out, "reason": "snoozed", "label": f"{md(ra)}부터 다시 확인", "next_on": ra}
    kind = item["kind"]
    start_d = local_date(cfg, item["start_at"]) if kind == "meeting" or time_kind(item, cfg) == "work" else None
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
    reported = local_date(cfg, _get(item, "last_reported_at"))
    created = local_date(cfg, item["created_at"])
    if since and asked is None and reported is None and created and created < since:
        nxt = add_days(since, (int(_get(item, "id") or 0) % n), skip) if n > 1 else since
    else:
        base = max(d for d in (asked, reported, created) if d)
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
            "time_kind": time_kind(it, cfg), "next_action": it["next_action"], "waiting_on": it["waiting_on"],
            "next_check_at": it["next_check_at"], "blocked_reason": it["blocked_reason"],
            "check": {**plan, "next_on": plan["next_on"].isoformat() if plan.get("next_on") else None},
            "external": _ext_note(conn, it), "project": it["project"], "short_label": _get(it, "short_label"),
            "last_detailed_at": _get(it, "last_detailed_at"), "created_at": it["created_at"],
            "all_day": it["all_day"], "due_precision": it["due_precision"]}


def agenda_data(conn, cfg, now: datetime, overlays: list[dict] | None = None) -> dict:
    """활성 미완료 업무 전체를 구역으로 나눈다. 한 업무는 주된 구역 한 곳에만 (중복 질문 방지).
    1 today   오늘 일정·마감·작업 예약 (+ 준비·후속 업무 상태, 실제 충돌, 마감 몰림)
    2 check   오늘 진행 확인 (기한 지남·지난 일정·내일·재확인일·매일/주기)
    3 open    그 외 진행 중·할 일·확인 필요·참고 (마감 없는 업무 포함) + 앞으로의 일정
    4 parked  답변 대기·보류 (다음 확인일 표시, 그 전에는 묻지 않는다)"""
    tz = tz_of(cfg)
    today = now.astimezone(tz).date()
    since = tracking_since(conn, cfg)
    rows = conn.execute("SELECT * FROM item WHERE deleted_at IS NULL AND status IN (%s) ORDER BY id"
                        % ",".join("?" * len(OPEN_STATUSES)), OPEN_STATUSES).fetchall()
    entries = {r["id"]: _entry(conn, cfg, r, check_plan(cfg, r, now, since)) for r in rows}
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
        e["series_key"] = r["series_key"]
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

    upcoming = []                                   # 앞으로의 단발 일정 — 모두 하나씩 (0.6.1: 기간 제한·건수 접기 없음)
    series_members: dict[str, list[dict]] = {}      # 앞으로의 정기 일정 — 시리즈마다 한 줄 (교수님 결정 2026-09-27)
    parked = {"waiting": [], "on_hold": []}
    groups = {"in_progress": [], "todo": [], "needs_info": [], "captured": []}
    for i, e in entries.items():
        if i in used:
            continue
        r = by_id[i]
        if r["status"] in ("waiting", "on_hold"):
            parked[r["status"]].append(e)
            continue
        if r["kind"] == "meeting" and r["start_at"] and local_date(cfg, r["start_at"]) > today \
                and r["status"] in ("todo", "in_progress"):
            e["when"] = ops.fmt_when(cfg, r["start_at"], r["end_at"], bool(r["all_day"]))
            if r["series_key"]:
                series_members.setdefault(r["series_key"], []).append(e)
            else:
                upcoming.append(e)
            continue
        groups.get(r["status"], groups["todo"]).append(e)
    series = []
    for key, members in series_members.items():
        members.sort(key=lambda e: e["start_at"] or "")
        if len(members) == 1:
            upcoming.append(members[0])             # 남은 회차가 하나면 단발 일정처럼
            continue
        series.append(_series_line(cfg, key, members))
    series.sort(key=lambda s: s["next"]["start_at"] or "")
    for g in groups.values():
        g.sort(key=lambda e: (0 if e["priority"] == "high" else 1, e["due_at"] or "9999", e["id"]))
    upcoming.sort(key=lambda e: e["start_at"] or "")
    for g in parked.values():
        g.sort(key=lambda e: (e["next_check_at"] or "9999", e["id"]))

    # 빠짐 검사 (R2): 모든 활성 업무가 어느 구역에 하나씩 있거나 시리즈 줄에 들어 있어야 한다
    listed = [e["id"] for e in today_list + check_list + upcoming] + \
             [e["id"] for g in list(groups.values()) + list(parked.values()) for e in g] + \
             [m["id"] for sl in series for m in sl["members"]]
    missing = sorted({r["id"] for r in rows} - set(listed))
    dup = sorted({i for i in listed if listed.count(i) > 1})
    coverage = {"active": len(rows), "listed": len(set(listed)), "in_series_lines": sum(sl["count"] for sl in series),
                "missing": [by_id[i]["ref"] for i in missing], "duplicated": [by_id[i]["ref"] for i in dup]}

    day = day_conflicts(conn, cfg, today, overlays)
    check_refs = [e["ref"] for e in today_list if e["check"]["due"]] + [e["ref"] for e in check_list]
    counts = {"active": len(rows), "today": len(today_list), "check": len(check_refs),
              "open": sum(len(g) for g in groups.values()), "upcoming": len(upcoming), "series": len(series),
              "series_occurrences": coverage["in_series_lines"],
              "waiting": len(parked["waiting"]), "on_hold": len(parked["on_hold"])}
    return {"today_date": today.isoformat(), "weekday": "월화수목금토일"[today.weekday()], "counts": counts,
            "today": today_list, "overlay": overlays or [], "overlaps": day["overlaps"],
            "deadline_clusters": day["deadline_clusters"], "check": check_list, "open": groups,
            "series": series, "upcoming": upcoming, "parked": parked, "check_refs": check_refs, "coverage": coverage}


WEEKDAY_KO = "월화수목금토일"


def _series_line(cfg, key: str, members: list[dict]) -> dict:
    """정기 일정 한 줄: 제목 · (매주) 요일 시각 · 다음 날짜 · 앞으로 N회. 버튼은 다음 회차."""
    tz = tz_of(cfg)
    starts = [parse_iso(m["start_at"]).astimezone(tz) for m in members if m["start_at"] and len(m["start_at"]) > 10]
    days = sorted({d.weekday() for d in starts})
    times = [d.strftime("%H:%M") for d in starts]
    common = max(set(times), key=times.count) if times else None
    weekly = False
    by_wd: dict[int, list] = {}
    for d in starts:
        by_wd.setdefault(d.weekday(), []).append(d.date())
    gaps = [(b - a).days for ds in by_wd.values() for a, b in zip(ds, ds[1:])]
    if gaps and max(set(gaps), key=gaps.count) == 7:
        weekly = True
    pattern = ("매주 " if weekly else "") + "·".join(WEEKDAY_KO[d] for d in days)
    if common:
        pattern += f" {common}" + (" 외" if len(set(times)) > 1 else "")
    nxt = members[0]
    nd = parse_iso(nxt["start_at"]).astimezone(tz) if nxt["start_at"] and len(nxt["start_at"]) > 10 else None
    next_text = (f"{nd.month}/{nd.day}({WEEKDAY_KO[nd.weekday()]}) {nd:%H:%M}" if nd
                 else ops.fmt_when(cfg, nxt["start_at"], nxt["end_at"]))
    return {"series_key": key, "title": nxt["title"], "pattern": pattern.strip(), "next": nxt, "next_text": next_text,
            "count": len(members), "members": members, "refs": [m["ref"] for m in members]}


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


def agenda_rows(cfg, data: dict) -> list[dict]:
    """업무 현황의 줄 목록. {"heading": 구역 제목} | {"text": 버튼 없는 줄} | {"text": 줄, "ref": 번호, "section": "1".."4"}.
    claire_buttons 가 이것으로 메시지를 만든다(1구역은 줄마다 오른쪽 버튼, 2~4구역은 5줄마다 번호 버튼 한 줄)."""
    c = data["counts"]
    rows: list[dict] = []

    def head(t):
        rows.append({"heading": t})

    def plain(t):
        rows.append({"text": t})

    def item(t, ref, sec):
        rows.append({"text": t, "ref": ref, "section": sec})

    # 1. 오늘
    head(f"**1. 오늘 일정·마감 ({c['today']})**")
    if not data["today"] and not data["overlay"]:
        plain("  없음")
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
        item(f"  {e['when']}  {e['title']} ({e['ref']})" + (f" · {' · '.join(extra)}" if extra else ""), e["ref"], "1")
    for o in data["overlay"]:
        when = "종일" if o.get("all_day") else (_hm(cfg, o["start"]) + (f"–{_hm(cfg, o['end'])}" if o.get("end") and len(o["end"]) > 10 else ""))
        plain(f"  {when}  ({o['calendar']}) {o['summary']}")
    for ov in data["overlaps"]:
        a = ov["a"] if not ov["a"].startswith("overlay:") else ov["a"].split(":", 1)[1]
        b = ov["b"] if not ov["b"].startswith("overlay:") else ov["b"].split(":", 1)[1]
        plain(f"  ⚠ 겹침: {ov['a_title']}({a})와 {ov['b_title']}({b})")
    for cl in data["deadline_clusters"]:
        span = cl["from"] if cl["from"] == cl["to"] else f"{cl['from']}–{cl['to']}"
        plain(f"  ⏱ {span} 마감 {cl['count']}건({'·'.join(cl['refs'])}) — 약속 충돌이 아니라 작업량 안내입니다")
    # 2. 진행 확인
    head(f"**2. 오늘 진행 확인 ({len(data['check'])})** — 번호를 누르면 완료·진행 중·보류·대기·처리 불필요")
    if not data["check"]:
        plain("  없음")
    for e in data["check"]:
        label = e["check"]["label"]
        if e["check"]["reason"] == "recheck":
            label += f" · {e['status_label']}" + (f"({e['waiting_on']})" if e["waiting_on"] else "")
        bits = _line_bits(e)
        item(f"  {label}  {e['title']} ({e['ref']})" + (f" · {bits}" if bits else ""), e["ref"], "2")
    # 3. 그 외
    n3 = c["open"] + c["upcoming"] + c["series"]
    head(f"**3. 그 외 활성 업무 ({c['open']}) · 앞으로의 일정 (단발 {c['upcoming']} · 정기 {c['series']}개 시리즈 "
         f"{c['series_occurrences']}회)**")
    if not n3:
        plain("  없음")
    for key, label in (("in_progress", "진행 중"), ("todo", "할 일"), ("needs_info", "확인 필요(질문 대기)"),
                       ("captured", "참고(신뢰도 낮음 — 업무인지 확인)")):
        g = data["open"][key]
        if not g:
            continue
        plain(f"  {label} {len(g)}")
        for e in g:
            nxt = e["check"].get("next_on")
            tail = _line_bits(e)
            if nxt and key in ("todo", "in_progress"):
                tail += f" · 다음 확인 {md(date.fromisoformat(nxt))}"
            if e["check"]["reason"] == "reported_today":
                tail += " · 오늘 보고받음"
            elif e["check"]["reason"] == "snoozed":
                tail += f" · {e['check']['label']}"
            item(f"  {e['title']} ({e['ref']})" + (f" · {tail.lstrip(' ·')}" if tail.strip(' ·') else ""), e["ref"], "3")
    if data["series"]:
        plain(f"  정기 일정 {len(data['series'])} (시리즈마다 한 줄 — 버튼은 다음 회차)")
        for sl in data["series"]:
            item(f"  {sl['title']} · {sl['pattern']} · 다음 {sl['next_text']} ({sl['next']['ref']}) · 앞으로 {sl['count']}회",
                 sl["next"]["ref"], "3")
    if data["upcoming"]:
        plain(f"  앞으로의 단발 일정 {len(data['upcoming'])}")
        for e in data["upcoming"]:
            item(f"  {e['when']}  {e['title']} ({e['ref']})", e["ref"], "3")
    # 4. 대기·보류
    parked = data["parked"]
    n4 = len(parked["waiting"]) + len(parked["on_hold"])
    head(f"**4. 답변 대기·보류 ({n4})** — 다음 확인일 전에는 묻지 않습니다")
    if not n4:
        plain("  없음")
    for key in ("waiting", "on_hold"):
        for e in parked[key]:
            nc = local_date(cfg, e["next_check_at"])
            who = f"{e['waiting_on']} 답변 대기" if key == "waiting" else "보류" + (f"({e['blocked_reason']})" if e["blocked_reason"] else "")
            item(f"  {e['title']} ({e['ref']}) · {who} · 다음 확인 {md(nc) if nc else '미정'}"
                 + (f" · {e['due_text']}" if e["due_at"] else ""), e["ref"], "4")
    if data["coverage"]["missing"] or data["coverage"]["duplicated"]:
        plain(f"  ⚠ 목록 검사: 빠짐 {data['coverage']['missing']} · 중복 {data['coverage']['duplicated']} (도구 오류 — 알려 주세요)")
    return rows


def agenda_header(data: dict) -> str:
    c = data["counts"]
    d = date.fromisoformat(data["today_date"])
    return f"[업무 현황 {md(d)}({data['weekday']})] 활성 업무 {c['active']}건 · 오늘 확인 {c['check']}건"


def agenda_text(cfg, data: dict) -> str:
    """업무 현황 전체 본문 (번호 복사 블록 포함). 미리 보기·버튼을 못 쓸 때의 본문."""
    lines = [agenda_header(data)]
    for r in agenda_rows(cfg, data):
        lines.append(r.get("heading") or r["text"])
        if r.get("ref"):
            lines.append(_block(r["ref"]))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 아침 보고 (0.7.0, ISSUE 2026-09-29) — 전체는 추적하고 보고에는 필요한 만큼만
# --------------------------------------------------------------------------

PRI_SCORE = {"high": 300, "normal": 200, "low": 100}
REPORT_NAV = {"overdue": "기한 지남 전체 보기", "progress": "진행 확인 전체 보기", "past": "지난 일정 보기",
              "waiting": "대기·보류 보기", "done": "완료 내역 보기", "questions": "질문 전체 보기",
              "approvals": "승인 대기 보기", "full": "전체 목록"}


def short_num(ref: str) -> str:
    """CLR-0764 → 764 (교수님이 짧게 적으면 도구가 CLR-0764 로 읽는다)."""
    m = re.search(r"(\d+)$", ref or "")
    return str(int(m.group(1))) if m else ref


# 키워드로 쓰면 뜻이 없는 낱말 (0.7.1 점검: '내 연구 및', '내가 맡은 업무에', 'pilwon'). 제목 앞부분 대체값에서만 뺀다.
LABEL_STOP = {"내", "내가", "나의", "제", "제가", "저의", "우리", "저희", "및", "등", "관련", "관련한", "대한", "대해", "위한", "건",
              "요청", "요청의", "안내", "안내의", "문의", "공지", "알림", "참고", "맡은", "해야", "할", "하는", "있는", "the", "a", "an",
              "re:", "fw:", "fwd:", "re", "fw", "fwd", "for", "of", "to", "and", "on", "in", "교수님", "교수", "님"}


LABEL_TAGS = {"마감", "회신", "요청", "안내", "공지", "긴급", "중요", "참고", "필독", "재공지", "re", "fw", "fwd", "ext", "외부"}


def _label_words(title: str, stop_extra: set[str]) -> list[str]:
    t = re.sub(r"^\s*(?:(?:re|fwd?|회신|전달)\s*[:：]\s*)+", "", title or "", flags=re.I)
    # [IROS] 같은 꼬리표는 낱말로 살리고 [마감]·(회신)·[공지] 같은 일반 꼬리표는 뺀다
    t = re.sub(r"[\[(（【]([^\])）】]{0,12})[\])）】]",
               lambda m: " " if m.group(1).strip().lower() in LABEL_TAGS else f" {m.group(1)} ", t)
    words = []
    for w in re.split(r"[\s/,·|]+", t):
        w = w.strip(" -_:;.'\"“”‘’")
        base = re.sub(r"(에게|에서|으로|에|의|을|를|은|는|이|가|과|와|도|로)$", "", w) if len(w) > 2 else w
        if not w or w.lower() in LABEL_STOP or base.lower() in LABEL_STOP or w.lower() in stop_extra or w.isdigit():
            continue
        words.append(w)
    return words


def auto_label(title: str, stop_extra: set[str] | None = None) -> str:
    """키워드가 없을 때 제목에서 1~3단어를 고른다. 앞의 [마감]·Re:·(회신) 같은 꼬리표와 뜻 없는 낱말(내·및·관련·요청·교수님·
    본인 이름)은 뺀다. 판단이 필요한 좋은 이름은 Claire 가 short_label 로 적는다(labels_needed)."""
    words = _label_words(title, stop_extra or set())
    t = " ".join(words) or (title or "").strip()
    out: list[str] = []
    for w in (words or t.split()):
        if out and len(" ".join(out + [w])) > 14:
            break
        out.append(w)
        if len(out) == 3:
            break
    label = " ".join(out) or t
    return label if len(label) <= 16 else label[:15] + "…"


def label_stopwords(cfg) -> set[str]:
    """교수님 이름·주소 앞부분 (pilwon·hur 등). 키워드 대체값에서 뺀다."""
    out = set()
    for a in (cfg.get("gmail", {}).get("user_addresses") or []):
        local = a.split("@", 1)[0].lower()
        out.add(local)
        out.update(x for x in re.split(r"[._\-+]", local) if len(x) >= 2)
    return out


def check_label(label: str | None) -> str | None:
    """Claire 가 적는 키워드 검사: 1~3단어, 20자 이내, 뜻 없는 낱말만으로 된 것이 아닐 것. 문제가 있으면 이유."""
    v = re.sub(r"\s+", " ", str(label or "")).strip()
    if not v:
        return "비어 있음"
    if len(v.split()) > 3:
        return "3단어 초과"
    if len(v) > 20:
        return "20자 초과"
    if not _label_words(v, set()):
        return "뜻 없는 낱말뿐 (내·및·관련·요청 등)"
    return None


def keyword_of(e, stop_extra: set[str] | None = None) -> str:
    return f"{e.get('short_label') or auto_label(e['title'], stop_extra)}({short_num(e['ref'])})"


def detail_score(cfg, e, today: date) -> int:
    """상세(버튼)로 보일 업무 고르기: 중요도 → 직접 요청 → 내일·재확인·마감 임박 → 최근에 기한이 지난 것 →
    오래 상세로 보이지 않은 것(같은 업무만 반복되지 않게, 최대 10일분). 결정론적."""
    s = PRI_SCORE.get(e["priority"], 200)
    if e.get("request_scope") == "direct":
        s += 60
    s += {"d_minus_1": 120, "wait_due": 120, "recheck": 90, "d_day": 150}.get(e["check"].get("reason"), 0)
    due_d = local_date(cfg, e["due_at"])
    if due_d and due_d < today:
        s += max(0, 30 - (today - due_d).days) * 2
    last = local_date(cfg, e.get("last_detailed_at")) or local_date(cfg, e.get("created_at"))
    s += min((today - last).days if last else 10, 10) * 8
    return s


def _rank(cfg, entries: list[dict], today: date) -> list[dict]:
    for e in entries:
        e["score"] = detail_score(cfg, e, today)
    return sorted(entries, key=lambda e: (-e["score"], e["due_at"] or "9999", e["id"]))


def keyword_block(entries: list[dict], budget: int, stop_extra: set[str] | None = None) -> dict:
    """키워드 요약: 분야(project)별로 묶고 글자 수 상한을 넘으면 묶음마다 "외 N건". 언급한 업무 번호(mentions)와
    줄인 업무(omitted)를 돌려준다 — 줄인 업무도 추적은 그대로이고 "전체 보기"로 모두 볼 수 있다."""
    groups: dict[str, list[dict]] = {}
    for e in entries:
        groups.setdefault((e.get("project") or "").strip() or "기타", []).append(e)
    named = len(groups) > 1 or "기타" not in groups
    lines, mentions, omitted = [], [], []
    used = 0
    # 분야 묶음은 중요한 업무가 든 순서(입력이 점수 순), "기타"는 맨 뒤
    for g, members in sorted(groups.items(), key=lambda kv: kv[0] == "기타"):
        head = f"  {g}: " if named else "  "
        words = []
        for e in members:
            k = keyword_of(e, stop_extra)
            if used + len(head) + len(k) + 3 > budget and words:
                break
            if used + len(head) + len(k) + 3 > budget:
                break
            words.append(k)
            used += len(k) + 3
            mentions.append(e["ref"])
        rest = members[len(words):]
        omitted += [e["ref"] for e in rest]
        if words:
            lines.append(head + " · ".join(words) + (f" 외 {len(rest)}건" if rest else ""))
            used += len(head)
        elif rest:
            lines.append(f"{head}{len(rest)}건")
            used += len(head) + 4
    return {"lines": lines, "mentions": mentions, "omitted": omitted}


def _today_when(cfg, r, tk) -> tuple[str, str]:
    """1구역 한 줄의 시간 표기와 정렬 키. 작업 예약과 실제 마감을 한 항목에서 구분한다."""
    if r["kind"] == "meeting":
        when = "종일" if r["all_day"] else _hm(cfg, r["start_at"]) + (
            f"–{_hm(cfg, r['end_at'])}" if r["end_at"] and len(r["end_at"]) > 10 else "")
        return when, r["start_at"] or ""
    due_today = r["due_at"] and local_date(cfg, r["due_at"]) == local_date(cfg, r["start_at"])
    if tk == "deadline_marker":
        return (f"{_hm(cfg, r['due_at'])} 마감" if r["due_at"] else _hm(cfg, r["start_at"])), r["start_at"]
    work = _hm(cfg, r["start_at"]) + (f"–{_hm(cfg, r['end_at'])}" if r["end_at"] and len(r["end_at"]) > 10 else "") + " 작업"
    if r["due_at"]:
        if due_today and r["due_precision"] == "time":
            work += f" · {_hm(cfg, r['due_at'])} 마감"
        elif due_today:
            work += " · 오늘 마감"
        else:
            work += f" · 마감 {_due_text(cfg, r).replace(' 마감', '')}"
    return work, r["start_at"]


def pending_note_asks(conn, cfg, now: datetime, limit: int = 1) -> list[dict]:
    """처음 보는 프로젝트의 Tasks 문서 위치 질문 (한 번 묻고, 답이 없으면 7일 뒤 한 번 더. 그 뒤로는 수집함에 두고 묻지 않는다)."""
    out = []
    week_ago = (now - timedelta(days=7)).isoformat()
    for r in conn.execute("SELECT * FROM note_map WHERE status='pending' AND asked_count < 2 AND "
                          "(last_asked_at IS NULL OR last_asked_at <= ?) ORDER BY created_at LIMIT ?", (week_ago, limit)):
        out.append({"project": r["project"], "candidates": json.loads(r["candidates"] or "[]"),
                    "asked_count": r["asked_count"]})
    return out


def report_data(conn, cfg, now: datetime, overlays: list[dict] | None = None) -> dict:
    """아침 보고 재료. 활성 업무를 한 곳에만 넣고(accounting 합 = 활성 수) 표시할 만큼만 고른다.
    today_cal(오늘 달력) · today_due(달력 밖 오늘 마감) · overdue(기한 지남: 상세 10~15 + 키워드) ·
    progress(기한 전·마감 없는 업무 중 오늘 확인 차례: 두 쪽) · 숨김(지난 일정·앞으로의 일정·대기·보류·확인 차례 아님)."""
    tz = tz_of(cfg)
    rp = {**{"overdue_detail": 10, "overdue_detail_max": 15, "progress_messages": 2, "progress_detail_per_message": 15,
             "keyword_chars": 1100, "questions_max": 3, "done_hours": 24}, **(cfg.get("report") or {})}
    today = now.astimezone(tz).date()
    since = tracking_since(conn, cfg)
    rows = conn.execute("SELECT * FROM item WHERE deleted_at IS NULL AND status IN (%s) ORDER BY id"
                        % ",".join("?" * len(OPEN_STATUSES)), OPEN_STATUSES).fetchall()
    b = {k: [] for k in ("today_cal", "today_due", "overdue", "progress", "past_meetings", "parked", "upcoming",
                         "scheduled", "snoozed", "captured")}
    by_id = {}
    for r in rows:
        plan = check_plan(cfg, r, now, since)
        e = _entry(conn, cfg, r, plan)
        by_id[r["id"]] = e
        tk = e["time_kind"]
        start_d = local_date(cfg, r["start_at"])
        due_d = local_date(cfg, r["due_at"])
        if (r["kind"] == "meeting" and start_d == today) or (tk in ("work", "deadline_marker") and start_d == today):
            e["when"], e["sort"] = _today_when(cfg, r, tk)
            if r["kind"] == "meeting":
                e["preps"] = [{"ref": x["ref"], "title": x["title"], "status_label": ops.status_label(x)} for x in conn.execute(
                    "SELECT i.* FROM relation rl JOIN item i ON i.id=rl.from_item_id WHERE rl.rel_type='prep_for' "
                    "AND rl.to_item_id=? AND i.deleted_at IS NULL ORDER BY i.id", (r["id"],))]
            b["today_cal"].append(e)
            continue
        if r["kind"] != "meeting" and due_d == today and parse_iso(r["due_at"]) >= now:
            e["when"] = f"{_hm(cfg, r['due_at'])} 마감" if r["due_precision"] == "time" else "오늘 마감"
            e["sort"] = r["due_at"]
            b["today_due"].append(e)
            continue
        if r["kind"] == "meeting" and plan["reason"] == "meeting_passed":
            b["past_meetings"].append(e)           # 기본 보고에서 뺀다. 시간이 지났다고 완료로 보지 않는다
            continue
        if r["status"] == "captured":
            b["captured"].append(e)
            continue
        if r["status"] in ("waiting", "on_hold") and not plan["due"]:
            b["parked"].append(e)                  # 재확인일 전(오늘 보고받은 것 포함): 독촉하지 않는다
            continue
        if plan["reason"] == "snoozed":
            b["snoozed"].append(e)
            continue
        if r["kind"] != "meeting" and r["due_at"] and parse_iso(r["due_at"]) < now:
            b["overdue"].append(e)
            continue
        if plan["due"]:
            b["progress"].append(e)
            continue
        if r["kind"] == "meeting":
            b["upcoming"].append(e)
            continue
        b["scheduled"].append(e)
    b["today_cal"].sort(key=lambda e: e["sort"] or "")
    b["today_due"].sort(key=lambda e: e["sort"] or "")

    # 기한 지남: 한 쪽. 상세 기본 10건, 높음이 더 많으면 15건까지. 15건 이하면 모두 상세.
    od = _rank(cfg, b["overdue"], today)
    n_high = sum(1 for e in od if e["priority"] == "high")
    n_od = len(od) if len(od) <= rp["overdue_detail_max"] else max(rp["overdue_detail"], min(n_high, rp["overdue_detail_max"]))
    stop = label_stopwords(cfg)
    overdue = {"total": len(od), "detail": od[:n_od], "rest": od[n_od:],
               "keywords": keyword_block(od[n_od:], rp["keyword_chars"], stop),
               "parked_overdue": sum(1 for e in b["parked"] if e["due_at"] and parse_iso(e["due_at"]) < now)}
    # 진행 확인: 최대 두 쪽. 쪽마다 상세 15건, 넘치면 마지막 쪽에 키워드.
    pg = _rank(cfg, b["progress"], today)
    cap = rp["progress_messages"] * rp["progress_detail_per_message"]
    progress = {"total": len(pg), "detail": pg[:cap], "rest": pg[cap:],
                "keywords": keyword_block(pg[cap:], rp["keyword_chars"], stop),
                "per_message": rp["progress_detail_per_message"]}

    # 질문: 기본 3건 (나머지는 계속 추적). 마감이 가까운 업무의 질문 먼저.
    qs = []
    for q in conn.execute("SELECT q.*, i.ref AS item_ref, i.title AS item_title, i.due_at AS item_due FROM question q "
                          "LEFT JOIN item i ON i.id=q.item_id WHERE q.status='open' AND (q.next_ask_at IS NULL OR q.next_ask_at <= ?) "
                          "ORDER BY (i.due_at IS NULL), i.due_at, q.id", (now.isoformat(),)):
        qs.append({"ref": q["ref"], "item_ref": q["item_ref"], "item_title": q["item_title"], "question": q["question"],
                   "options": json.loads(q["options"]) if q["options"] else None})
    questions = {"total": len(qs), "shown": qs[:rp["questions_max"]]}

    done_since = (now - timedelta(hours=float(rp["done_hours"]))).isoformat()
    done_refs = [r["ref"] for r in conn.execute(
        "SELECT DISTINCT i.ref FROM activity_log a JOIN item i ON i.id=a.item_id WHERE a.action='complete' "
        "AND a.undone_at IS NULL AND a.created_at >= ? AND i.status='done' AND i.deleted_at IS NULL ORDER BY i.id", (done_since,))]
    day = day_conflicts(conn, cfg, today, overlays)
    check_refs = ([e["ref"] for e in b["today_cal"] + b["today_due"] if e["check"]["due"]]
                  + [e["ref"] for e in overdue["detail"] + progress["detail"]])
    accounting = {k: len(v) for k, v in b.items()}
    accounting["active"] = len(rows)
    accounting["ok"] = sum(len(v) for v in b.values()) == len(rows)
    counts = {"active": len(rows), "today": len(b["today_cal"]), "today_due": len(b["today_due"]),
              "overdue": len(od), "progress": len(pg), "check": len(check_refs),
              "parked": len(b["parked"]), "past_meetings": len(b["past_meetings"]), "done": len(done_refs),
              "upcoming": len(b["upcoming"]), "scheduled": len(b["scheduled"]) + len(b["snoozed"]) + len(b["captured"])}
    # 0.7.1: 이번 보고에서 키워드로 보일 업무 중 이름(short_label)이 없는 것 — Claire 가 보내기 전에 labels 로 적는다
    shown = set(overdue["keywords"]["mentions"]) | set(progress["keywords"]["mentions"])
    labels_needed = [{"ref": e["ref"], "title": e["title"], "project": e.get("project"),
                      "auto": auto_label(e["title"], stop)}
                     for e in od[n_od:] + pg[cap:] if e["ref"] in shown and not e.get("short_label")]
    return {"today_date": today.isoformat(), "weekday": WEEKDAY_KO[today.weekday()], "counts": counts,
            "labels_needed": labels_needed,
            "today_cal": b["today_cal"], "today_due": b["today_due"], "overlay": overlays or [],
            "overlaps": day["overlaps"], "deadline_clusters": day["deadline_clusters"],
            "overdue": overdue, "progress": progress, "questions": questions, "done_refs": done_refs,
            "past_meetings": b["past_meetings"], "parked": b["parked"], "upcoming": b["upcoming"],
            "note_asks": pending_note_asks(conn, cfg, now), "check_refs": check_refs, "accounting": accounting}


def _title(e, n: int = 48) -> str:
    t = e["title"]
    return t if len(t) <= n else t[:n - 1] + "…"


def _detail_line(cfg, e, today: date, overdue: bool = False) -> str:
    r = e["check"].get("reason")
    if overdue:
        due_d = local_date(cfg, e["due_at"])
        head = f"{md(due_d)} 마감({(today - due_d).days}일 지남)" if due_d < today else "오늘 마감 지남"
        if r == "recheck":
            head += " · 재확인일" + (f"·{e['waiting_on']}" if e.get("waiting_on") else "")
    elif r in ("recheck", "wait_due"):
        head = e["check"]["label"] + (f"·{e['waiting_on']}" if e.get("waiting_on") else "")
    else:
        head = e["check"].get("label") or ""
    bits = []
    if not overdue and e["due_at"]:
        bits.append(e["due_text"])
    elif not e["due_at"]:
        bits.append("기한 없음")
    if e["priority"] == "high":
        bits.append("중요")
    if e["request_scope"] == "direct":
        bits.append("직접 요청")
    if e["next_action"]:
        bits.append(f"다음: {e['next_action'][:30]}")
    if e["external"]:
        bits.append(e["external"])
    return f"  {head}  {_title(e)} ({e['ref']})" + (f" · {' · '.join(bits)}" if bits else "")


def report_pages(cfg, data: dict, approvals: dict | None = None, health_lines: list[str] | None = None) -> list[dict]:
    """아침 보고의 쪽 목록. 쪽 = {"key", "rows", "mentions", "question_refs"}. rows 는 agenda_rows 와 같은 모양에
    {"nav": [버튼 라벨…]}(전체 보기 버튼 줄)이 더해진다. today 는 길면 여러 메시지, 나머지 쪽은 메시지 하나."""
    c = data["counts"]
    today = date.fromisoformat(data["today_date"])
    pages = []
    # 1. 오늘
    rows: list[dict] = []
    rows.append({"heading": f"**1. 오늘 일정 ({c['today']})**"})
    if not data["today_cal"] and not data["overlay"]:
        rows.append({"text": "  없음"})
    for e in data["today_cal"]:
        extra = [f"준비: {p['title']}({p['ref']} {p['status_label']})" for p in e.get("preps", [])]
        if e["kind"] != "meeting" and e["priority"] == "high":
            extra.append("중요")
        rows.append({"text": f"  {e['when']}  {_title(e)} ({e['ref']})" + (f" · {' · '.join(extra)}" if extra else ""),
                     "ref": e["ref"], "section": "1"})
    for o in data["overlay"]:
        when = "종일" if o.get("all_day") else (_hm(cfg, o["start"]) + (f"–{_hm(cfg, o['end'])}" if o.get("end") and len(o["end"]) > 10 else ""))
        rows.append({"text": f"  {when}  ({o['calendar']}) {o['summary']}"})
    for ov in data["overlaps"]:
        a = ov["a"].split(":", 1)[1] if ov["a"].startswith("overlay:") else ov["a"]
        bb = ov["b"].split(":", 1)[1] if ov["b"].startswith("overlay:") else ov["b"]
        rows.append({"text": f"  ⚠ 겹침: {ov['a_title']}({a})와 {ov['b_title']}({bb})"})
    for cl in data["deadline_clusters"]:
        span = cl["from"] if cl["from"] == cl["to"] else f"{cl['from']}–{cl['to']}"
        rows.append({"text": f"  ⏱ {span} 마감 {cl['count']}건 — 약속 충돌이 아니라 작업량 안내입니다"})
    rows.append({"heading": f"**2. 오늘 마감 — 달력 밖 ({c['today_due']})**"})
    if not data["today_due"]:
        rows.append({"text": "  없음"})
    for e in data["today_due"]:
        bits = [x for x in ("중요" if e["priority"] == "high" else "", "직접 요청" if e["request_scope"] == "direct" else "",
                            f"다음: {e['next_action'][:30]}" if e["next_action"] else "", e["external"]) if x]
        rows.append({"text": f"  {e['when']}  {_title(e)} ({e['ref']})" + (f" · {' · '.join(bits)}" if bits else ""),
                     "ref": e["ref"], "section": "1"})
    pages.append({"key": "today", "rows": rows, "mentions": [], "question_refs": []})

    empty = []
    # 3. 기한 지남 (한 쪽) — 없으면 쪽을 만들지 않고 상태 쪽에 한 줄
    od = data["overdue"]
    if not od["total"]:
        empty.append("기한 지남 없음" + (f"(답변 대기·보류 {od['parked_overdue']}건은 재확인일까지 따로)" if od["parked_overdue"] else ""))
    rows = [{"heading": f"**3. 기한 지남 {od['total']}건**" + (f" (답변 대기·보류 {od['parked_overdue']}건은 재확인일까지 따로)"
                                                            if od["parked_overdue"] else "")}]
    if not od["total"]:
        rows.append({"text": "  없음"})
    for e in od["detail"]:
        rows.append({"text": _detail_line(cfg, e, today, overdue=True), "ref": e["ref"], "section": "3"})
    if od["rest"]:
        rows.append({"text": f"  그 밖 {len(od['rest'])}건 — 번호(예: {short_num(od['rest'][0]['ref'])})를 적으면 처리 카드"})
        rows += [{"text": ln} for ln in od["keywords"]["lines"]]
        rows.append({"nav": [REPORT_NAV["overdue"]]})
    if od["total"]:
        pages.append({"key": "overdue", "rows": rows, "mentions": od["keywords"]["mentions"], "question_refs": []})

    # 4. 진행 확인 (최대 두 쪽)
    pg = data["progress"]
    per = pg["per_message"]
    chunks = [pg["detail"][i:i + per] for i in range(0, len(pg["detail"]), per)]
    if not pg["total"]:
        empty.append("오늘 진행 확인할 차례인 업무 없음")
    for i, chunk in enumerate(chunks):
        head = f"**4. 진행 확인 {pg['total']}건** — 기한 전·마감 없는 업무 중 오늘 확인할 차례" if i == 0 else \
            f"**4. 진행 확인 (계속 {i + 1}/{len(chunks)})**"
        rows = [{"heading": head}]
        if not pg["total"]:
            rows.append({"text": "  없음"})
        for e in chunk:
            rows.append({"text": _detail_line(cfg, e, today), "ref": e["ref"], "section": "4"})
        mentions = []
        if i == len(chunks) - 1 and pg["rest"]:
            rows.append({"text": f"  그 밖 {len(pg['rest'])}건 — 다음 보고에 차례로 올립니다"})
            rows += [{"text": ln} for ln in pg["keywords"]["lines"]]
            rows.append({"nav": [REPORT_NAV["progress"]]})
            mentions = pg["keywords"]["mentions"]
        pages.append({"key": f"progress{i + 1}", "rows": rows, "mentions": mentions, "question_refs": []})

    # 5. 확인·상태
    rows = []
    qd = data["questions"]
    qrefs = []
    if qd["shown"] or data["note_asks"]:
        rows.append({"heading": f"**5. 확인 필요 ({qd['total'] + len(data['note_asks'])})**"})
    for q in qd["shown"]:
        opts = f" 후보 {' / '.join(str(o) for o in q['options'])}" if q.get("options") else ""
        about = f"({q['item_ref']} {q['item_title'][:24]}) " if q.get("item_ref") else ""
        rows.append({"text": f"  {q['ref']} {about}{q['question']}{opts}", "ref": q["ref"], "section": "5"})
        qrefs.append(q["ref"])
    if qd["total"] > len(qd["shown"]):
        rows.append({"text": f"  질문 {qd['total'] - len(qd['shown'])}건은 다음 보고에 (계속 추적)"})
        rows.append({"nav": [REPORT_NAV["questions"]]})
    for na in data["note_asks"]:
        names = [cand["name"] for cand in na["candidates"][:3]]
        rows.append({"text": f"  📁 '{na['project']}' 업무의 Tasks 를 어느 문서에 둘까요? 지금은 임시 수집 노트에 있습니다."
                             + (" 후보: " + " / ".join(names) if names else " (찾은 후보 없음 — 문서 이름을 적어 주세요)")})
        rows.append({"nav": [f"{na['project']} → {n}" for n in names] + [f"{na['project']} → 수집함"]})
        qrefs.append(f"NOTE:{na['project']}")
    rows.append({"heading": "**6. 상태**"})
    if empty:
        rows.append({"text": "  " + " · ".join(empty)})
    status = [f"완료 반영 {c['done']}건"]
    nav = []
    if c["done"]:
        nav.append(REPORT_NAV["done"])
    ap = approvals or {}
    if ap.get("calendar") or ap.get("tasks"):
        status.append("승인 대기 " + " · ".join(x for x in (f"달력 {ap['calendar']}건" if ap.get("calendar") else "",
                                                          f"Tasks {ap['tasks']}건" if ap.get("tasks") else "") if x))
        nav.append(REPORT_NAV["approvals"])
    if c["past_meetings"]:
        status.append(f"지난 일정 {c['past_meetings']}건(완료 체크 목록에서 뺌 · 기록 유지)")
        nav.append(REPORT_NAV["past"])
    if c["parked"]:
        status.append(f"답변 대기·보류 {c['parked']}건(재확인일 전)")
        nav.append(REPORT_NAV["waiting"])
    hidden = c["upcoming"] + c["scheduled"]
    if hidden:
        status.append(f"그 밖 추적 중 {hidden}건(앞으로의 일정·확인 차례 아님)")
    rows.append({"text": "  " + " · ".join(status)})
    for ln in health_lines or ["  운영: 수집·반영·전달 정상"]:
        rows.append({"text": ln})
    nav.append(REPORT_NAV["full"])
    rows.append({"nav": nav[:5]})
    pages.append({"key": "status", "rows": rows, "mentions": [], "question_refs": qrefs})
    return pages


def report_header(data: dict) -> str:
    c = data["counts"]
    d = date.fromisoformat(data["today_date"])
    return (f"[아침 보고 {md(d)}({data['weekday']})] 활성 {c['active']}건 · 오늘 일정 {c['today']} · 오늘 마감 {c['today_due']} · "
            f"기한 지남 {c['overdue']} · 진행 확인 {c['progress']}")


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
        plan = check_plan(cfg, it, now, since_d)
        # 0.7.0: 기본 보고에서 일부러 빼는 것(재확인일 전 대기·보류, 다시 물을 날 전, 지난 일정, 앞으로의 일정)은 누락이 아니다
        if it["priority"] == "high" and plan["reason"] not in ("waiting", "on_hold", "snoozed", "meeting_passed",
                                                                "upcoming", "captured"):
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
            # 첫 확인을 나눈 기존 업무는 한 주기를 더 준다 (나눈 날까지 기다린 것을 누락으로 보지 않는다)
            late = (today - add_days(base, n + (n - 1 if since_d and base == since_d else 0),
                                     it["priority"] in tr.get("skip_weekends_for", ["normal", "low"]))).days
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
        "SELECT ref FROM item WHERE deleted_at IS NULL AND (priority_source='unreviewed' OR priority_source IS NULL) "
        "AND status IN (%s) ORDER BY id" % ",".join("?" * len(OPEN_STATUSES)), OPEN_STATUSES)]
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
    gaps = h["exposure_gaps"]
    for g in [g for g in gaps if g["why"] == "not_delivered"][:5]:
        lines.append(f"  누락 감지: {g['title']} ({g['ref']}) — {g['message']}")
    nd = sum(1 for g in gaps if g["why"] == "not_delivered")
    if nd > 5:
        lines.append(f"  누락 감지 외 {nd - 5}건")
    late = [g for g in gaps if g["why"] == "check_missed"]
    if late:
        lines.append(f"  진행 확인 늦음 {len(late)}건 ({', '.join(g['ref'] for g in late[:5])}"
                     + (f" 외 {len(late) - 5}건" if len(late) > 5 else "") + ") — 차례대로 보고에 올립니다")
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
        if m.get("question_refs") is not None:
            page_qs = list(m["question_refs"])        # 0.7.0: 보고가 직접 준 질문 번호(+ NOTE:프로젝트 문서 위치 질문)
        else:
            page_qs = [q for q in re.findall(r"\bQ-\d+\b", text) if q in qs] if qs else re.findall(r"\bQ-\d+\b", text)
        items = [n for n in nums if n.startswith("CLR-")] or ([] if m.get("mentions") is not None
                                                            else re.findall(r"\bCLR-\d+\b", text))
        payload = {"message": text, "components": m.get("components")}
        did = conn.execute(
            "INSERT INTO delivery(report_key,kind,page,pages,item_refs,check_refs,question_refs,mention_refs,payload,"
            "content_hash,problems,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?, 'generated', ?)",
            (report_key, kind, i, len(messages), _marks(items), _marks(r for r in items if r in checks), _marks(page_qs),
             _marks(m.get("mentions") or []),
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
            mentions = json.loads(_get(r, "mention_refs") or "[]")
            if items:
                # 상세(번호 버튼)로 보인 업무: 보여 줌 + 상세 노출 시각(다음 보고의 돌려 가며 고르기 기준)
                conn.execute("UPDATE item SET last_exposed_at=?, last_detailed_at=? WHERE ref IN (%s)"
                             % ",".join("?" * len(items)), (ts, ts, *items))
                exposed.update(items)
            if mentions:
                # 키워드로만 언급된 업무: 보여 줌(누락 아님). 진행 확인을 물은 것으로는 치지 않는다
                conn.execute("UPDATE item SET last_exposed_at=? WHERE ref IN (%s)" % ",".join("?" * len(mentions)),
                             (ts, *mentions))
                exposed.update(mentions)
            for ref in checks:
                cur = conn.execute("UPDATE item SET last_asked_at=? WHERE ref=? AND (last_asked_at IS NULL OR "
                                   "substr(last_asked_at,1,10) <> ?)", (ts, ref, ts[:10])).rowcount
                if cur:
                    asked.add(ref)
            qrefs = json.loads(r["question_refs"] or "[]")
            qs += mark_questions_asked(conn, cfg, [q for q in qrefs if not q.startswith("NOTE:")], ts)
            for q in qrefs:
                if q.startswith("NOTE:"):             # 0.7.0: 프로젝트 문서 위치 질문을 실제로 물었다
                    conn.execute("UPDATE note_map SET asked_count=asked_count+1, last_asked_at=?, updated_at=? "
                                 "WHERE project=? AND status='pending' AND (last_asked_at IS NULL OR substr(last_asked_at,1,10)<>?)",
                                 (ts, ts, q[5:], ts[:10]))
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
