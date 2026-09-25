#!/usr/bin/env bash
# claire-schedule-manager 설치·업그레이드 (GitHub 릴리스 태그 기준)
#
# 처음 설치 / 업그레이드 (같은 명령):
#   curl -fsSL https://raw.githubusercontent.com/pilwonhur/claire-schedule-manager/main/get.sh | bash
# 설치 뒤에는:
#   claire-update                 최신 릴리스로
#   claire-update --check         설치 버전과 최신 릴리스만 비교 (아무것도 바꾸지 않음)
#   claire-update --version v0.4.5   특정 버전으로 (되돌리기 포함)
#   claire-update --main          릴리스 전 main 최신 커밋으로 (시험용)
#   claire-update --skip-tests    설치 전 테스트 생략
#
# 소스는 Dropbox·iCloud 밖의 깨끗한 클론(기본 ~/.claire-schedule-manager/src)에 받는다. 개발용 저장소와 섞이지 않는다.
# 설치 전 그 버전의 테스트를 돌려 실패하면 설치하지 않는다. 설치는 install.sh 가 한다(데이터·config 보존, DB 사전 백업).
set -euo pipefail

REPO_URL="${CLAIRE_REPO_URL:-https://github.com/pilwonhur/claire-schedule-manager.git}"
SRC_DIR="${CLAIRE_SRC_DIR:-$HOME/.claire-schedule-manager/src}"
TARGET=""
MODE="release"
RUN_TESTS=1
CHECK_ONLY=0

usage() { sed -n '2,15p' "${BASH_SOURCE[0]:-$0}" 2>/dev/null | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --version|-v) TARGET="${2:?--version 뒤에 버전(예: v0.5.0)}"; MODE="pinned"; shift 2 ;;
    --main) MODE="main"; shift ;;
    --skip-tests) RUN_TESTS=0; shift ;;
    --check) CHECK_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "알 수 없는 옵션: $1"; usage; exit 2 ;;
  esac
done
case "$TARGET" in ""|v*) ;; *) TARGET="v$TARGET" ;; esac

command -v git >/dev/null || { echo "git 이 필요합니다 (xcode-select --install)"; exit 1; }
command -v python3 >/dev/null || { echo "python3 가 필요합니다 (3.11 이상)"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo "Python 3.11 이상이 필요합니다: $(python3 --version 2>&1)"; exit 1; }

latest_tag() {
  { git ls-remote --tags --refs "$REPO_URL" 'v*' | awk -F/ '{print $NF}' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1; } || true
}

installed_version() {
  local core="${CLAIRE_SKILL_DIR:-$HOME/.claude/skills/claire-schedule-manager}/scripts/claire_core.py"
  [ -f "$core" ] && sed -n 's/^CLAIRE_VERSION = "\(.*\)"/\1/p' "$core" || true
}

if [ "$CHECK_ONLY" = 1 ]; then
  cur="$(installed_version)"; new="$(latest_tag)"
  echo "설치: ${cur:-없음} · 최신 릴리스: ${new:-없음}"
  if [ -n "$new" ] && [ "v${cur}" != "$new" ] && [ "$(printf '%s\n%s\n' "v${cur:-0.0.0}" "$new" | sort -V | tail -1)" = "$new" ]; then
    echo "업데이트 가능 → claire-update"
  fi
  exit 0
fi

# 1) 깨끗한 클론 준비
case "$SRC_DIR" in
  *Dropbox*|*"Mobile Documents"*|*iCloud*) echo "소스 클론을 동기화 폴더 안에 두지 않습니다: $SRC_DIR (CLAIRE_SRC_DIR 로 바꾸세요)"; exit 1 ;;
esac
if [ -d "$SRC_DIR/.git" ]; then
  if [ -n "$(git -C "$SRC_DIR" status --porcelain)" ]; then
    echo "$SRC_DIR 에 손으로 고친 파일이 있습니다. 이 폴더는 설치 전용입니다 — 변경을 치우고 다시 실행하세요:"
    git -C "$SRC_DIR" status --short | head -20
    exit 1
  fi
  git -C "$SRC_DIR" fetch --quiet --tags --force origin
else
  mkdir -p "$(dirname "$SRC_DIR")"
  echo "소스 받기: $REPO_URL → $SRC_DIR"
  git clone --quiet "$REPO_URL" "$SRC_DIR"
fi

# 2) 대상 버전 결정·체크아웃
case "$MODE" in
  release)
    TARGET="$( { git -C "$SRC_DIR" tag -l 'v*' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1; } || true)"
    [ -n "$TARGET" ] || { echo "릴리스 태그가 없습니다. --main 으로 설치하거나 태그를 먼저 만드세요."; exit 1; }
    git -C "$SRC_DIR" checkout --quiet --detach "refs/tags/$TARGET" ;;
  pinned)
    git -C "$SRC_DIR" rev-parse --verify --quiet "refs/tags/$TARGET" >/dev/null || {
      echo "태그 $TARGET 이 없습니다. 있는 릴리스:"; git -C "$SRC_DIR" tag -l 'v*' | sort -V | tail -10; exit 1; }
    git -C "$SRC_DIR" checkout --quiet --detach "refs/tags/$TARGET" ;;
  main)
    git -C "$SRC_DIR" checkout --quiet --detach origin/main
    TARGET="main@$(git -C "$SRC_DIR" rev-parse --short HEAD)" ;;
esac
NEW_VER="$(sed -n 's/^CLAIRE_VERSION = "\(.*\)"/\1/p' "$SRC_DIR/scripts/claire_core.py")"
CUR_VER="$(installed_version)"
echo "대상: $TARGET (버전 $NEW_VER) · 현재 설치: ${CUR_VER:-없음}"
if [ "$MODE" = "release" ] && [ -n "$CUR_VER" ] && [ "$(printf '%s\n%s\n' "$CUR_VER" "$NEW_VER" | sort -V | tail -1)" = "$CUR_VER" ] && [ "$CUR_VER" != "$NEW_VER" ]; then
  echo "설치된 버전($CUR_VER)이 최신 릴리스($NEW_VER)보다 높습니다. 내리려면 --version v$NEW_VER 로 명시하세요."
  exit 0
fi

# 3) 그 버전의 테스트 (실패하면 설치하지 않는다)
if [ "$RUN_TESTS" = 1 ]; then
  echo "테스트 실행 중… (30초 안팎, --skip-tests 로 생략)"
  LOG="$(mktemp -t claire-get-tests.XXXXXX)"
  if ! (cd "$SRC_DIR" && python3 tests/test_claire.py >"$LOG" 2>&1); then
    echo "테스트 실패 — 설치하지 않았습니다. 로그: $LOG"
    tail -20 "$LOG"
    exit 1
  fi
  echo "테스트 통과 ($(grep -Eo 'Ran [0-9]+ tests' "$LOG" || true))"
fi

# 4) 설치
GIT_TAG_ARG=""
[ "$MODE" = "main" ] || GIT_TAG_ARG="$TARGET"
CLAIRE_INSTALLER="get.sh $TARGET" CLAIRE_GIT_TAG="$GIT_TAG_ARG" bash "$SRC_DIR/install.sh"
