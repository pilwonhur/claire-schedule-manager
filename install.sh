#!/usr/bin/env bash
# Claire 일정·업무 관리 스킬 설치·업데이트 (PRD.md §4.2, Clio install.sh 계승)
#
#   ./install.sh                  이 폴더(체크아웃한 버전)를 설치
#
# 보통은 직접 부르지 않고 get.sh(= claire-update)가 GitHub 릴리스 태그를 받아 이 스크립트를 부른다.
# 바뀐 파일만 덮어쓰고 원본에서 사라진 파일만 지운다. 데이터와 사용자가 고친 config.json 값은 보존한다.
#
# 0.9.0 데이터 위치: 이 스크립트도 Claire 도구·예약 실행과 같은 규칙(scripts/claire_paths.py)으로 정한다.
#   CLAIRE_DATA_DIR > 위치 기록(~/.claire-schedule-manager/data-location.json) > 기존 ~/ClaireData > 새 기본 ~/SecretaryData/services/schedule
#   기존 설치는 그대로 ~/ClaireData 를 쓴다(자동으로 옮기지 않는다). 옮기는 것은 claire_relocate 로만.
#   위치가 정해지지 않거나(두 위치 충돌·위치 기록 대상 없음) 이전 작업 중이면 아무것도 바꾸지 않고 멈춘다.
# 직전 스킬은 ~/.claire-schedule-manager/skill-prev 에 한 세대 보관하고, 버전이 바뀌면 스키마 변환 전에
# DB 사본을 백업 루트(기본 ~/SecretaryBackups/schedule)의 pre-upgrade-* 에 남긴다.
# 중단된 이전이 있으면 멈춘다. 그 이전을 고친 코드로 잇거나 되돌려야 할 때만 CLAIRE_INSTALL_CODE_ONLY=1 로
# 프로그램 파일만 설치한다(장부·설정·위치 기록·백업은 건드리지 않음).
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="${CLAIRE_SKILL_DIR:-$HOME/.claude/skills/claire-schedule-manager}"
APP_HOME="${CLAIRE_HOME:-$HOME/.claire-schedule-manager}"
PREV_DIR="${CLAIRE_PREV_DIR:-$APP_HOME/skill-prev}"
BIN_DIR="${CLAIRE_BIN_DIR:-$HOME/.local/bin}"

PAYLOAD=(SKILL.md CHANGELOG.md VERSION get.sh scripts connectors references evals tests docs)

command -v python3 >/dev/null || { echo "python3 가 필요합니다 (3.11 이상)"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo "Python 3.11 이상이 필요합니다: $(python3 --version 2>&1)"; exit 1; }

version_of() {
  local f="$1/scripts/claire_core.py"
  [ -f "$f" ] || return 0
  python3 - "$f" <<'PY'
import re, sys
m = re.search(r'^CLAIRE_VERSION\s*=\s*"([^"]+)"', open(sys.argv[1], encoding='utf-8').read(), re.M)
print(m.group(1) if m else "")
PY
}

NEW_VER="$(version_of "$SRC")"
OLD_VER="$(version_of "$SKILL_DIR")"

# 데이터 위치 (설치할 버전의 규칙으로). 막힌 이유가 있으면 어떤 파일도 바꾸기 전에 멈춘다.
LOC_SH="$(python3 "$SRC/scripts/claire_relocate" where --shell)" || { echo "데이터 위치를 확인하지 못했습니다"; exit 1; }
DATA_DIR=""; DATA_SOURCE=""; BACKUP_ROOT=""; BLOCKED=""; BLOCK_CODE=""; BLOCK_CODES=""; BLOCK_LIVE=""
eval "$LOC_SH"
CODE_ONLY=""
if [ -n "$BLOCKED" ]; then
  INTERRUPTED=""                                       # 이전 작업이 중단된 상태(실행 중 아님)인가
  case " $BLOCK_CODES " in *" maintenance "*) [ -n "$BLOCK_LIVE" ] || INTERRUPTED=1 ;; esac
  if [ -n "$INTERRUPTED" ] && [ "${CLAIRE_INSTALL_CODE_ONLY:-}" = 1 ]; then
    # 중단된 이전을 고친 코드로 resume·abort 하려는 경우: 프로그램 파일만 바꾸고 장부·설정·백업은 건드리지 않는다
    CODE_ONLY=1
    echo "중단된 데이터 이전이 있어 프로그램 파일만 설치합니다 (CLAIRE_INSTALL_CODE_ONLY=1). 장부·설정·위치 기록은 건드리지 않습니다."
    echo "  $BLOCKED"
  else
    echo "데이터 위치 문제로 설치·업데이트를 멈춥니다 ($BLOCK_CODE):"
    echo "  $BLOCKED"
    echo "확인: python3 \"$SRC/scripts/claire_relocate\" status --format text"
    if [ -n "$INTERRUPTED" ]; then
      echo "중단된 이전을 이 버전의 코드로 resume·abort 해야 하면, 프로그램 파일만 설치: CLAIRE_INSTALL_CODE_ONLY=1 bash \"$SRC/install.sh\""
    fi
    exit 1
  fi
fi
case "$DATA_SOURCE" in
  env)     LOC_NOTE="환경변수 CLAIRE_DATA_DIR" ;;
  pointer) LOC_NOTE="위치 기록" ;;
  legacy)  LOC_NOTE="기존 설치 위치 — 그대로 씀" ;;
  default) LOC_NOTE="새 기본 위치" ;;
  *)       LOC_NOTE="$DATA_SOURCE" ;;
esac

if [ -f "$SKILL_DIR/scripts/claire_core.py" ]; then
  MODE="업데이트"
  if [ "$OLD_VER" = "$NEW_VER" ]; then
    echo "== Claire 스킬 재적용 (버전 $OLD_VER, 변경분만 반영) =="
  else
    echo "== Claire 스킬 업데이트: $OLD_VER → $NEW_VER =="
  fi
else
  MODE="설치"
  echo "== Claire 스킬 설치: $NEW_VER =="
fi
echo "스킬  : $SKILL_DIR"
echo "데이터: $DATA_DIR  [$LOC_NOTE] (claire.db·secrets·첨부는 건드리지 않음)"
echo "백업  : $BACKUP_ROOT"
echo

if [ "$MODE" = "업데이트" ]; then
  mkdir -p "$(dirname "$PREV_DIR")"
  rm -rf "$PREV_DIR"
  cp -R "$SKILL_DIR" "$PREV_DIR"
  echo "이전 버전 보관: $PREV_DIR"
fi

# 버전이 바뀌면 스키마 변환(claire_check init) 전에 DB 를 그대로 복사해 둔다 (sqlite 온라인 백업, 읽기 전용, Claire 코드 없이).
# 0.9.0: 데이터 폴더 안 backups/ 가 아니라 백업 루트에 둔다. 인증정보는 넣지 않는다.
if [ -z "$CODE_ONLY" ] && [ -f "$DATA_DIR/claire.db" ] && [ "$OLD_VER" != "$NEW_VER" ]; then
  PRE="$BACKUP_ROOT/pre-upgrade-${OLD_VER:-unknown}-to-${NEW_VER}-$(date +%Y%m%dT%H%M%S)"
  mkdir -p "$PRE"
  chmod 700 "$PRE"
  python3 - "$DATA_DIR/claire.db" "$PRE/claire.db" <<'PY'
import os, sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True); dst = sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
os.chmod(sys.argv[2], 0o600)
PY
  echo "업그레이드 전 DB 사본: $PRE/claire.db"
fi

mkdir -p "$SKILL_DIR"
python3 - "$SRC" "$SKILL_DIR" "${PAYLOAD[@]}" <<'PY'
import filecmp, shutil, sys
from pathlib import Path

src, dst, *payload = sys.argv[1:]
src, dst = Path(src), Path(dst)

def files_under(root, names):
    out = set()
    for name in names:
        p = root / name
        if p.is_file():
            out.add(Path(name))
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_file() and "__pycache__" not in f.parts:
                    out.add(f.relative_to(root))
    return out

want = files_under(src, payload)
have = files_under(dst, payload)
added = updated = kept = 0
for rel in sorted(want):
    s, d = src / rel, dst / rel
    if d.exists():
        if filecmp.cmp(s, d, shallow=False):
            kept += 1
            continue
        updated += 1
    else:
        added += 1
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(s, d)
removed = 0
for rel in sorted(have - want):
    (dst / rel).unlink()
    removed += 1
for cache in dst.rglob("__pycache__"):
    shutil.rmtree(cache, ignore_errors=True)
for name in payload:
    p = dst / name
    if p.is_dir():
        for d in sorted((x for x in p.rglob("*") if x.is_dir()), reverse=True):
            if not any(d.iterdir()):
                d.rmdir()
print(f"파일: 추가 {added} · 갱신 {updated} · 유지 {kept} · 삭제 {removed}")
PY

chmod +x "$SKILL_DIR"/scripts/claire_check "$SKILL_DIR"/scripts/claire_run "$SKILL_DIR"/scripts/claire_sync "$SKILL_DIR"/scripts/claire_store "$SKILL_DIR"/scripts/claire_search "$SKILL_DIR"/scripts/claire_apply \
         "$SKILL_DIR"/scripts/claire_export "$SKILL_DIR"/scripts/claire_buttons "$SKILL_DIR"/scripts/claire_memo "$SKILL_DIR"/scripts/claire_relocate \
         "$SKILL_DIR"/connectors/*.py "$SKILL_DIR"/get.sh

# 설치 출처 기록 (claire_check version 이 읽는다). git 체크아웃이면 커밋·태그도.
GIT_COMMIT=""; GIT_TAG=""; GIT_DIRTY=""
if command -v git >/dev/null && git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1; then
  GIT_COMMIT="$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || true)"
  GIT_TAG="${CLAIRE_GIT_TAG:-$(git -C "$SRC" describe --tags --exact-match 2>/dev/null || true)}"
  if [ -n "$(git -C "$SRC" status --porcelain 2>/dev/null)" ]; then GIT_DIRTY="true"; fi
fi
python3 - "$SKILL_DIR/INSTALL_INFO.json" "$NEW_VER" "$SRC" "$GIT_COMMIT" "$GIT_TAG" "$GIT_DIRTY" "${CLAIRE_INSTALLER:-install.sh}" <<'PY'
import datetime, json, sys
path, ver, src, commit, tag, dirty, installer = sys.argv[1:]
with open(path, "w", encoding="utf-8") as fh:
    json.dump({"version": ver, "source": src, "git_commit": commit or None, "git_tag": tag or None,
               "local_changes": dirty == "true", "installer": installer,
               "installed_at": datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()},
              fh, ensure_ascii=False, indent=2)
PY

# 데이터 디렉터리·스키마·config.json (멱등). secrets/ 권한 700. 스키마가 오르면 여기서 변환된다.
# 0.9.0: CLAIRE_DATA_DIR 를 강제하지 않는다 — init 이 같은 규칙으로 위치를 정하고, 처음이면 위치 기록을 남긴다.
if [ -n "$CODE_ONLY" ]; then
  echo "장부 초기화·무결성 확인은 건너뜀 (이전 작업이 끝난 뒤 claire_check init·doctor 로 확인)"
else
INIT_JSON="$(python3 "$SKILL_DIR/scripts/claire_check" init)" || { echo "$INIT_JSON"; echo "init 실패 — 위 오류를 확인하세요"; exit 1; }
printf '%s' "$INIT_JSON" | python3 -c "
import json,sys
d=json.load(sys.stdin); print(d['message'], '(스키마 v%s)' % d['schema_version'])
w=d.get('warning'); print('경고:', w) if w else None
loc=d.get('location') or {}
print('위치 기록:', '새로 남김' if loc.get('pointer_written') else '유지', '·', loc.get('source_label') or '')"
python3 "$SKILL_DIR/scripts/claire_check" integrity \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('무결성:', '이상 없음' if d['healthy'] else d['problems'])"
fi

# OpenClaw Claire 에이전트 워크스페이스에도 판단 계층 문서를 둔다 (심볼릭 링크는 OpenClaw가 거부한다).
# 실행 스크립트는 ~/.claude/skills 한 곳만 쓴다. SKILL.md 의 경로가 그곳을 가리킨다.
OC_WS="${CLAIRE_OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace-claire}"
if [ -d "$OC_WS" ]; then
  OC_SKILL="$OC_WS/skills/claire-schedule-manager"
  mkdir -p "$OC_SKILL"
  cp "$SKILL_DIR/SKILL.md" "$SKILL_DIR/CHANGELOG.md" "$OC_SKILL/"
  rm -rf "$OC_SKILL/references" && cp -R "$SKILL_DIR/references" "$OC_SKILL/references"
  echo "OpenClaw 워크스페이스 스킬 갱신: $OC_SKILL (SKILL.md·references)"
fi

# claire-update 명령 (GitHub 최신 릴리스로 업그레이드). ~/.local/bin 이 없으면 만든다.
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/claire-update" <<EOF
#!/usr/bin/env bash
# claire-schedule-manager 업데이트: GitHub 릴리스 태그를 받아 설치한다 (install.sh 가 만든 파일)
exec bash "$SKILL_DIR/get.sh" "\$@"
EOF
chmod +x "$BIN_DIR/claire-update"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "참고: $BIN_DIR 이 PATH 에 없습니다. ~/.zshrc 에 export PATH=\"$BIN_DIR:\$PATH\" 를 넣으면 claire-update 로 부를 수 있습니다." ;;
esac

echo
echo "$MODE 완료 — claire-schedule-manager $NEW_VER${GIT_TAG:+ ($GIT_TAG)}${GIT_DIRTY:+ (커밋 안 된 변경 포함)}"
echo "데이터베이스: $DATA_DIR/claire.db"
if [ -n "$CODE_ONLY" ]; then
  echo "다음: $SKILL_DIR/scripts/claire_relocate status --format text → resume --id … 또는 abort --id …"
else
  echo "다음: $SKILL_DIR/scripts/claire_check doctor --format text"
fi
if [ "$DATA_SOURCE" = "legacy" ] || { [ "$DATA_SOURCE" = "pointer" ] && [ "$(cd "$DATA_DIR" && pwd -P)" = "$(cd "$HOME/ClaireData" 2>/dev/null && pwd -P)" ]; }; then
  echo "참고: 기존 위치($DATA_DIR)를 계속 씁니다. 새 위치로 옮기려면 계획부터: $SKILL_DIR/scripts/claire_relocate plan --format text"
fi
if [ "$MODE" = "업데이트" ] && [ "$OLD_VER" != "$NEW_VER" ]; then
  echo "변경 내역: $SKILL_DIR/CHANGELOG.md (## [$NEW_VER]). OpenClaw 설정·cron 변경이 필요하면 doctor 가 알려 줍니다."
fi
exit 0
