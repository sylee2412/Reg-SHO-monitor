"""
Reg SHO Threshold Security Monitor
====================================
Flask 서버 + APScheduler로 24/7 운영
- NASDAQ에서 매일 데이터 자동 다운로드
- 연속 등재일(streak) 계산
- 신규 추가/제외 종목 감지
- Rule 3210 위반 여부 추적
- CSV 내보내기, 워치리스트 지원
"""

import json
import os
import threading
import time
import io
import csv
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, render_template, Response

# ─── 상수 ──────────────────────────────────────────────────────────
BASE_URL        = "http://www.nasdaqtrader.com/dynamic/symdir/regsho/nasdaqth{date}.txt"
DATA_DIR        = os.path.join(os.path.dirname(__file__), "data")
CACHE_FILE      = os.path.join(DATA_DIR, "cache.json")
HISTORY_FILE    = os.path.join(DATA_DIR, "history.json")
MAX_LOOKBACK_CAL   = 120   # 최대 120 캘린더일 검색
MAX_STREAK_DAYS    = 60    # streak 계산 최대 거래일
CLOSEOUT_DAYS      = 13    # Reg SHO Rule 203(b)(3) 강제청산 기준
ET_ZONE         = ZoneInfo("America/New_York")

# ─── Flask 앱 ─────────────────────────────────────────────────────
app = Flask(__name__)
os.makedirs(DATA_DIR, exist_ok=True)

# ─── JSON 헬퍼 ────────────────────────────────────────────────────
def load_json(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ─── NASDAQ 파싱 ──────────────────────────────────────────────────
def parse_regsho_file(text: str) -> dict:
    """파이프 구분 Reg SHO 파일 → {symbol: {name,market,rule3210}} 딕셔너리"""
    result = {}
    for line in text.strip().splitlines()[1:]:   # 헤더 스킵
        parts = line.split("|")
        if len(parts) < 4:
            continue
        sym = parts[0].strip()
        if not sym or sym[:8].isdigit():          # 타임스탬프 행 스킵
            continue
        result[sym] = {
            "name":    parts[1].strip() if len(parts) > 1 else "",
            "market":  parts[2].strip() if len(parts) > 2 else "",
            "rule3210": parts[4].strip() if len(parts) > 4 else "N",
        }
    return result

MARKET_LABELS = {
    "G": "Global Select",
    "S": "Capital Market",
    "Q": "Global Market",
}

def market_label(code: str) -> str:
    return MARKET_LABELS.get(code, code)

# ─── 거래일 목록 ──────────────────────────────────────────────────
def prev_trading_days(start: date, n: int) -> list:
    """start 날짜부터 과거 n 거래일(평일) 리스트 반환"""
    days, cur, checked = [], start, 0
    while len(days) < n and checked < MAX_LOOKBACK_CAL:
        if cur.weekday() < 5:
            days.append(cur)
        cur -= timedelta(days=1)
        checked += 1
    return days

# ─── 데이터 수집 ──────────────────────────────────────────────────
def fetch_day(d: date) -> dict | None:
    url = BASE_URL.format(date=d.strftime("%Y%m%d"))
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200 and "|" in r.text:
            parsed = parse_regsho_file(r.text)
            return parsed if parsed else None   # 빈 파일(미게시) → None
    except Exception:
        pass
    return None

def update_history() -> dict:
    """히스토리 파일 업데이트. 새 날짜만 다운로드."""
    history = load_json(HISTORY_FILE)
    today   = date.today()
    days    = prev_trading_days(today, MAX_STREAK_DAYS + 10)

    new_cnt = 0
    for d in days:
        key = d.strftime("%Y%m%d")
        if key in history:
            continue
        data = fetch_day(d)
        if data is not None:
            history[key] = data
            new_cnt += 1
            print(f"  [+] {key}: {len(data)} 종목")
        time.sleep(0.25)

    # 오래된 항목 정리
    cutoff = (today - timedelta(days=MAX_LOOKBACK_CAL + 10)).strftime("%Y%m%d")
    history = {k: v for k, v in history.items() if k >= cutoff}

    save_json(HISTORY_FILE, history)
    print(f"[history] {new_cnt}개 날짜 신규 수집.")
    return history

# ─── 분석 ─────────────────────────────────────────────────────────
def analyze(history: dict) -> dict:
    if not history:
        return {}

    # 데이터가 있는 날짜만, 최신순
    valid_dates = sorted(
        [k for k, v in history.items() if v],
        reverse=True
    )
    if not valid_dates:
        return {}

    today_key  = valid_dates[0]
    today_data = history[today_key]

    prev_key   = valid_dates[1] if len(valid_dates) > 1 else None
    prev_data  = history.get(prev_key, {}) if prev_key else {}

    today_syms = set(today_data)
    prev_syms  = set(prev_data)

    added_today   = sorted(today_syms - prev_syms)
    removed_today = sorted(prev_syms  - today_syms)

    # ── streak 계산 ─────────────────────────────────────────────
    securities = []
    for sym, info in today_data.items():
        streak        = 0
        hist_flags    = []
        max_streak    = 0   # 현재 streak 중 최장

        for dk in valid_dates[:MAX_STREAK_DAYS]:
            present = sym in history.get(dk, {})
            hist_flags.append({"date": dk, "in": present})
            if present:
                streak     += 1
                max_streak  = streak
            else:
                break

        pct             = min(100, round(streak / CLOSEOUT_DAYS * 100))
        days_remaining  = max(0, CLOSEOUT_DAYS - streak)

        if streak >= 11:
            risk = "danger"
        elif streak >= 8:
            risk = "warning"
        else:
            risk = "safe"

        first_key  = valid_dates[streak - 1] if streak <= len(valid_dates) else valid_dates[-1]
        first_date = f"{first_key[:4]}-{first_key[4:6]}-{first_key[6:]}"

        securities.append({
            "symbol":        sym,
            "name":          info.get("name", ""),
            "market":        info.get("market", ""),
            "market_label":  market_label(info.get("market", "")),
            "rule3210":      info.get("rule3210", "N"),
            "streak":        streak,
            "days_remaining": days_remaining,
            "pct":           pct,
            "risk":          risk,
            "first_date":    first_date,
            "is_new":        sym in added_today,
            "history_flags": hist_flags[:30],
        })

    securities.sort(key=lambda x: (-x["streak"], x["symbol"]))

    # ── 제거 종목 상세 ──────────────────────────────────────────
    removed_detail = []
    for sym in removed_today:
        info   = prev_data.get(sym, {})
        streak = 0
        for dk in valid_dates[1:]:
            if sym in history.get(dk, {}):
                streak += 1
            else:
                break
        removed_detail.append({
            "symbol":           sym,
            "name":             info.get("name", ""),
            "market_label":     market_label(info.get("market", "")),
            "streak_at_removal": streak,
            "rule3210":         info.get("rule3210", "N"),
        })

    ref_d = today_key
    return {
        "securities":    securities,
        "added_today":   added_today,
        "removed_today": removed_detail,
        "ref_date":      f"{ref_d[:4]}-{ref_d[4:6]}-{ref_d[6:]}",
        "prev_date":     f"{prev_key[:4]}-{prev_key[4:6]}-{prev_key[6:]}" if prev_key else None,
        "summary": {
            "danger":   sum(1 for s in securities if s["risk"] == "danger"),
            "warning":  sum(1 for s in securities if s["risk"] == "warning"),
            "safe":     sum(1 for s in securities if s["risk"] == "safe"),
            "total":    len(securities),
            "rule3210": sum(1 for s in securities if s["rule3210"] == "Y"),
            "new_today": len(added_today),
            "removed_today": len(removed_today),
        },
        "last_updated": datetime.now(ET_ZONE).strftime("%Y-%m-%d %H:%M:%S ET"),
    }

# ─── 캐시 재생성 ─────────────────────────────────────────────────
_lock = threading.Lock()

def rebuild_cache():
    print(f"\n[rebuild] 시작 @ {datetime.now().strftime('%H:%M:%S')}")
    history = update_history()
    result  = analyze(history)
    if result:
        with _lock:
            save_json(CACHE_FILE, result)
        s = result["summary"]
        print(f"[rebuild] 완료 → 전체:{s['total']} 위험:{s['danger']} 경고:{s['warning']} 안전:{s['safe']}")
    else:
        print("[rebuild] 분석 실패 (데이터 없음)")

# ─── Flask 라우트 ─────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/data")
def api_data():
    with _lock:
        cache = load_json(CACHE_FILE)
    if not cache:
        return jsonify({"error": "데이터 준비 중입니다. 잠시 후 새로고침하세요."}), 503
    return jsonify(cache)

@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    threading.Thread(target=rebuild_cache, daemon=True).start()
    return jsonify({"status": "ok", "message": "백그라운드 업데이트 시작됨"})

@app.route("/api/history/<symbol>")
def api_history(symbol: str):
    """개별 종목 히스토리 반환 (최근 30거래일)"""
    history = load_json(HISTORY_FILE)
    valid   = sorted([k for k, v in history.items() if v], reverse=True)[:30]
    flags   = [{"date": f"{k[:4]}-{k[4:6]}-{k[6:]}", "in": symbol.upper() in history.get(k, {})} for k in valid]
    return jsonify({"symbol": symbol.upper(), "history": flags})

@app.route("/api/export/csv")
def export_csv():
    cache = load_json(CACHE_FILE)
    if not cache:
        return "데이터 없음", 503
    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["Symbol", "Name", "Market", "Streak(Days)", "Days Remaining",
                "Risk", "First Listed", "% to Deadline", "Rule 3210"])
    for s in cache.get("securities", []):
        w.writerow([s["symbol"], s["name"], s["market_label"],
                    s["streak"], s["days_remaining"], s["risk"].upper(),
                    s["first_date"], f"{s['pct']}%", s["rule3210"]])
    output.seek(0)
    fname = f"regsho_{cache.get('ref_date','').replace('-','')}.csv"
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})

# ─── 스케줄러 ─────────────────────────────────────────────────────
def start_scheduler():
    sched = BackgroundScheduler(timezone=ET_ZONE)
    # NASDAQ은 통상 미동부 오후 10시~11시 사이 파일 게시
    sched.add_job(rebuild_cache, "cron", hour=22, minute=30, id="nightly")
    # 오전 장 시작 전 확인
    sched.add_job(rebuild_cache, "cron", hour=7,  minute=0,  id="morning")
    sched.start()
    print("[scheduler] 등록: 매일 07:00, 22:30 ET")
    return sched

# ─── 진입점 ──────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 58)
    print("  Reg SHO Threshold Monitor  |  http://localhost:5000")
    print("=" * 58)

    cache_exists = os.path.exists(CACHE_FILE) and os.path.getsize(CACHE_FILE) > 200
    if cache_exists:
        print("[init] 기존 캐시 감지 → 백그라운드 업데이트")
        threading.Thread(target=rebuild_cache, daemon=True).start()
    else:
        print("[init] 첫 실행 — 데이터 다운로드 중... (1~2분 소요)")
        rebuild_cache()

    sched = start_scheduler()
    try:
        app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
    finally:
        sched.shutdown()
