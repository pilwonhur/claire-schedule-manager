"""claire_policy — 정기 일정 종료 후 추적 제외 (0.10.0, ISSUE 2026-11-15 `Issue_정기일정_종료후_추적제외`).

원칙: **일정의 종료와 업무의 완료를 나눈다.** 참석형 정기 일정(수업·개인미팅·과제미팅)의 회차는 원본 종료 시각이 지나면
확인 없이 추적에서 빠진다. 그것은 완료·취소·처리 불필요가 아니다 — 상태(status)는 그대로 두고, 조회할 때마다
"정책 + 원본 종료 시각 + 지금"으로 계산한다(저장형 종료 상태를 두지 않는다). 그래서
  - 회차가 미래로 옮겨지면(Calendar 원본) 다음 조회부터 다시 안내 대상이다.
  - 교수님이 완료·보류·대기·취소로 바꾼 회차는 그 상태가 이긴다(시간 계산이 덮지 않는다).
  - 재수집·반복 실행이 아무것도 되살리거나 새로 만들지 않는다(상태를 바꾼 적이 없으므로).
  - 되돌리기 = 정책을 지우면 이전과 똑같은 집계로 돌아온다. 원본 일정·완료 이력은 건드린 적이 없다.

정책은 시리즈(series_key = Calendar recurringEventId)마다 한 번 정한다(series_policy 표). 회차 예외는 item.track_policy.
  after_end  : 회차 종료 후 추적 제외 (참석형 정기 일정)
  until_done : 완료 확인까지 추적 (정책이 없을 때와 같다 — 반복 결제·제출·회신 같은 수행형)

자동 제외의 구조 조건(모두 만족해야 한다. 아니면 정책이 after_end 여도 추적을 계속하고 미리보기에 '판단 필요'로 보인다):
  - 일정(kind='meeting')이고 시리즈 회차(series_key)다 — 단발 일정은 이번 변경 대상이 아니다.
  - Calendar 원본에 연결되어 있고 그 연결이 정상(ok)이다 — 사라짐(missing)·충돌(conflict)은 기존 확인 절차.
  - 상태가 할 일·진행 중·확인 필요·참고다 — 답변 대기·보류·완료·취소는 교수님이 정한 상태라 그대로.
  - 확정된 종료 시각이 있다(claire_core.occurrence_end) — 종료 시각을 지어내지 않는다.
제목에 '미팅'·'매주'가 있다는 이유로 정책을 정하지 않는다. 참석형인지(의미)는 Claire 가 원문을 보고 판단하고(제안의
track_policy), 기존 누적분은 미리보기 → 교수님 확인 → apply 로 정한다.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from claire_core import (
    ClaireError, OPEN_STATUSES, log_activity, occurrence_end, parse_iso, register_functions, tz_of,
)

POLICIES = ("after_end", "until_done")
POLICY_KO = {"after_end": "종료 후 추적 제외", "until_done": "완료 확인까지 추적", None: "정책 미정(완료 확인까지 추적)"}
# 시간이 지나면 추적에서 빠질 수 있는 상태. 대기·보류는 교수님이 고른 상태라 재확인일에 다시 묻는다.
ENDABLE_STATUSES = ("captured", "needs_info", "todo", "in_progress")
ENDED_LABEL = "종료·추적 제외"
# 미리보기에서 의미 확인이 필요하다는 표시만 한다(자동 분류에 쓰지 않는다): 수행 결과를 확인해야 하는 일의 단어
TASKLIKE_RE = re.compile(r"결제|납부|제출|결재|회신|마감|정산|청구|신청|보고서|송금|갱신")

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}([+-]\d{2}:\d{2}|Z)$")


def _now_literal(now) -> str:
    s = now if isinstance(now, str) else now.isoformat()
    if not _ISO_RE.match(s):
        raise ClaireError("bad_value", f"시각 형식이 잘못되었습니다: {s}")
    return s


def ended_sql(now, a: str = "item") -> str:
    """'지금 끝나서 추적에서 빠진 정기 회차' SQL 조건. occurrence_state(...)["ended"] 와 같은 규칙 (테스트가 대조한다)."""
    n = _now_literal(now)
    st = ",".join(f"'{s}'" for s in ENDABLE_STATUSES)
    # 모든 항이 참/거짓만 내게 한다(NULL 금지): NOT (… NULL …) 은 NULL 이 되어 활성 조회에서 행을 잃는다.
    return (f"({a}.deleted_at IS NULL AND {a}.kind='meeting' AND {a}.series_key IS NOT NULL AND {a}.status IN ({st}) "
            f"AND COALESCE({a}.track_policy, (SELECT sp.policy FROM series_policy sp WHERE sp.series_key={a}.series_key), "
            f"'until_done')='after_end' "
            f"AND EXISTS (SELECT 1 FROM link lk WHERE lk.item_id={a}.id AND lk.system='calendar') "
            f"AND NOT EXISTS (SELECT 1 FROM link lk WHERE lk.item_id={a}.id AND lk.system='calendar' AND lk.sync_status<>'ok') "
            f"AND COALESCE(claire_occurrence_ended({a}.start_at, {a}.end_at, {a}.all_day, {a}.tz, '{n}'), 0)=1)")


def tracked_sql(now, a: str = "item") -> str:
    """활성(추적 중) 업무 조건: 열린 상태이면서 종료·추적 제외가 아님. OPEN_STATUSES 만 보던 조회를 이것으로 바꾼다."""
    st = ",".join(f"'{s}'" for s in OPEN_STATUSES)
    return f"({a}.status IN ({st}) AND NOT {ended_sql(now, a)})"


def ended_ids(conn, now) -> set[int]:
    register_functions(conn)
    return {r[0] for r in conn.execute(f"SELECT id FROM item WHERE {ended_sql(now)}")}


def tracked_rows(conn, now, extra_where: str = "", params: tuple = ()) -> list:
    """활성 업무 행 (ORDER BY id). 아침 보고·업무 현황·누락 감지가 같은 집합을 쓴다."""
    register_functions(conn)
    sql = f"SELECT * FROM item WHERE deleted_at IS NULL AND {tracked_sql(now)}"
    if extra_where:
        sql += f" AND ({extra_where})"
    return conn.execute(sql + " ORDER BY id", params).fetchall()


def series_row(conn, series_key: str | None):
    if not series_key:
        return None
    return conn.execute("SELECT * FROM series_policy WHERE series_key=?", (series_key,)).fetchone()


def _get(row, key, default=None):
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def effective_policy(conn, item) -> dict:
    """회차 예외 > 시리즈 정책 > 없음. {policy, from: occurrence|series|None, source, reason}"""
    own = _get(item, "track_policy")
    if own:
        return {"policy": own, "from": "occurrence", "source": None, "reason": _get(item, "track_policy_reason")}
    sp = series_row(conn, _get(item, "series_key"))
    if sp is not None:
        return {"policy": sp["policy"], "from": "series", "source": sp["source"], "reason": sp["reason"]}
    return {"policy": None, "from": None, "source": None, "reason": None}


def occurrence_state(conn, cfg, item, now: datetime) -> dict:
    """회차 하나의 추적 판정과 그 근거. ended 가 참이면 활성 목록·진행 확인·기한 지남·누락 감지에서 빠진다."""
    pol = effective_policy(conn, item)
    end = occurrence_end(_get(item, "end_at"), _get(item, "all_day"), _get(item, "tz"), _get(item, "start_at"))
    blockers = []
    if item["kind"] != "meeting":
        blockers.append("kind_not_meeting")
    if not _get(item, "series_key"):
        blockers.append("not_series")
    links = conn.execute("SELECT sync_status FROM link WHERE item_id=? AND system='calendar'", (item["id"],)).fetchall()
    if not links:
        blockers.append("no_calendar_link")
    elif any(r["sync_status"] != "ok" for r in links):
        blockers.append("calendar_" + next(r["sync_status"] for r in links if r["sync_status"] != "ok"))
    if end is None:
        blockers.append("no_end_time")
    status_ok = item["status"] in ENDABLE_STATUSES and not _get(item, "deleted_at")
    eligible = not blockers
    passed = end is not None and end <= now
    ended = pol["policy"] == "after_end" and eligible and status_ok and passed
    if ended:
        label = ENDED_LABEL
    elif pol["policy"] == "after_end" and not eligible:
        label = "정책은 종료 후 제외지만 조건 부족 — 계속 추적"
    else:
        label = None
    return {"policy": pol["policy"], "policy_label": POLICY_KO.get(pol["policy"]), "policy_from": pol["from"],
            "policy_source": pol["source"], "policy_reason": pol["reason"], "eligible": eligible, "blockers": blockers,
            "end": end.isoformat() if end else None, "end_passed": passed, "ended": ended, "label": label}


BLOCKER_KO = {"kind_not_meeting": "일정이 아닌 업무", "not_series": "단발 일정", "no_calendar_link": "Calendar 원본 연결 없음",
              "calendar_missing": "Calendar 에서 사라짐(확인 필요)", "calendar_conflict": "Calendar 충돌(확인 필요)",
              "calendar_pending": "Calendar 반영 대기", "no_end_time": "종료 시각 없음"}


# --------------------------------------------------------------------------
# 미리보기 (변경 없음) — ISSUE §6 2·3단계
# --------------------------------------------------------------------------

def _series_members(conn) -> dict[str, list]:
    out: dict[str, list] = {}
    for r in conn.execute("SELECT * FROM item WHERE deleted_at IS NULL AND series_key IS NOT NULL ORDER BY start_at, id"):
        out.setdefault(r["series_key"], []).append(r)
    return out


def _linked_work(conn, ids: list[int]) -> list[dict]:
    """회차에 연결된 준비·후속 업무 (일정 종료와 무관하게 계속 추적)."""
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT DISTINCT i.ref, i.title, i.status, rl.rel_type FROM relation rl JOIN item i ON i.id=rl.from_item_id "
        f"WHERE rl.rel_type IN ('prep_for','follow_up_of') AND rl.to_item_id IN ({q}) AND i.deleted_at IS NULL ORDER BY i.id",
        ids).fetchall()
    return [{"ref": r["ref"], "title": r["title"], "status": r["status"],
             "role": "준비" if r["rel_type"] == "prep_for" else "후속"} for r in rows]


def classify_series(conn, cfg, key: str, members: list, now: datetime) -> dict:
    states = [(m, occurrence_state(conn, cfg, m, now)) for m in members]
    kinds = sorted({m["kind"] for m in members})
    title = members[-1]["title"]
    sp = series_row(conn, key)
    open_past = [(m, s) for m, s in states if m["status"] in ENDABLE_STATUSES and s["end_passed"]]
    open_future = [m for m, s in states if m["status"] in ENDABLE_STATUSES and not s["end_passed"] and s["end"]]
    noend = [m for m, s in states if m["status"] in ENDABLE_STATUSES and "no_end_time" in s["blockers"]]
    bad_link = [(m, s) for m, s in states if m["status"] in ENDABLE_STATUSES
                and any(b.startswith("calendar_") or b == "no_calendar_link" for b in s["blockers"])]
    parked = [m for m in members if m["status"] in ("waiting", "on_hold")]
    reasons, review = [], []
    if sp is not None and sp["policy"] == "after_end":
        category = "applied"
        reasons.append(f"이미 적용됨 ({'교수님' if sp['source'] == 'user' else 'Claire'} 결정" + (f": {sp['reason']}" if sp["reason"] else "") + ")")
    elif sp is not None and sp["policy"] == "until_done":
        category = "keep"
        reasons.append("정책: 완료 확인까지 추적" + (f" ({sp['reason']})" if sp["reason"] else ""))
    elif "meeting" not in kinds:
        category = "keep"
        reasons.append("일정이 아닌 업무(" + "·".join(kinds) + ") — 수행 결과를 확인해야 하므로 시간 경과로 빼지 않음")
    else:
        category = "candidate"
        reasons.append("Calendar 반복 시리즈의 일정 회차")
        if len(kinds) > 1:
            category = "review"
            review.append("일정과 업무 회차가 섞여 있음(" + "·".join(kinds) + ")")
        if not any(conn.execute("SELECT 1 FROM link WHERE item_id=? AND system='calendar'", (m["id"],)).fetchone()
                   for m in members):
            category = "review"
            review.append("Calendar 원본 연결이 없음(반복 정보 확인 불가)")
        if TASKLIKE_RE.search(title):
            category = "review"
            review.append(f"제목에 수행형 단어('{TASKLIKE_RE.search(title).group(0)}') — 참석형인지 의미 확인 필요")
    linked = _linked_work(conn, [m["id"] for m in members])
    oq = conn.execute("SELECT COUNT(*) FROM question q JOIN item i ON i.id=q.item_id WHERE q.status='open' AND i.series_key=?",
                      (key,)).fetchone()[0]
    ob = conn.execute("SELECT COUNT(*) FROM outbox o JOIN item i ON i.id=o.item_id WHERE o.status='awaiting_confirm' "
                      "AND i.series_key=?", (key,)).fetchone()[0]
    would_end = [m["ref"] for m, s in open_past if s["eligible"]]
    return {
        "series_key": key, "title": title, "kinds": kinds, "category": category, "reasons": reasons, "review": review,
        "policy": sp["policy"] if sp is not None else None, "policy_source": sp["source"] if sp is not None else None,
        "occurrences": len(members), "open_past": len(open_past), "open_future": len(open_future),
        "done": sum(1 for m in members if m["status"] == "done"),
        "cancelled": sum(1 for m in members if m["status"] == "cancelled"),
        "parked": [m["ref"] for m in parked],
        "would_end": would_end if category in ("candidate", "applied") else [],
        "would_end_count": len(would_end) if category in ("candidate", "applied") else 0,
        "needs_review_occurrences": [{"ref": m["ref"], "start_at": m["start_at"], "why": BLOCKER_KO["no_end_time"]} for m in noend]
        + [{"ref": m["ref"], "start_at": m["start_at"], "why": " · ".join(BLOCKER_KO.get(b, b) for b in s["blockers"])}
           for m, s in bad_link],
        "first_start": members[0]["start_at"], "last_start": members[-1]["start_at"],
        "linked_work": linked, "open_questions": oq, "awaiting_approvals": ob,
    }


def _counts(conn, cfg, now) -> dict:
    import claire_track as track
    rd = track.report_data(conn, cfg, now)
    ag = track.agenda_data(conn, cfg, now)
    gaps = track.exposure_gaps(conn, cfg, now)
    c = rd["counts"]
    return {"active": c["active"], "past_meetings": c["past_meetings"], "progress": c["progress"], "overdue": c["overdue"],
            "check_today": ag["counts"]["check"], "exposure_gaps": len(gaps), "open_questions_shown": rd["questions"]["total"],
            "accounting_ok": rd["accounting"]["ok"], "coverage_missing": len(ag["coverage"]["missing"])}


def projected_counts(conn, cfg, now, assume: dict[str, str]) -> dict:
    """정책을 가정했을 때의 집계. 한 트랜잭션 안에서 가정한 정책을 넣고 집계한 뒤 되돌린다(장부 변경 없음)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        ts = _now_literal(now)
        for key, pol in assume.items():
            conn.execute("INSERT INTO series_policy(series_key,policy,source,reason,created_at,updated_at) "
                         "VALUES(?,?,'claire','preview',?,?) ON CONFLICT(series_key) DO UPDATE SET policy=excluded.policy",
                         (key, pol, ts, ts))
        return _counts(conn, cfg, now)
    finally:
        conn.execute("ROLLBACK")


def preview(conn, cfg, now: datetime, series_keys: list[str] | None = None) -> dict:
    register_functions(conn)
    members = _series_members(conn)
    if series_keys:
        unknown = [k for k in series_keys if k not in members]
        if unknown:
            raise ClaireError("series_not_found", f"시리즈를 찾지 못했습니다: {unknown}", series=unknown)
        members = {k: members[k] for k in series_keys}
    rows = [classify_series(conn, cfg, k, v, now) for k, v in members.items()]
    rows.sort(key=lambda r: (["candidate", "review", "applied", "keep"].index(r["category"]), -r["open_past"], r["title"]))
    singles = conn.execute(
        f"SELECT COUNT(*) FROM item WHERE deleted_at IS NULL AND kind='meeting' AND series_key IS NULL "
        f"AND status IN ({','.join('?' * len(ENDABLE_STATUSES))}) "
        f"AND claire_occurrence_ended(start_at, end_at, all_day, tz, ?)=1", (*ENDABLE_STATUSES, _now_literal(now))).fetchone()[0]
    before = _counts(conn, cfg, now)
    assume = {r["series_key"]: "after_end" for r in rows if r["category"] == "candidate"}
    after = projected_counts(conn, cfg, now, assume) if assume else before
    summary = {c: sum(1 for r in rows if r["category"] == c) for c in ("candidate", "review", "applied", "keep")}
    summary["candidate_occurrences_to_exclude"] = sum(r["would_end_count"] for r in rows if r["category"] == "candidate")
    summary["already_excluded"] = sum(r["would_end_count"] for r in rows if r["category"] == "applied")
    summary["linked_work_kept"] = sum(len([w for w in r["linked_work"] if w["status"] in OPEN_STATUSES])
                                      for r in rows if r["category"] in ("candidate", "applied"))
    return {"now": _now_literal(now), "series": rows, "summary": summary,
            "single_meetings_past_open": singles,
            "projection": {"before": before, "after_candidates": after, "assumed_series": sorted(assume)},
            "note": ("미리보기는 장부를 바꾸지 않는다. 후보(candidate)도 의미(참석형인지)는 교수님 확인 뒤 apply 로 정한다. "
                     "'지난 일정' 누적 건수 전체가 전환 대상은 아니다 — 단발 일정·정책 없는 시리즈·조건 부족 회차는 그대로 추적.")}


def preview_text(pv: dict) -> str:
    s = pv["summary"]
    b, a = pv["projection"]["before"], pv["projection"]["after_candidates"]
    L = [f"[정기 일정 추적 정책 미리보기] {pv['now'][:16].replace('T', ' ')} 기준 — 장부 변경 없음",
         f"  후보 {s['candidate']}개 시리즈(지난 회차 {s['candidate_occurrences_to_exclude']}건 제외 예정) · 판단 필요 {s['review']} · "
         f"이미 적용 {s['applied']}(제외 중 {s['already_excluded']}건) · 계속 추적 {s['keep']}",
         f"  집계 전→후: 활성 {b['active']}→{a['active']} · 지난 일정(미처리) {b['past_meetings']}→{a['past_meetings']} · "
         f"진행 확인 {b['progress']}→{a['progress']} · 오늘 확인 {b['check_today']}→{a['check_today']} · "
         f"누락 감지 {b['exposure_gaps']}→{a['exposure_gaps']} · accounting {'ok' if a['accounting_ok'] else '오류'}",
         f"  단발 일정 중 끝났는데 열린 것 {pv['single_meetings_past_open']}건은 이번 변경 대상이 아님(그대로 추적)",
         f"  연결된 준비·후속 업무 {s['linked_work_kept']}건은 일정 종료와 무관하게 계속 추적"]
    names = {"candidate": "후보 — 참석형이면 apply", "review": "판단 필요", "applied": "적용됨", "keep": "계속 추적"}
    for cat in ("candidate", "review", "applied", "keep"):
        rs = [r for r in pv["series"] if r["category"] == cat]
        if not rs:
            continue
        L.append(f"■ {names[cat]} ({len(rs)})")
        for r in rs:
            extra = []
            if r["would_end_count"]:
                extra.append(f"지난 회차 {r['would_end_count']}건 제외")
            if r["open_future"]:
                extra.append(f"앞으로 {r['open_future']}회 안내 유지")
            if r["linked_work"]:
                extra.append("연결 업무 " + ",".join(w["ref"] for w in r["linked_work"]))
            if r["parked"]:
                extra.append("대기·보류 유지 " + ",".join(r["parked"]))
            if r["needs_review_occurrences"]:
                extra.append("회차 확인 필요 " + ",".join(o["ref"] for o in r["needs_review_occurrences"]))
            why = "; ".join(r["review"] or r["reasons"])
            L.append(f"  {r['title']} [{r['series_key'][:24]}] · {'·'.join(r['kinds'])} · {why}"
                     + (f" · {' · '.join(extra)}" if extra else ""))
    return "\n".join(L)


# --------------------------------------------------------------------------
# 정책 바꾸기 (이력·되돌리기)
# --------------------------------------------------------------------------

def _row_dict(row) -> dict | None:
    return dict(row) if row is not None else None


def set_series_policy(conn, key: str, policy: str | None, *, actor: str, ts: str, reason: str | None,
                      title: str | None = None, batch: str | None = None, force: bool = False) -> dict:
    """시리즈 정책을 바꾼다(policy=None 이면 지운다). 같은 값이면 아무것도 하지 않는다(멱등).
    Claire 는 교수님이 정한 정책을 덮지 못한다(force 는 교수님 지시일 때만)."""
    if policy is not None and policy not in POLICIES:
        raise ClaireError("bad_value", f"policy 는 {POLICIES} 중 하나", policy=policy)
    old = series_row(conn, key)
    if old is not None and old["source"] == "user" and actor != "user" and not force:
        if old["policy"] == policy:
            return {"series_key": key, "changed": False, "policy": policy}
        raise ClaireError("user_decided", f"이 시리즈 정책은 교수님이 정했습니다({POLICY_KO[old['policy']]}). Claire 가 바꾸지 않습니다",
                          series_key=key, policy=old["policy"])
    if (old["policy"] if old is not None else None) == policy:
        return {"series_key": key, "changed": False, "policy": policy}
    if policy == "after_end" and conn.execute("SELECT 1 FROM item WHERE series_key=? AND kind='meeting' AND deleted_at IS NULL",
                                              (key,)).fetchone() is None:
        raise ClaireError("not_eligible", "일정(참석형) 회차가 없는 시리즈입니다. 결제·제출·회신 같은 업무는 완료 확인까지 추적합니다",
                          series_key=key)
    source = "user" if actor == "user" else "claire"
    if policy is None:
        conn.execute("DELETE FROM series_policy WHERE series_key=?", (key,))
    elif old is None:
        conn.execute("INSERT INTO series_policy(series_key,policy,source,reason,title,batch,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,?,?,?)", (key, policy, source, reason, title, batch, ts, ts))
    else:
        conn.execute("UPDATE series_policy SET policy=?, source=?, reason=?, title=COALESCE(?, title), batch=?, updated_at=? "
                     "WHERE series_key=?", (policy, source, reason, title, batch, ts, key))
    aid = log_activity(conn, item_id=None, action="policy", actor=actor if actor in ("user", "claire") else "claire", ts=ts,
                       field="track_policy", old_value=old["policy"] if old is not None else None, new_value=policy,
                       reason=reason, payload={"scope": "series", "series_key": key, "batch": batch, "title": title,
                                               "before": _row_dict(old)})
    return {"series_key": key, "changed": True, "policy": policy, "before": old["policy"] if old is not None else None,
            "activity_id": aid}


def set_occurrence_policy(conn, item, policy: str | None, *, actor: str, ts: str, reason: str | None) -> dict:
    """회차 하나의 예외 정책 (예: 이 회차만 보고서가 필요 → until_done). None 이면 시리즈 정책을 따른다."""
    if policy is not None and policy not in POLICIES:
        raise ClaireError("bad_value", f"policy 는 {POLICIES} 중 하나", policy=policy)
    if policy == "after_end" and (item["kind"] != "meeting" or not item["series_key"]):
        raise ClaireError("not_eligible", f"{item['ref']} 는 정기 일정 회차가 아닙니다. 단발 일정·업무는 종료 후 제외 대상이 아닙니다",
                          ref=item["ref"])
    if item["track_policy"] == policy:
        return {"ref": item["ref"], "changed": False, "policy": policy}
    conn.execute("UPDATE item SET track_policy=?, track_policy_reason=?, updated_at=? WHERE id=?",
                 (policy, reason if policy else None, ts, item["id"]))
    aid = log_activity(conn, item_id=item["id"], action="policy", actor=actor if actor in ("user", "claire") else "claire",
                       ts=ts, field="track_policy", old_value=item["track_policy"], new_value=policy, reason=reason,
                       payload={"scope": "occurrence", "series_key": item["series_key"],
                                "before": {"track_policy": item["track_policy"],
                                           "track_policy_reason": item["track_policy_reason"]}})
    return {"ref": item["ref"], "changed": True, "policy": policy, "before": item["track_policy"], "activity_id": aid}


def revert_batch(conn, batch: str, *, ts: str, actor: str = "user") -> dict:
    """apply 한 묶음을 되돌린다: 묶음 전의 시리즈 정책으로. 그 뒤에 다시 바뀐 시리즈는 건드리지 않고 알린다."""
    acts = conn.execute("SELECT * FROM activity_log WHERE action='policy' AND undone_at IS NULL AND item_id IS NULL "
                        "ORDER BY id DESC").fetchall()
    mine = [a for a in acts if (json.loads(a["payload"] or "{}").get("batch") == batch)]
    if not mine:
        raise ClaireError("batch_not_found", f"되돌릴 정책 묶음이 없습니다: {batch} (이미 되돌렸거나 번호가 다름)", batch=batch)
    restored, skipped = [], []
    for a in mine:
        p = json.loads(a["payload"])
        key, before = p["series_key"], p.get("before")
        cur = series_row(conn, key)
        if cur is None or cur["batch"] != batch:
            skipped.append({"series_key": key, "why": "묶음 뒤에 정책이 다시 바뀜 — 그대로 둠"})
            continue
        if before is None:
            conn.execute("DELETE FROM series_policy WHERE series_key=?", (key,))
        else:
            conn.execute("UPDATE series_policy SET policy=?, source=?, reason=?, title=?, batch=?, created_at=?, updated_at=? "
                         "WHERE series_key=?", (before["policy"], before["source"], before["reason"], before["title"],
                                                before["batch"], before["created_at"], before["updated_at"], key))
        conn.execute("UPDATE activity_log SET undone_at=? WHERE id=?", (ts, a["id"]))
        log_activity(conn, item_id=None, action="policy", actor=actor, ts=ts, field="track_policy",
                     old_value=cur["policy"], new_value=before["policy"] if before else None,
                     reason=f"정책 묶음 {batch} 되돌림", payload={"scope": "series", "series_key": key, "revert_of": batch,
                                                             "before": _row_dict(cur)})
        restored.append({"series_key": key, "policy": before["policy"] if before else None})
    return {"batch": batch, "restored": restored, "skipped": skipped}


def series_history(conn, cfg, now: datetime, *, series_key: str | None = None, days: int = 30,
                   limit: int = 200) -> list[dict]:
    """일정 이력: 지난 회차들의 결과(종료·추적 제외 / 완료 / 취소 / 추적 중). '종료'와 '실제 완료'를 나눠 보인다."""
    since = (now - timedelta(days=days)).isoformat()
    sql = ("SELECT * FROM item WHERE deleted_at IS NULL AND kind='meeting' AND series_key IS NOT NULL "
           "AND start_at IS NOT NULL AND start_at >= ? AND start_at <= ?")
    params: list = [since[:10], now.isoformat()]
    if series_key:
        sql += " AND series_key=?"
        params.append(series_key)
    out = []
    for r in conn.execute(sql + " ORDER BY start_at DESC, id DESC LIMIT ?", (*params, limit)):
        st = occurrence_state(conn, cfg, r, now)
        if not st["end_passed"] and r["status"] not in ("done", "cancelled"):
            continue                                   # 아직 안 끝난 회차는 이력이 아니다
        if r["status"] == "done":
            outcome = "완료(보고됨)"
        elif r["status"] == "cancelled":
            outcome = "취소"
        elif st["ended"]:
            outcome = ENDED_LABEL
        elif r["status"] in ("waiting", "on_hold"):
            outcome = "추적 중(대기·보류)"
        else:
            outcome = "추적 중(완료 확인 대기)"
        out.append({"ref": r["ref"], "title": r["title"], "series_key": r["series_key"], "start_at": r["start_at"],
                    "end_at": r["end_at"], "all_day": r["all_day"], "status": r["status"], "outcome": outcome,
                    "ended": st["ended"], "policy": st["policy"]})
    return out
