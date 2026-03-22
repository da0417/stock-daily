#!/usr/bin/env python3
"""
台股全市場選股器

篩選條件：
  1. 今日成交量 > 昨日成交量 × 2
  2. 今日成交量 > 5 日均量
  3. 均線多頭排列 (5MA > 10MA > 20MA > 60MA)
  4. 股價 > 23 日均線
  5. 股價 > 50 元

資料來源：FinMind API（TaiwanStockPrice / TaiwanStockInfo）
分析：Claude claude-sonnet-4-6 API
推播：Telegram Bot
"""

import json
import os
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
import traceback

# ── 設定 ─────────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
FINMIND_TOKEN      = os.environ.get("FINMIND_TOKEN", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

DATA_DIR   = Path(__file__).parent.parent / "docs" / "data"
CACHE_FILE = DATA_DIR / "screener_cache.json"
TODAY_STR  = datetime.utcnow().strftime("%Y-%m-%d")

CACHE_DAYS   = 65   # 保留天數（計算 60MA 最少需要 60 天）
FINMIND_URL  = "https://api.finmindtrade.com/api/v4/data"

# ── FinMind 資料抓取 ──────────────────────────────────────────────────────────

def fetch_stock_info() -> dict:
    """取得股票名稱對照表，回傳 {stock_id: name}（只用於顯示）"""
    params = {"dataset": "TaiwanStockInfo", "token": FINMIND_TOKEN}
    try:
        resp = requests.get(FINMIND_URL, params=params, timeout=30)
        data = resp.json()
        if data.get("status") != 200:
            print(f"  ⚠ TaiwanStockInfo 錯誤: {data.get('msg')}")
            return {}
        df = pd.DataFrame(data["data"])
        # 只取 4 位數字代碼（用於名稱顯示）
        df = df[df["stock_id"].str.match(r"^\d{4}$")]
        return dict(zip(df["stock_id"], df["stock_name"]))
    except Exception:
        traceback.print_exc()
        return {}


def fetch_prices_range(start_date: str, end_date: str) -> pd.DataFrame:
    """從 FinMind 抓全市場日線資料（指定日期區間，不限股票）"""
    params = {
        "dataset":    "TaiwanStockPrice",
        "start_date": start_date,
        "end_date":   end_date,
        "token":      FINMIND_TOKEN,
    }
    try:
        resp = requests.get(FINMIND_URL, params=params, timeout=120)
        data = resp.json()
        if data.get("status") != 200:
            print(f"  ⚠ TaiwanStockPrice 錯誤 status={data.get('status')}: {data.get('msg')}")
            return pd.DataFrame()
        raw = data.get("data", [])
        print(f"  FinMind 回傳筆數: {len(raw)}")
        if not raw:
            return pd.DataFrame()
        df = pd.DataFrame(raw)
        if df.empty:
            return df
        df["date"]           = pd.to_datetime(df["date"])
        df["close"]          = pd.to_numeric(df["close"],          errors="coerce")
        df["Trading_Volume"] = pd.to_numeric(df["Trading_Volume"], errors="coerce")
        # 過濾無效資料
        df = df[(df["close"] > 0) & (df["Trading_Volume"] > 0)]
        return df[["stock_id", "date", "close", "Trading_Volume"]].rename(
            columns={"Trading_Volume": "volume"}
        )
    except Exception:
        traceback.print_exc()
        return pd.DataFrame()

# ── 快取管理 ──────────────────────────────────────────────────────────────────

def load_cache() -> dict:
    """讀取快取 JSON → {stock_id: [{date, close, volume}, ...]}"""
    if not CACHE_FILE.exists():
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("stocks", {})
    except Exception:
        return {}


def save_cache(stocks: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_updated": TODAY_STR, "stocks": stocks},
                  f, ensure_ascii=False, separators=(",", ":"))
    size_mb = CACHE_FILE.stat().st_size / 1024 / 1024
    print(f"  ✓ 快取儲存完成（{len(stocks)} 檔, {size_mb:.1f} MB）")


def _records_from_df(df: pd.DataFrame, cutoff: str) -> dict:
    """將 DataFrame 轉換成快取格式 dict"""
    stocks = {}
    for sid, group in df.groupby("stock_id"):
        group = group.sort_values("date")
        records = [
            {
                "date":   row.date.strftime("%Y-%m-%d"),
                "close":  float(row.close),
                "volume": int(row.volume),
            }
            for row in group.itertuples()
            if row.date.strftime("%Y-%m-%d") >= cutoff
        ]
        if len(records) >= 5:
            stocks[str(sid)] = records
    return stocks


def _trading_days(start_date: str, end_date: str) -> list[str]:
    """產生 start~end 之間的所有平日清單（Mon-Fri，簡易近似交易日）"""
    days = []
    cur = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date,   "%Y-%m-%d")
    while cur <= end:
        if cur.weekday() < 5:   # 0=Mon … 4=Fri
            days.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return days


def bootstrap_cache() -> dict:
    """首次執行：逐日抓 CACHE_DAYS 天的歷史快取（每日一次 API 呼叫）"""
    print("  首次執行，建立初始快取（約需 1~3 分鐘）...")
    cutoff     = (datetime.utcnow() - timedelta(days=CACHE_DAYS)).strftime("%Y-%m-%d")
    start_date = cutoff
    end_date   = TODAY_STR

    trading_days = _trading_days(start_date, end_date)
    print(f"  預計抓取 {len(trading_days)} 個交易日...")

    all_dfs = []
    for i, day in enumerate(trading_days, 1):
        df_day = fetch_prices_range(day, day)
        if not df_day.empty:
            df_day = df_day[df_day["stock_id"].str.match(r"^\d{4}$")]
            if not df_day.empty:
                all_dfs.append(df_day)
        if i % 10 == 0:
            print(f"  進度：{i}/{len(trading_days)} 天")
        time.sleep(0.3)

    if not all_dfs:
        print("  ⚠ 無法取得歷史資料")
        return {}

    df = pd.concat(all_dfs, ignore_index=True)
    unique_dates = df["date"].dt.strftime("%Y-%m-%d").nunique()
    print(f"  共取得：{len(df)} 筆，{unique_dates} 個交易日，{df['stock_id'].nunique()} 檔股票")

    stocks = _records_from_df(df, cutoff)
    save_cache(stocks)
    return stocks


def update_cache(existing: dict) -> dict:
    """增量更新：抓最近 5 天新資料，merge 進現有快取"""
    print("  增量更新快取...")
    start_date = (datetime.utcnow() - timedelta(days=5)).strftime("%Y-%m-%d")
    end_date   = TODAY_STR
    cutoff     = (datetime.utcnow() - timedelta(days=CACHE_DAYS)).strftime("%Y-%m-%d")

    df = fetch_prices_range(start_date, end_date)
    if df.empty:
        print("  ⚠ FinMind 無新資料，繼續使用舊快取")
        return existing

    df = df[df["stock_id"].str.match(r"^\d{4}$")]
    for sid, group in df.groupby("stock_id"):
        sid = str(sid)
        group = group.sort_values("date")
        new_recs = [
            {
                "date":   row.date.strftime("%Y-%m-%d"),
                "close":  float(row.close),
                "volume": int(row.volume),
            }
            for row in group.itertuples()
        ]
        if not new_recs:
            continue

        old_recs = [r for r in existing.get(sid, []) if r["date"] >= cutoff]
        existing_dates = {r["date"] for r in old_recs}

        for rec in new_recs:
            if rec["date"] not in existing_dates:
                old_recs.append(rec)

        old_recs.sort(key=lambda x: x["date"])
        old_recs = [r for r in old_recs if r["date"] >= cutoff]

        if old_recs:
            existing[sid] = old_recs

    save_cache(existing)
    return existing

# ── 篩選邏輯 ──────────────────────────────────────────────────────────────────

def screen_stocks(stocks: dict) -> list[dict]:
    """
    套用 5 條篩選條件，回傳符合個股清單（依量倍率排序）。

    篩選條件：
      1. 今日成交量 > 昨日成交量 × 2
      2. 今日成交量 > 5 日均量（不含今日）
      3. 均線多頭排列：5MA > 10MA > 20MA > 60MA
      4. 股價 > 23MA
      5. 股價 > 50 元
    """
    results = []

    for sid, records in stocks.items():
        if len(records) < 62:  # 至少 62 筆才能計算 60MA + 昨日量
            continue

        closes  = pd.Series([r["close"]  for r in records], dtype=float)
        volumes = pd.Series([r["volume"] for r in records], dtype=float)

        close_today   = closes.iloc[-1]
        vol_today     = volumes.iloc[-1]
        vol_yesterday = volumes.iloc[-2]
        vol_5ma       = volumes.iloc[-6:-1].mean()  # 前 5 日均量（不含今日）

        ma5  = closes.tail(5).mean()
        ma10 = closes.tail(10).mean()
        ma20 = closes.tail(20).mean()
        ma23 = closes.tail(23).mean()
        ma60 = closes.tail(60).mean()

        cond1 = vol_today > vol_yesterday * 2
        cond2 = vol_today > vol_5ma
        cond3 = (ma5 > ma10) and (ma10 > ma20) and (ma20 > ma60)
        cond4 = close_today > ma23
        cond5 = close_today > 50

        if cond1 and cond2 and cond3 and cond4 and cond5:
            vol_ratio = round(vol_today / vol_yesterday, 2) if vol_yesterday > 0 else 0
            results.append({
                "stock_id":   sid,
                "close":      round(close_today, 2),
                "vol_today":  int(vol_today),
                "vol_ratio":  vol_ratio,
                "vol_vs_5ma": round(vol_today / vol_5ma, 2) if vol_5ma > 0 else 0,
                "ma5":        round(float(ma5),  2),
                "ma10":       round(float(ma10), 2),
                "ma20":       round(float(ma20), 2),
                "ma23":       round(float(ma23), 2),
                "ma60":       round(float(ma60), 2),
                "latest_date": records[-1]["date"],
            })

    results.sort(key=lambda x: x["vol_ratio"], reverse=True)
    return results

# ── Claude 分析 ───────────────────────────────────────────────────────────────

def analyze_with_claude(results: list[dict], stock_names: dict) -> str:
    """呼叫 Claude claude-sonnet-4-6 API 分析前 10 支候選股"""
    if not ANTHROPIC_API_KEY or not results:
        return ""

    top = results[:10]
    lines = []
    for r in top:
        name = stock_names.get(r["stock_id"], "")
        lines.append(
            f"- {r['stock_id']} {name}: 收{r['close']} "
            f"量倍率{r['vol_ratio']}x(vs昨日) / {r['vol_vs_5ma']}x(vs5MA) "
            f"| 5MA:{r['ma5']} 10MA:{r['ma10']} 20MA:{r['ma20']} "
            f"23MA:{r['ma23']} 60MA:{r['ma60']}"
        )

    prompt = (
        f"以下是 {TODAY_STR} 台股盤後，依爆量多頭排列條件選出的個股（前10名）：\n\n"
        + "\n".join(lines)
        + "\n\n請用繁體中文，針對每支股票簡要說明（2~3句）：均線型態、量能意義、短期操作注意事項。"
        "回覆控制在 600 字以內，使用條列格式，每股一條。"
    )

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-6",
                "max_tokens": 1024,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        data = resp.json()
        return data["content"][0]["text"]
    except Exception:
        traceback.print_exc()
        return ""

# ── Telegram 推播 ─────────────────────────────────────────────────────────────

def send_telegram(msg: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  ⚠ Telegram 未設定，跳過")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       msg,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        return resp.ok
    except Exception:
        traceback.print_exc()
        return False


def format_and_send(results: list[dict], analysis: str, stock_names: dict):
    total = len(results)

    if total == 0:
        send_telegram(f"📊 <b>台股選股結果</b> — {TODAY_STR}\n\n今日無符合條件個股")
        return

    lines = [
        f"📊 <b>台股選股結果</b> — {TODAY_STR}",
        f"共找到 <b>{total}</b> 檔符合條件\n",
        "🔥 <b>爆量多頭股（前15名）：</b>",
    ]

    for i, r in enumerate(results[:15], 1):
        name = stock_names.get(r["stock_id"], "")
        lines.append(
            f"{i}. <b>{r['stock_id']} {name}</b>  "
            f"收:{r['close']}  量:{r['vol_ratio']}x  "
            f"5MA:{r['ma5']}  20MA:{r['ma20']}"
        )

    lines.append(
        "\n⚙️ <i>篩選條件：量>昨日2x + >5日均量 + 多頭排列(5>10>20>60MA) + >23MA + 股價>50</i>"
    )

    send_telegram("\n".join(lines))

    if analysis:
        time.sleep(0.5)
        ai_msg = f"🤖 <b>Claude 分析（前10名）</b>\n\n{analysis}"
        if len(ai_msg) > 4000:
            ai_msg = ai_msg[:4000] + "..."
        send_telegram(ai_msg)

# ── 主程式 ────────────────────────────────────────────────────────────────────

def main():
    print(f"\n=== 台股選股器 {TODAY_STR} ===\n")

    if not FINMIND_TOKEN:
        print("❌ FINMIND_TOKEN 未設定，結束")
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 載入或建立快取
    print("【快取管理】")
    cache = load_cache()
    if not cache:
        cache = bootstrap_cache()
    else:
        cache = update_cache(cache)

    if not cache:
        print("❌ 快取建立失敗，結束")
        return

    # 2. 取得股票名稱（供顯示用）
    print("\n【取得股票名稱】")
    stock_names = fetch_stock_info()
    print(f"  取得 {len(stock_names)} 檔股票名稱")

    # 3. 執行篩選
    print(f"\n【執行篩選】")
    print(f"  掃描 {len(cache)} 檔股票...")
    results = screen_stocks(cache)
    print(f"  符合條件：{len(results)} 檔")

    # 4. Claude 分析
    print("\n【Claude 分析】")
    analysis = ""
    if ANTHROPIC_API_KEY and results:
        analysis = analyze_with_claude(results, stock_names)
        print(f"  分析完成（{len(analysis)} 字）")
    elif not ANTHROPIC_API_KEY:
        print("  ANTHROPIC_API_KEY 未設定，跳過")

    # 5. Telegram 推播
    print("\n【Telegram 推播】")
    format_and_send(results, analysis, stock_names)

    print("\n=== 選股完成 ===")


if __name__ == "__main__":
    main()
