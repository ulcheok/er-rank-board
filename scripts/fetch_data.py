#!/usr/bin/env python3
"""
players.txt에 적힌 사람들의 이터널 리턴 랭크 정보(현재 RP·티어, 최고 티어, 주챔)를
Open API에서 모아 data/players.json으로 저장합니다.

최고 티어는 아래 셋 중 가장 높은 것입니다.
  - 이번 시즌: 갱신할 때마다 그때까지의 최고 RP를 저장
  - 지난 시즌(HISTORY_FROM_SEASON부터): API의 시즌 최종 RP (한 번 불러오면 저장해 두고 재사용)
  - 그보다 전 시즌: past_peaks.txt에 직접 적은 티어 (RP는 그 티어의 최저치)

사용법 (로컬):
    1) 프로젝트 폴더에 .env 파일을 만들고  ER_API_KEY=발급받은키  한 줄을 적습니다.
    2) python scripts/fetch_data.py

환경 변수:
    ER_API_KEY              (필수) Open API 키
    ER_SEASON_ID            (선택) 현재 시즌 ID를 직접 지정. 비우면 자동으로 찾습니다.
    ER_HISTORY_SEASON_IDS   (선택) API로 확인할 지난 시즌 ID 목록 (쉼표로 구분). 비우면 자동으로 찾습니다.
    ER_DELAY                (선택) 요청 사이 대기 시간(초). 기본 1.1초
    ER_API_BASE             (선택) API 주소. 기본 https://open-api.bser.io

표준 라이브러리만 사용하므로 따로 설치할 패키지는 없습니다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KST = timezone(timedelta(hours=9))

MATCHING_MODE_RANKED = 3  # 랭크 게임 (스쿼드)
MATCHING_TEAM_MODE_SQUAD = 3

# 이 시즌부터는 지난 시즌 기록을 API로 확인해요. 그보다 전 시즌은 past_peaks.txt에 직접 적어요.
HISTORY_FROM_SEASON = 11

# ── 티어 구간 ─────────────────────────────────────────────────────────────
# 시즌마다 바뀔 수 있어요. 게임 내 구간이 바뀌면 여기 숫자만 고치면 됩니다.
# (키, 이름, 디비전별 시작 RP)  디비전이 있는 티어는 [4, 3, 2, 1] 순서예요.
TIERS = [
    ("iron", "아이언", [0, 150, 300, 450]),
    ("bronze", "브론즈", [600, 800, 1000, 1200]),
    ("silver", "실버", [1400, 1650, 1900, 2150]),
    ("gold", "골드", [2400, 2700, 3000, 3300]),
    ("platinum", "플래티넘", [3600, 3950, 4300, 4650]),
    ("diamond", "다이아몬드", [5000, 5350, 5700, 6050]),
    ("meteorite", "메테오라이트", [6400, 6700, 7000, 7300]),
    ("mithril", "미스릴", [7600]),
]
TOP_TIER_MIN_RP = 8300  # 데미갓·이터니티가 되려면 최소 이 RP 이상
ETERNITY_MAX_RANK = 300  # 순위 300위 이내 → 이터니티
DEMIGOD_MAX_RANK = 1000  # 순위 1000위 이내 → 데미갓
TIER_ORDER = [t[0] for t in TIERS] + ["demigod", "eternity"]
TIER_NAMES = {k: n for k, n, _ in TIERS} | {"demigod": "데미갓", "eternity": "이터니티"}
TIER_ALIASES = {
    "아이언": "iron", "브론즈": "bronze", "실버": "silver", "골드": "gold",
    "플래티넘": "platinum", "플레티넘": "platinum", "플래": "platinum", "플레": "platinum",
    "다이아몬드": "diamond", "다이아": "diamond", "메테오라이트": "meteorite", "메테오": "meteorite",
    "미스릴": "mithril", "데미갓": "demigod", "이터니티": "eternity",
    # 한 글자 줄임말 (다3, 플4, 골 ...)
    "브": "bronze", "실": "silver", "골": "gold", "플": "platinum", "다": "diamond",
    "메": "meteorite", "미스": "mithril", "데미": "demigod", "이터": "eternity",
}


def log(msg: str) -> None:
    print(msg, flush=True)


# ── API ──────────────────────────────────────────────────────────────────
class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status} {message}".strip())
        self.status = status


class Api:
    RETRYABLE = {403, 429, 500, 502, 503, 504}

    def __init__(self, key: str, base: str, delay: float):
        self.key = key
        self.base = base.rstrip("/")
        self.delay = delay
        self.calls = 0
        self._last = 0.0

    def _throttle(self) -> None:
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def get(self, path: str, params: dict | None = None, retries: int = 3):
        """요청을 보내고 응답의 실제 내용(code, message를 뺀 나머지 키)을 돌려줍니다."""
        url = f"{self.base}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"x-api-key": self.key, "Accept": "application/json", "User-Agent": "er-rank-board/1.0"}

        for attempt in range(retries + 1):
            self._throttle()
            self.calls += 1
            status, message, body = 0, "", None
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as res:
                    body = json.loads(res.read().decode("utf-8"))
                status = int(body.get("code", 200))
                message = body.get("message", "")
            except urllib.error.HTTPError as e:
                status, message = e.code, str(e.reason)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                status, message = 0, str(e)

            if status == 200:
                return unwrap(body)
            # 호출 제한(429/403)이나 일시적인 서버 오류면 잠깐 쉬었다 다시 시도
            if (status in self.RETRYABLE or status == 0) and attempt < retries:
                time.sleep(3 * (2**attempt))
                continue
            raise ApiError(status, message)
        raise ApiError(0, "unreachable")


def unwrap(body: dict):
    """{code, message, <내용>} 형태의 응답에서 <내용>만 꺼냅니다."""
    for k, v in body.items():
        if k not in ("code", "message"):
            return v
    return None


def download_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "er-rank-board/1.0"})
    with urllib.request.urlopen(req, timeout=60) as res:
        return res.read().decode("utf-8-sig", errors="replace")


# ── 게임 데이터 ──────────────────────────────────────────────────────────
def pick(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def season_label(name: str | None, sid: int | None = None) -> str:
    """'Season 11' → '시즌 11'"""
    if name:
        return re.sub(r"(?i)^\s*season\s*", "시즌 ", name).strip()
    return f"시즌 ID {sid}" if sid is not None else "지난 시즌"


def season_number(name: str | None) -> int | None:
    if not name or re.search(r"(?i)pre|프리", name):
        return None
    m = re.search(r"(?i)season\s*(\d+)|시즌\s*(\d+)", name)
    return int(m.group(1) or m.group(2)) if m else None


def resolve_seasons(api: Api) -> tuple[int, dict[int, str]]:
    """(현재 시즌 ID, {시즌 ID: 시즌 이름})"""
    override = os.environ.get("ER_SEASON_ID", "").strip()
    names: dict[int, str] = {}
    current = None
    try:
        for row in api.get("v2/data/Season") or []:
            sid = pick(row, "seasonID", "seasonId", "id")
            if sid is None:
                continue
            names[int(sid)] = pick(row, "seasonName", "name")
            if str(pick(row, "isCurrent", default=0)).lower() in ("1", "true"):
                current = int(sid)
    except ApiError as e:
        if not override:
            raise
        log(f"  시즌 목록을 못 불러왔지만 ER_SEASON_ID={override}로 계속합니다 ({e})")

    if override:
        current = int(override)
    if current is None:
        raise SystemExit("현재 시즌을 찾지 못했어요. ER_SEASON_ID 환경 변수로 시즌 ID를 직접 지정해 주세요.")
    return current, names


def history_season_ids(current: int, names: dict[int, str]) -> list[int]:
    override = os.environ.get("ER_HISTORY_SEASON_IDS", "").strip()
    if override:
        return [int(x) for x in re.split(r"[,\s]+", override) if x]
    ids = [sid for sid, name in names.items()
           if sid != current and (season_number(name) or 0) >= HISTORY_FROM_SEASON]
    if not ids and names:
        log(f"  시즌 {HISTORY_FROM_SEASON} 이후의 지난 시즌을 이름에서 찾지 못했어요. "
            f"필요하면 ER_HISTORY_SEASON_IDS로 지정해 주세요. (시즌 목록: {names})")
    return sorted(ids)


L10N_CHARACTER = re.compile(r"Character/Name/(\d+)\s*[┃|\t]\s*(.+?)\s*$")


def character_names(api: Api) -> dict[int, str]:
    """캐릭터 코드 → 이름. 한국어 이름을 우선 쓰고, 실패하면 영어 이름을 씁니다."""
    names: dict[int, str] = {}
    try:
        for row in api.get("v2/data/Character") or []:
            code = pick(row, "code", "characterCode")
            if code is not None:
                names[int(code)] = pick(row, "name", default=f"#{code}")
    except ApiError as e:
        log(f"  캐릭터 목록을 못 불러왔어요 ({e})")

    try:
        l10n = api.get("v1/l10n/Korean")
        path = pick(l10n, "l10Path", "l10nPath", "path") if isinstance(l10n, dict) else None
        if path:
            found = 0
            for line in download_text(path).splitlines():
                m = L10N_CHARACTER.search(line)
                if m:
                    names[int(m.group(1))] = m.group(2)
                    found += 1
            if not found:
                log("  한국어 데이터에서 캐릭터 이름을 찾지 못해 영어 이름을 씁니다.")
    except (ApiError, urllib.error.URLError, TimeoutError) as e:
        log(f"  한국어 이름을 못 불러와서 영어 이름을 씁니다 ({e})")
    return names


# ── 티어 계산 ────────────────────────────────────────────────────────────
def tier_of(rp: int, rank: int | None) -> dict:
    if rp >= TOP_TIER_MIN_RP and rank:
        if rank <= ETERNITY_MAX_RANK:
            return {"tier": "eternity", "tierName": "이터니티", "division": None, "divStart": None, "nextAt": None}
        if rank <= DEMIGOD_MAX_RANK:
            return {"tier": "demigod", "tierName": "데미갓", "division": None, "divStart": None, "nextAt": None}

    steps = []  # (시작 RP, 키, 이름, 디비전)
    for key, name, starts in TIERS:
        for i, start in enumerate(starts):
            steps.append((start, key, name, len(starts) - i if len(starts) > 1 else None))
    current = steps[0]
    nxt = None
    for idx, step in enumerate(steps):
        if rp >= step[0]:
            current = step
            nxt = steps[idx + 1][0] if idx + 1 < len(steps) else TOP_TIER_MIN_RP
    if current[1] == "mithril" and rp >= TOP_TIER_MIN_RP:
        nxt = None  # RP는 충분하고 순위만 남은 상태
    return {"tier": current[1], "tierName": current[2], "division": current[3], "divStart": current[0], "nextAt": nxt}


def tier_floor(tier: str, division: int | None) -> int:
    """티어(디비전)의 최저 RP"""
    if tier in ("demigod", "eternity"):
        return TOP_TIER_MIN_RP
    for key, _, starts in TIERS:
        if key == tier:
            if len(starts) == 1:
                return starts[0]
            div = division if division in (1, 2, 3, 4) else 4
            return starts[4 - div]
    raise KeyError(tier)


def tier_key(t: dict | None) -> tuple:
    """티어 비교용 (높을수록 큼)"""
    if not t:
        return (-1, -1)
    return (TIER_ORDER.index(t["tier"]), t.get("rp") or 0)


def as_percent(value, games: int | None = None):
    """API가 비율(0~1)이나 횟수로 주는 값을 퍼센트(0~100)로 맞춥니다."""
    if value is None:
        return None
    value = float(value)
    if value <= 1:
        return round(value * 100, 1)
    if games:
        return round(value / games * 100, 1)
    return round(value, 1)


# ── 플레이어 ─────────────────────────────────────────────────────────────
def ranked_stats(api: Api, uid: str, season_id: int) -> dict | None:
    """그 시즌 랭크 전적. 기록이 없으면 None."""
    try:
        stats_list = api.get(f"v2/user/stats/uid/{uid}/{season_id}/{MATCHING_MODE_RANKED}") or []
    except ApiError as e:
        if e.status == 404:
            return None
        raise
    if isinstance(stats_list, dict):
        stats_list = [stats_list]
    stats = next((s for s in stats_list if pick(s, "matchingMode") == MATCHING_MODE_RANKED), None)
    stats = stats or (stats_list[0] if stats_list else None)
    if not stats or int(pick(stats, "totalGames", default=0) or 0) == 0:
        return None
    return stats


def server_rank(api: Api, uid: str, season_id: int) -> int | None:
    """데미갓·이터니티는 서버 순위로 정해지므로 서버 순위를 한 번 더 조회"""
    try:
        r = api.get(f"v1/rank/uid/{uid}/{season_id}/{MATCHING_TEAM_MODE_SQUAD}") or {}
        return pick(r, "serverRank")
    except ApiError as e:
        log(f"    서버 순위 조회 실패 ({e}), 전체 순위로 대신 계산해요")
        return None


def season_record(api: Api, uid: str, season_id: int) -> dict | None:
    """지난 시즌의 최종 RP와 티어"""
    stats = ranked_stats(api, uid, season_id)
    if not stats:
        return None
    rp = int(pick(stats, "mmr", default=0) or 0)
    rank = pick(stats, "rank")
    srank = server_rank(api, uid, season_id) if rp >= TOP_TIER_MIN_RP else None
    t = tier_of(rp, srank or rank)
    return {"rp": rp, "tier": t["tier"], "tierName": t["tierName"], "division": t["division"],
            "games": int(pick(stats, "totalGames", default=0) or 0)}


def fetch_player(api: Api, nickname: str, season_id: int, names: dict[int, str],
                 history_ids: list[int], cached_history: dict) -> dict:
    entry: dict = {"nickname": nickname}
    try:
        user = api.get("v1/user/nickname", {"query": nickname})
    except ApiError as e:
        if e.status == 404:
            return {**entry, "status": "not_found"}
        raise
    user_id = pick(user, "userId", "uid", "userNum")
    if not user_id:
        return {**entry, "status": "not_found"}
    entry["nickname"] = pick(user, "nickname", default=nickname)
    uid = urllib.parse.quote(str(user_id), safe="")

    # 지난 시즌 기록: 끝난 시즌은 바뀌지 않으니 저장해 둔 값이 있으면 다시 부르지 않아요.
    history = {}
    for sid in history_ids:
        if str(sid) in cached_history:
            history[str(sid)] = cached_history[str(sid)]
            continue
        try:
            history[str(sid)] = season_record(api, uid, sid)
        except ApiError as e:
            log(f"    시즌 ID {sid} 기록 조회 실패 ({e}), 다음 갱신 때 다시 시도해요")
    entry["history"] = history

    stats = ranked_stats(api, uid, season_id)
    if not stats:
        return {**entry, "status": "unranked"}
    games = int(pick(stats, "totalGames", default=0) or 0)
    rp = int(pick(stats, "mmr", default=0) or 0)
    rank = pick(stats, "rank")
    rank_size = pick(stats, "rankSize")
    srank = server_rank(api, uid, season_id) if rp >= TOP_TIER_MIN_RP else None

    rank_percent = as_percent(pick(stats, "rankPercent"))
    if rank_percent is None and rank and rank_size:
        rank_percent = round(rank / rank_size * 100, 1)

    mains = []
    chars = sorted(
        pick(stats, "characterStats", default=[]) or [],
        key=lambda c: int(pick(c, "totalGames", "usages", default=0) or 0),
        reverse=True,
    )
    for c in chars:
        c_games = int(pick(c, "totalGames", "usages", default=0) or 0)
        if c_games <= 0 or len(mains) >= 3:
            break
        code = int(pick(c, "characterCode", default=0))
        mains.append({
            "code": code,
            "name": names.get(code, f"#{code}"),
            "games": c_games,
            "wins": int(pick(c, "wins", default=0) or 0),
            "top3Rate": as_percent(pick(c, "top3Rate", "top3"), c_games),
            "avgRank": pick(c, "averageRank"),
        })

    wins = int(pick(stats, "totalWins", default=0) or 0)
    return {
        **entry,
        "status": "ok",
        "rp": rp,
        **tier_of(rp, srank or rank),
        "rank": rank,
        "serverRank": srank,
        "rankSize": rank_size,
        "rankPercent": rank_percent,
        "games": games,
        "wins": wins,
        "winRate": round(wins / games * 100, 1) if games else None,
        "top3Rate": as_percent(pick(stats, "top3"), games),
        "avgRank": pick(stats, "averageRank"),
        "avgKills": pick(stats, "averageKills"),
        "mains": mains,
    }


def best_peak(p: dict, season_peak: dict | None, current_label: str,
              history_labels: dict[str, str], manual: dict | None) -> dict | None:
    candidates = []
    if season_peak:
        candidates.append({**season_peak, "season": current_label, "source": "current"})
    for sid, rec in (p.get("history") or {}).items():
        if rec:
            candidates.append({**rec, "season": history_labels.get(sid, season_label(None, int(sid))), "source": "api"})
    if manual:
        candidates.append({**manual, "source": "manual"})
    if not candidates:
        return None
    best = max(candidates, key=tier_key)
    return {k: best.get(k) for k in ("tier", "tierName", "division", "rp", "season", "source")}


# ── 파일 ─────────────────────────────────────────────────────────────────
def read_players(path: Path) -> list[tuple[str, str | None]]:
    players, seen = [], set()
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        nick, _, memo = line.partition(",")
        nick, memo = nick.strip(), memo.strip() or None
        if nick and nick.lower() not in seen:
            seen.add(nick.lower())
            players.append((nick, memo))
    return players


TIER_TEXT = re.compile(r"^([가-힣]+)\s*([1-4])?$")


def read_past_peaks(path: Path) -> dict[str, dict]:
    """past_peaks.txt: '닉네임, 티어[, 시즌]' → {닉네임(소문자): 티어 정보}. 한 사람이 여러 줄이면 가장 높은 것."""
    result: dict[str, dict] = {}
    if not path.exists():
        return result
    for no, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 2:
            log(f"  past_peaks.txt {no}번째 줄을 건너뛰어요 (형식: 닉네임, 티어, 시즌): {line}")
            continue
        m = TIER_TEXT.match(parts[1].replace(" ", ""))
        tier = TIER_ALIASES.get(m.group(1)) if m else None
        if not tier:
            log(f"  past_peaks.txt {no}번째 줄의 티어를 못 읽었어요: {parts[1]}")
            continue
        has_div = tier in [k for k, _, s in TIERS if len(s) > 1]
        division = (int(m.group(2)) if m.group(2) else 4) if has_div else None
        season = parts[2] if len(parts) > 2 and parts[2] else None
        if season and season.isdigit():
            season = f"시즌 {season}"
        rec = {"tier": tier, "tierName": TIER_NAMES[tier], "division": division,
               "rp": tier_floor(tier, division), "season": season}
        key = parts[0].lower()
        if tier_key(rec) > tier_key(result.get(key)):
            result[key] = rec
    return result


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        k, sep, v = line.strip().partition("=")
        if sep and not k.startswith("#") and k.strip() not in os.environ:
            os.environ[k.strip()] = v.strip().strip('"').strip("'")


def describe(t: dict | None) -> str:
    if not t:
        return "-"
    div = f" {t['division']}" if t.get("division") else ""
    return f"{t['tierName']}{div}"


def summary(p: dict) -> str:
    peak = p.get("peak")
    peak_txt = f" / 최고: {describe(peak)} ({peak.get('season') or '직접 입력'})" if peak else ""
    if p.get("status") == "ok":
        mains = ", ".join(m["name"] for m in p.get("mains", [])) or "-"
        return f"현재: {describe(p)} {p['rp']:,} RP{peak_txt} / {p['games']}판 / 주챔: {mains}"
    status = {"not_found": "닉네임을 찾을 수 없음", "unranked": "이번 시즌 랭크 기록 없음"}.get(p.get("status"), p.get("error", "오류"))
    return status + peak_txt


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # 어떤 콘솔에서도 한글 출력 때문에 멈추지 않게
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--players", type=Path, default=ROOT / "players.txt")
    ap.add_argument("--past", type=Path, default=ROOT / "past_peaks.txt")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "players.json")
    args = ap.parse_args()

    load_env_file(ROOT / ".env")
    key = os.environ.get("ER_API_KEY", "").strip()
    if not key:
        log("ER_API_KEY가 없어요. GitHub Secrets 또는 .env 파일에 API 키를 넣어 주세요.")
        return 1

    players = read_players(args.players)
    if not players:
        log(f"{args.players}에 닉네임이 없어요.")
        return 1
    past = read_past_peaks(args.past)

    previous, previous_season = {}, None
    if args.out.exists():
        try:
            old = json.loads(args.out.read_text(encoding="utf-8"))
            previous = {(p.get("query") or p["nickname"]).lower(): p for p in old.get("players", [])}
            previous_season = old.get("seasonId")
        except (json.JSONDecodeError, KeyError, AttributeError):
            previous, previous_season = {}, None

    api = Api(key, os.environ.get("ER_API_BASE", "https://open-api.bser.io"), float(os.environ.get("ER_DELAY", "1.1")))
    log("시즌 정보를 불러오는 중...")
    try:
        season_id, season_names = resolve_seasons(api)
    except ApiError as e:
        hint = " API 키가 맞는지, 호출 제한에 걸리지 않았는지 확인해 주세요." if e.status in (401, 403, 429) else ""
        log(f"시즌 정보를 불러오지 못했어요 ({e}).{hint}")
        return 1
    season_name = season_names.get(season_id)
    history_ids = history_season_ids(season_id, season_names)
    history_labels = {str(s): season_label(season_names.get(s), s) for s in history_ids}
    log(f"  현재 시즌 ID {season_id} ({season_name or '이름 없음'}), "
        f"지난 시즌 확인: {', '.join(history_labels.values()) or '없음'}")
    log("캐릭터 이름을 불러오는 중...")
    names = character_names(api)
    log(f"  캐릭터 {len(names)}명")

    results, failed = [], 0
    for i, (nick, memo) in enumerate(players, 1):
        old = previous.get(nick.lower()) or {}
        same_season = previous_season == season_id
        try:
            p = fetch_player(api, nick, season_id, names, history_ids, old.get("history") or {})
        except ApiError as e:
            failed += 1
            if old.get("status") == "ok" and same_season:
                p = {**old, "stale": True}
            else:
                p = {"nickname": nick, "status": "error", "history": old.get("history") or {}}
            p["error"] = str(e)

        # 이번 시즌 최고 RP: 지난번 저장값과 지금 값 중 높은 것
        season_peak = old.get("seasonPeak") if same_season else None
        if p.get("status") == "ok":
            now = {k: p.get(k) for k in ("rp", "tier", "tierName", "division")}
            if tier_key(now) > tier_key(season_peak):
                season_peak = now
        if season_peak:
            p["seasonPeak"] = season_peak
        p["peak"] = best_peak(p, season_peak, season_label(season_name, season_id), history_labels, past.get(nick.lower()))
        if memo:
            p["memo"] = memo
        p["query"] = nick
        results.append(p)
        log(f"[{i}/{len(players)}] {nick}: {summary(p)}{' (지난 데이터 유지)' if p.get('stale') else ''}")

    unknown = set(past) - {n.lower() for n, _ in players}
    if unknown:
        log(f"  past_peaks.txt에만 있고 players.txt에 없는 닉네임: {', '.join(sorted(unknown))}")

    if failed == len(players):
        log("모든 조회가 실패해서 기존 데이터를 그대로 둡니다. API 키와 호출 제한을 확인해 주세요.")
        return 1

    out = {
        "updatedAt": datetime.now(KST).isoformat(timespec="seconds"),
        "seasonId": season_id,
        "seasonName": season_name,
        "historySeasons": history_labels,
        "matchingMode": MATCHING_MODE_RANKED,
        "tierOrder": TIER_ORDER,
        "players": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(args.out)
    log(f"완료: {len(results)}명 저장 (실패 {failed}명, API 호출 {api.calls}회) → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
