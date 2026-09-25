#!/usr/bin/env bash
# Claire 일정·업무 관리 스킬 설치·업데이트 (PRD.md §4.2, Clio install.sh 계승)
#
#   ./install.sh                  이 폴더(체크아웃한 버전)를 설치
#
# 보통은 직접 부르지 않고 get.sh(= claire-update)가 GitHub 릴리스 태그를 받아 이 스크립트를 부른다.
# 바뀐 파일만 덮어쓰고 원본에서 사라진 파일만 지운다. 데이터(~/ClaireData)와
# 사용자가 고친 config.json 값은 보존한다. 직전 스킬을 데이터 디렉터리 안에 한 세대 보관하고,
# 버전이 바뀌면 스키마 변환 전에 DB 사본을 backups/pre-upgrade-* 에 남긴다.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="${CLAIRE_SKILL_DIR:-$HOME/.claude/skills/claire-schedule-manager}"
DATA_DIR="${CLAIRE_DATA_DIR:-$HOME/ClaireData}"
PREV_DIR="${CLAIRE_PREV_DIR:-$DATA_DIR/skill-prev}"
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
echo "데이터: $DATA_DIR  (claire.db·secrets·첨부·백업은 건드리지 않음)"
echo

if [ "$MODE" = "업데이트" ]; then
  mkdir -p "$(dirname "$PREV_DIR")"
  rm -rf "$PREV_DIR"
  cp -R "$SKILL_DIR" "$PREV_DIR"
  echo "이전 버전 보관: $PREV_DIR"
fi

# 버전이 바뀌면 스키마 변환(claire_check init) 전에 DB 를 그대로 복사해 둔다 (sqlite 온라인 백업, Claire 코드 없이).
if [ -f "$DATA_DIR/claire.db" ] && [ "$OLD_VER" != "$NEW_VER" ]; then
  PRE="$DATA_DIR/backups/pre-upgrade-${OLD_VER:-unknown}-to-${NEW_VER}-$(date +%Y%m%dT%H%M%S)"
  mkdir -p "$PRE"
  python3 - "$DATA_DIR/claire.db" "$PRE/claire.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
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
         "$SKILL_DIR"/scripts/claire_export "$SKILL_DIR"/scripts/claire_buttons "$SKILL_DIR"/connectors/*.py "$SKILL_DIR"/get.sh

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
CLAIRE_DATA_DIR="$DATA_DIR" python3 "$SKILL_DIR/scripts/claire_check" init \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['message'], '(스키마 v%s)' % d['schema_version']); w=d.get('warning'); print('경고:', w) if w else None"
CLAIRE_DATA_DIR="$DATA_DIR" python3 "$SKILL_DIR/scripts/claire_check" integrity \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('무결성:', '이상 없음' if d['healthy'] else d['problems'])"

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
echo "다음: $SKILL_DIR/scripts/claire_check doctor --format text"
if [ "$MODE" = "업데이트" ] && [ "$OLD_VER" != "$NEW_VER" ]; then
  echo "변경 내역: $SKILL_DIR/CHANGELOG.md (## [$NEW_VER]). OpenClaw 설정·cron 변경이 필요하면 doctor 가 알려 줍니다."
fi
exit 0
