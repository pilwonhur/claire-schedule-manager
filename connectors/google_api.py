"""Google OAuth 갱신 + Gmail/Calendar REST (PRD §4.3 2순위 어댑터).

표준 라이브러리만 쓴다. `gog` CLI가 저장한 OAuth 클라이언트(client_id/secret)를
재사용하되, 토큰은 **읽기 전용 범위**로 따로 발급해 `~/ClaireData/secrets/`에 둔다 (D1).

  - 읽기 토큰: gmail.readonly + calendar.readonly  → secrets/google-<email>-read.json
  - 쓰기 토큰: calendar.events                      → secrets/google-<email>-write.json (4단계)

토큰 파일·클라이언트 비밀은 이 모듈 밖으로 나가지 않는다. 진단 출력에는 존재 여부와
범위·만료만 싣는다.

CLI (1단계 doctor·인증용):
  python3 connectors/google_api.py authorize <email> [--kind read|write] [--no-browser]
  python3 connectors/google_api.py ping <email> [--kind read]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets as _secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from claire_core import (  # noqa: E402
    ClaireError, data_dir, load_config, write_secret_json, emit,
)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1"
CAL_BASE = "https://www.googleapis.com/calendar/v3"

TOKEN_KINDS = ("read", "write")


class GoogleApiError(ClaireError):
    def __init__(self, status: int, message: str, **extra):
        super().__init__("google_api_error", message, status=status, **extra)
        self.status = status


# --------------------------------------------------------------------------
# 클라이언트·토큰 파일
# --------------------------------------------------------------------------

def client_file(cfg: dict) -> Path:
    return Path(cfg["google"]["oauth_client_file"]).expanduser()


def load_client(cfg: dict) -> dict:
    """gog가 저장한 Google OAuth 클라이언트 JSON('installed' 또는 'web')을 읽는다."""
    path = client_file(cfg)
    if not path.exists():
        raise ClaireError("oauth_client_missing",
                          f"OAuth 클라이언트 파일이 없습니다: {path}. "
                          "config.google.oauth_client_file 을 확인하세요.")
    raw = json.loads(path.read_text(encoding="utf-8"))
    body = raw.get("installed") or raw.get("web") or raw
    cid, csec = body.get("client_id"), body.get("client_secret")
    if not cid or not csec:
        raise ClaireError("oauth_client_invalid",
                          "OAuth 클라이언트 파일에 client_id/client_secret 이 없습니다.")
    return {"client_id": cid, "client_secret": csec,
            "kind": "installed" if "installed" in raw else ("web" if "web" in raw else "flat")}


def client_summary(cfg: dict) -> dict:
    """진단용. 비밀값은 싣지 않는다."""
    path = client_file(cfg)
    if not path.exists():
        return {"present": False, "path": str(path)}
    try:
        c = load_client(cfg)
        return {"present": True, "path": str(path), "kind": c["kind"],
                "client_id_suffix": c["client_id"][-24:]}
    except ClaireError as e:
        return {"present": True, "path": str(path), "error": e.message}


def token_path(email: str, kind: str = "read") -> Path:
    if kind not in TOKEN_KINDS:
        raise ClaireError("bad_token_kind", f"토큰 종류가 잘못되었습니다: {kind}")
    return data_dir() / "secrets" / f"google-{email}-{kind}.json"


def scopes_for(cfg: dict, kind: str) -> list[str]:
    return list(cfg["google"]["read_scopes" if kind == "read" else "write_scopes"])


def load_token(email: str, kind: str = "read") -> dict | None:
    p = token_path(email, kind)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def token_summary(cfg: dict, email: str, kind: str = "read") -> dict:
    """진단용 요약. refresh_token·access_token은 절대 싣지 않는다."""
    p = token_path(email, kind)
    if not p.exists():
        return {"present": False, "path": str(p)}
    st = p.stat()
    mode = st.st_mode & 0o777
    tok = json.loads(p.read_text(encoding="utf-8"))
    want = set(scopes_for(cfg, kind))
    have = set(tok.get("scopes", []))
    extra_write = {s for s in have if not s.endswith(".readonly") and s not in
                   ("openid", "email", "profile")} if kind == "read" else set()
    return {
        "present": True, "path": str(p), "mode": oct(mode), "mode_ok": mode == 0o600,
        "email": tok.get("email"), "kind": tok.get("kind"),
        "scopes": sorted(have), "scopes_ok": want <= have,
        "missing_scopes": sorted(want - have),
        "has_write_scope": bool(extra_write),
        "obtained_at": tok.get("obtained_at"),
    }


# --------------------------------------------------------------------------
# OAuth 인가 (loopback) — 표준 라이브러리 http.server
# --------------------------------------------------------------------------

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):  # noqa: N802
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.result = {k: v[0] for k, v in q.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = "code" in q
        msg = ("인증이 완료되었습니다. 이 창을 닫아도 됩니다." if ok
               else f"인증 실패: {q.get('error', ['unknown'])[0]}")
        self.wfile.write(f"<html><body><h3>Claire</h3><p>{msg}</p></body></html>".encode("utf-8"))

    def log_message(self, *a):  # 조용히
        pass


def _post_form(url: str, data: dict, timeout: int) -> dict:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(text)
        except json.JSONDecodeError:
            detail = {"raw": text[:500]}
        raise GoogleApiError(e.code, f"토큰 요청 실패 ({e.code}): "
                             f"{detail.get('error_description') or detail.get('error') or text[:200]}",
                             detail=detail)


def authorize(cfg: dict, email: str, kind: str = "read", open_browser: bool = True,
              timeout: int = 300) -> dict:
    """브라우저 loopback 흐름으로 refresh token을 발급해 secrets/에 저장한다.

    범위는 config.google.<kind>_scopes 로 고정한다. 발급 뒤 실제 부여된 범위를 대조하고,
    읽기 토큰인데 쓰기 범위가 섞여 있으면 저장하지 않고 거부한다 (D1).
    """
    client = load_client(cfg)
    scopes = scopes_for(cfg, kind)
    verifier = base64.urlsafe_b64encode(os.urandom(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = _secrets.token_urlsafe(16)

    server = http.server.HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    port = server.server_address[1]
    redirect = f"http://127.0.0.1:{port}/oauth2/callback"
    _CallbackHandler.result = {}
    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()

    params = {
        "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(scopes), "access_type": "offline", "prompt": "consent",
        "include_granted_scopes": "false", "login_hint": email, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    }
    url = AUTH_URL + "?" + urllib.parse.urlencode(params)
    print(f"[Claire] {email} ({kind}) 인증 URL — 브라우저에서 {email} 계정으로 로그인하세요:",
          file=sys.stderr)
    print(url, file=sys.stderr)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    t.join(timeout)
    server.server_close()
    res = _CallbackHandler.result
    if not res:
        raise ClaireError("oauth_timeout", f"{timeout}초 안에 인증 응답이 오지 않았습니다.")
    if res.get("state") != state:
        raise ClaireError("oauth_state_mismatch", "OAuth state 가 일치하지 않습니다. 다시 시도하세요.")
    if "code" not in res:
        raise ClaireError("oauth_denied", f"인증이 거부되었습니다: {res.get('error')}")

    tok = _post_form(TOKEN_URL, {
        "code": res["code"], "client_id": client["client_id"],
        "client_secret": client["client_secret"], "redirect_uri": redirect,
        "grant_type": "authorization_code", "code_verifier": verifier,
    }, cfg["google"]["timeout_seconds"])
    if "refresh_token" not in tok:
        raise ClaireError("oauth_no_refresh_token",
                          "refresh_token 이 오지 않았습니다. Google 계정 설정에서 이 앱의 접근을 "
                          "제거한 뒤 다시 시도하세요.")
    granted = set((tok.get("scope") or "").split())
    if kind == "read":
        writeish = {s for s in granted if s.startswith("https://www.googleapis.com/auth/")
                    and not s.endswith(".readonly")}
        if writeish:
            raise ClaireError("oauth_scope_too_broad",
                              "읽기 전용 토큰에 쓰기 범위가 섞여 있어 저장하지 않았습니다.",
                              scopes=sorted(granted))
    missing = set(scopes) - granted
    if missing:
        raise ClaireError("oauth_scope_missing", "요청한 범위가 모두 부여되지 않았습니다.",
                          missing=sorted(missing))

    record = {
        "email": email, "kind": kind, "scopes": sorted(granted),
        "refresh_token": tok["refresh_token"],
        "access_token": tok.get("access_token"),
        "expires_at": _expiry(tok.get("expires_in")),
        "obtained_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "client_id_suffix": client["client_id"][-24:],
    }
    # 실제로 어느 계정에 발급됐는지 확인한다. login_hint는 힌트일 뿐이다.
    actual = None
    if any(s.endswith("gmail.readonly") or s.endswith("gmail.modify") for s in granted):
        prof = _get(f"{GMAIL_BASE}/users/me/profile", record["access_token"], {},
                    cfg["google"]["timeout_seconds"])
        actual = prof.get("emailAddress")
    elif any(s.endswith("userinfo.email") or s == "email" for s in granted):
        info = _get("https://www.googleapis.com/oauth2/v3/userinfo", record["access_token"], {},
                    cfg["google"]["timeout_seconds"])
        actual = info.get("email")
    elif any(s.endswith("calendar.readonly") or s.endswith("/auth/calendar") for s in granted):
        cl = _get(f"{CAL_BASE}/users/me/calendarList/primary", record["access_token"], {},
                  cfg["google"]["timeout_seconds"])
        actual = cl.get("id")
    if actual and actual.lower() != email.lower():
        raise ClaireError("oauth_wrong_account",
                          f"{email} 로 요청했지만 {actual} 계정으로 인증되었습니다. 저장하지 않았습니다.")
    write_secret_json(token_path(email, kind), record)
    return {"email": email, "kind": kind, "scopes": record["scopes"],
            "path": str(token_path(email, kind)), "verified_account": actual}


def _expiry(expires_in) -> str | None:
    if not expires_in:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in) - 60)) \
        .replace(microsecond=0).isoformat()


def access_token(cfg: dict, email: str, kind: str = "read") -> str:
    """유효한 access token을 돌려준다. 만료됐으면 refresh 하고 파일을 갱신한다."""
    tok = load_token(email, kind)
    if tok is None:
        raise ClaireError("token_missing",
                          f"{email} ({kind}) 토큰이 없습니다. "
                          f"connectors/google_api.py authorize {email} --kind {kind} 를 실행하세요.",
                          path=str(token_path(email, kind)))
    exp = tok.get("expires_at")
    if tok.get("access_token") and exp and datetime.fromisoformat(exp) > datetime.now(timezone.utc):
        return tok["access_token"]
    client = load_client(cfg)
    fresh = _post_form(TOKEN_URL, {
        "client_id": client["client_id"], "client_secret": client["client_secret"],
        "refresh_token": tok["refresh_token"], "grant_type": "refresh_token",
    }, cfg["google"]["timeout_seconds"])
    tok["access_token"] = fresh["access_token"]
    tok["expires_at"] = _expiry(fresh.get("expires_in"))
    write_secret_json(token_path(email, kind), tok)
    return tok["access_token"]


# --------------------------------------------------------------------------
# REST 호출
# --------------------------------------------------------------------------

def _get(url: str, token: str, params: dict, timeout: int) -> dict:
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(clean, doseq=True)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(text)
        except json.JSONDecodeError:
            detail = {"raw": text[:500]}
        msg = (detail.get("error") or {}).get("message") if isinstance(detail.get("error"), dict) \
            else detail.get("error_description") or text[:200]
        raise GoogleApiError(e.code, f"Google API {e.code}: {msg}", url=url.split("?")[0],
                             detail=detail)
    except urllib.error.URLError as e:
        raise ClaireError("network_error", f"Google API 연결 실패: {e.reason}", url=url.split("?")[0])


def api_get(cfg: dict, email: str, url: str, params: dict | None = None, kind: str = "read") -> dict:
    return _get(url, access_token(cfg, email, kind), params or {}, cfg["google"]["timeout_seconds"])


# --------------------------------------------------------------------------
# 테스트 fixture (§14 "Google API는 기록된 응답으로 대체")
# --------------------------------------------------------------------------
#
# CLAIRE_GOOGLE_FIXTURE=<dir> 이면 네트워크 대신 그 디렉터리의 JSON 파일을 읽는다.
# 파일 내용이 {"__error__": 410} 이면 그 HTTP 오류를 낸다. 가장 구체적인 이름부터 찾는다.

def _fixture_dir() -> Path | None:
    d = os.environ.get("CLAIRE_GOOGLE_FIXTURE")
    return Path(d) if d else None


def _fx(*names: str) -> dict | None:
    d = _fixture_dir()
    if d is None:
        return None
    for n in names:
        safe = n.replace("/", "_").replace("#", "_")
        p = d / f"{safe}.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "__error__" in data:
                raise GoogleApiError(int(data["__error__"]), f"fixture error {data['__error__']}",
                                     detail=data)
            return data
    raise ClaireError("fixture_missing", f"fixture 파일이 없습니다: {names[0]}", tried=list(names))


# Gmail (읽기 전용)

def gmail_profile(cfg, email) -> dict:
    fx = _fx(f"gmail_profile_{email}")
    if fx is not None:
        return fx
    return api_get(cfg, email, f"{GMAIL_BASE}/users/me/profile")


def gmail_history_list(cfg, email, start_history_id: str, page_token: str | None = None,
                       history_types=("messageAdded", "labelAdded"), max_results: int = 500) -> dict:
    fx = _fx(f"gmail_history_{email}_{start_history_id}_{page_token}", f"gmail_history_{email}_{start_history_id}",
             f"gmail_history_{email}")
    if fx is not None:
        return fx
    return api_get(cfg, email, f"{GMAIL_BASE}/users/me/history",
                   {"startHistoryId": start_history_id, "historyTypes": list(history_types),
                    "pageToken": page_token, "maxResults": max_results})


def gmail_messages_list(cfg, email, q: str, page_token: str | None = None,
                        max_results: int = 100) -> dict:
    fx = _fx(f"gmail_list_{email}_{page_token}", f"gmail_list_{email}")
    if fx is not None:
        return fx
    return api_get(cfg, email, f"{GMAIL_BASE}/users/me/messages",
                   {"q": q, "pageToken": page_token, "maxResults": max_results})


def gmail_message_get(cfg, email, message_id: str, fmt: str = "full") -> dict:
    fx = _fx(f"gmail_msg_{message_id}")
    if fx is not None:
        return fx
    return api_get(cfg, email, f"{GMAIL_BASE}/users/me/messages/{message_id}", {"format": fmt})


# Calendar (읽기 전용)

def calendar_list_get(cfg, email, calendar_id: str) -> dict:
    fx = _fx(f"cal_list_{calendar_id}")
    if fx is not None:
        return fx
    cid = urllib.parse.quote(calendar_id, safe="@")
    return api_get(cfg, email, f"{CAL_BASE}/users/me/calendarList/{cid}")


def calendar_list(cfg, email) -> dict:
    return api_get(cfg, email, f"{CAL_BASE}/users/me/calendarList", {"maxResults": 250})


def calendar_events_list(cfg, email, calendar_id: str, *, time_min=None, time_max=None,
                         sync_token=None, page_token=None, max_results=250,
                         show_deleted=True) -> dict:
    fx = _fx(f"cal_events_{calendar_id}_{sync_token}_{page_token}", f"cal_events_{calendar_id}_{sync_token}",
             f"cal_events_{calendar_id}")
    if fx is not None:
        return fx
    cid = urllib.parse.quote(calendar_id, safe="@")
    params = {"pageToken": page_token, "maxResults": max_results, "singleEvents": "true",
              "showDeleted": "true" if show_deleted else "false"}
    if sync_token:
        params["syncToken"] = sync_token
    else:
        params.update({"timeMin": time_min, "timeMax": time_max})
    return api_get(cfg, email, f"{CAL_BASE}/calendars/{cid}/events", params)


# --------------------------------------------------------------------------
# Calendar 쓰기 (4단계, 쓰기 토큰 kind='write', Prof. Hur 한 곳만 — §4.3)
# --------------------------------------------------------------------------

def _send_json(method: str, url: str, token: str, body: dict, timeout: int) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(text)
        except json.JSONDecodeError:
            detail = {"raw": text[:500]}
        msg = (detail.get("error") or {}).get("message") if isinstance(detail.get("error"), dict) else text[:200]
        raise GoogleApiError(e.code, f"Google API {e.code}: {msg}", url=url.split("?")[0], detail=detail)
    except urllib.error.URLError as e:
        raise ClaireError("network_error", f"Google API 연결 실패: {e.reason}", url=url.split("?")[0])


def _record_write(name: str, body: dict) -> None:
    """fixture 모드에서 쓰기 요청을 기록한다 (테스트 검증용)."""
    d = _fixture_dir()
    if d is not None:
        with (d / "writes.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"op": name, "body": body}, ensure_ascii=False) + "\n")


def write_token_available(email: str) -> bool:
    return token_path(email, "write").exists()


def calendar_event_insert(cfg, email, calendar_id: str, body: dict) -> dict:
    fx = _fx(f"cal_insert_{calendar_id}", "cal_insert")
    if fx is not None:
        _record_write(f"insert:{calendar_id}", body)
        out = dict(fx); out.setdefault("id", "fx_" + hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:10])
        out.setdefault("etag", "fx_etag_1"); out.setdefault("status", "confirmed")
        for k in ("summary", "start", "end", "description", "location", "extendedProperties"):
            out.setdefault(k, body.get(k))
        return out
    cid = urllib.parse.quote(calendar_id, safe="@")
    return _send_json("POST", f"{CAL_BASE}/calendars/{cid}/events", access_token(cfg, email, "write"), body,
                      cfg["google"]["timeout_seconds"])


def calendar_event_patch(cfg, email, calendar_id: str, event_id: str, body: dict) -> dict:
    fx = _fx(f"cal_patch_{calendar_id}_{event_id}", f"cal_patch_{calendar_id}", "cal_patch")
    if fx is not None:
        _record_write(f"patch:{calendar_id}:{event_id}", body)
        out = dict(fx); out.setdefault("id", event_id); out.setdefault("etag", "fx_etag_2"); out.setdefault("status", "confirmed")
        for k in ("summary", "start", "end", "description", "location"):
            if k in body:
                out[k] = body[k]
        return out
    cid = urllib.parse.quote(calendar_id, safe="@")
    return _send_json("PATCH", f"{CAL_BASE}/calendars/{cid}/events/{urllib.parse.quote(event_id)}",
                      access_token(cfg, email, "write"), body, cfg["google"]["timeout_seconds"])


# --------------------------------------------------------------------------
# 진단 (doctor가 호출)
# --------------------------------------------------------------------------

def ping_gmail(cfg, email) -> dict:
    prof = gmail_profile(cfg, email)
    return {"emailAddress": prof.get("emailAddress"), "historyId": prof.get("historyId"),
            "messagesTotal": prof.get("messagesTotal")}


def ping_calendar(cfg, email, calendar_id) -> dict:
    cl = calendar_list_get(cfg, email, calendar_id)
    return {"id": cl.get("id"), "summary": cl.get("summary"),
            "accessRole": cl.get("accessRole"), "timeZone": cl.get("timeZone")}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(prog="google_api", description="Claire Google 커넥터")
    sub = p.add_subparsers(dest="cmd")
    a = sub.add_parser("authorize", help="읽기(또는 쓰기) 전용 토큰 발급")
    a.add_argument("email")
    a.add_argument("--kind", choices=TOKEN_KINDS, default="read")
    a.add_argument("--no-browser", action="store_true")
    a.add_argument("--timeout", type=int, default=300)
    g = sub.add_parser("ping", help="토큰으로 실제 호출해 계정·범위를 확인")
    g.add_argument("email")
    g.add_argument("--kind", choices=TOKEN_KINDS, default="read")
    s = sub.add_parser("status", help="토큰 파일 요약 (비밀값 제외)")
    s.add_argument("email")
    s.add_argument("--kind", choices=TOKEN_KINDS, default="read")
    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        sys.exit(2)
    cfg = load_config()
    try:
        if args.cmd == "authorize":
            emit(authorize(cfg, args.email, args.kind, open_browser=not args.no_browser,
                           timeout=args.timeout))
        elif args.cmd == "ping":
            out = {"email": args.email, "kind": args.kind}
            if args.kind == "read":
                out["gmail"] = ping_gmail(cfg, args.email)
            cals = [c for c in cfg["calendars"] if c["account"] == args.email]
            out["calendars"] = [ping_calendar(cfg, args.email, c["id"]) for c in cals]
            emit(out)
        elif args.cmd == "status":
            emit(token_summary(cfg, args.email, args.kind))
    except ClaireError as e:
        print(json.dumps({"ok": False, "error": {"code": e.code, "message": e.message, **e.extra}},
                         ensure_ascii=False, indent=2, default=str))
        sys.exit(1)


if __name__ == "__main__":
    main()
