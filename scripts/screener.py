#!/usr/bin/env python3
"""
台股全市場選股器

篩選條件：
  1. 股價 > MA240（長線多頭）
  2. MA20 > MA60 > MA120（中期均線多頭排列）
  3. K 值黃金交叉 + K < 70（KD 動能確認，非超買）
  4. MACD 黃金交叉（DIF 上穿 Signal）
  5. RSI 50–75（有動能，未超買）
  6. 成交量 > 20 日均量 × 1.5（放量確認）
  7. 股價 > 50 元
  8. 60 日平均成交金額 > 1,000,000 元（流動性門檻）

資料來源：FinMind API（TaiwanStockPrice / TaiwanStockInfo）
分析：Claude claude-sonnet-4-6 API
推播：Telegram Bot
"""

import json
import os
import time
import requests
import numpy as np
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

CACHE_DAYS   = 360  # 保留天數（MA240 需要 240 交易日 ≈ 340 日曆天，加 buffer）
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
        df["max"]            = pd.to_numeric(df["max"],            errors="coerce")
        df["min"]            = pd.to_numeric(df["min"],            errors="coerce")
        df["Trading_Volume"] = pd.to_numeric(df["Trading_Volume"], errors="coerce")
        # 過濾無效資料
        df = df[(df["close"] > 0) & (df["Trading_Volume"] > 0)]
        return df[["stock_id", "date", "close", "max", "min", "Trading_Volume"]].rename(
            columns={"Trading_Volume": "volume", "max": "high", "min": "low"}
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
                "high":   float(row.high),
                "low":    float(row.low),
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
    print("  首次執行，建立初始快取（約需 5~10 分鐘，請耐心等候）...")
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
                "high":   float(row.high),
                "low":    float(row.low),
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

# ── 技術指標計算 ──────────────────────────────────────────────────────────────

def _calc_rsi(closes: pd.Series, period: int = 14) -> float:
    delta = closes.diff().dropna()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1]) if len(rsi) > 0 and not pd.isna(rsi.iloc[-1]) else 50.0


def _calc_kd(highs: pd.Series, lows: pd.Series, closes: pd.Series, n: int = 9) -> tuple:
    """回傳 (K今日, D今日, K昨日, D昨日)"""
    k, d = 50.0, 50.0
    prev_k, prev_d = 50.0, 50.0
    for i in range(len(closes)):
        if i < n - 1:
            continue
        h = highs.iloc[i - n + 1:i + 1].max()
        l = lows.iloc[i - n + 1:i + 1].min()
        rsv = (closes.iloc[i] - l) / (h - l) * 100 if h != l else 50.0
        prev_k, prev_d = k, d
        k = 2 / 3 * k + 1 / 3 * rsv
        d = 2 / 3 * d + 1 / 3 * k
    return k, d, prev_k, prev_d


def _calc_macd(closes: pd.Series) -> tuple:
    """回傳 (DIF今日, Signal今日, DIF昨日, Signal昨日)"""
    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    dif   = ema12 - ema26
    sig   = dif.ewm(span=9, adjust=False).mean()
    return float(dif.iloc[-1]), float(sig.iloc[-1]), float(dif.iloc[-2]), float(sig.iloc[-2])


# ── 篩選邏輯 ──────────────────────────────────────────────────────────────────

def screen_stocks(stocks: dict) -> list[dict]:
    """
    套用 8 條篩選條件，回傳符合個股清單（依成交量 vs 20MA 倍率排序）。

    篩選條件：
      1. 股價 > MA240
      2. MA20 > MA60 > MA120
      3. KD 黃金交叉 + K < 70
      4. MACD 黃金交叉
      5. RSI 50–75
      6. 成交量 > 20 日均量 × 1.5
      7. 股價 > 50 元
      8. 60 日平均成交金額 > 1,000,000 元
    """
    results = []

    for sid, records in stocks.items():
        if len(records) < 245:  # 需要 240 筆才能算 MA240
            continue

        closes  = pd.Series([r["close"]            for r in records], dtype=float)
        highs   = pd.Series([r.get("high", r["close"]) for r in records], dtype=float)
        lows    = pd.Series([r.get("low",  r["close"]) for r in records], dtype=float)
        volumes = pd.Series([r["volume"]           for r in records], dtype=float)

        close_today = closes.iloc[-1]
        vol_today   = volumes.iloc[-1]

        ma20  = float(closes.tail(20).mean())
        ma60  = float(closes.tail(60).mean())
        ma120 = float(closes.tail(120).mean())
        ma240 = float(closes.tail(240).mean())
        vol_20ma   = float(volumes.tail(20).mean())
        value_60ma = float((closes.tail(60) * volumes.tail(60)).mean())

        rsi = _calc_rsi(closes)
        k_today, d_today, k_prev, d_prev = _calc_kd(highs, lows, closes)
        dif_today, sig_today, dif_prev, sig_prev = _calc_macd(closes)

        cond1 = close_today > ma240
        cond2 = (ma20 > ma60) and (ma60 > ma120)
        cond3 = (k_today > d_today) and (k_prev < d_prev) and (k_today < 70)
        cond4 = (dif_today > sig_today) and (dif_prev <= sig_prev)
        cond5 = 50 <= rsi <= 75
        cond6 = vol_today > vol_20ma * 1.5
        cond7 = close_today > 50
        cond8 = value_60ma > 1_000_000

        if cond1 and cond2 and cond3 and cond4 and cond5 and cond6 and cond7 and cond8:
            results.append({
                "stock_id":    sid,
                "close":       round(close_today, 2),
                "ma20":        round(ma20,  2),
                "ma60":        round(ma60,  2),
                "ma120":       round(ma120, 2),
                "ma240":       round(ma240, 2),
                "rsi":         round(rsi, 1),
                "k":           round(k_today, 1),
                "d":           round(d_today, 1),
                "vol_vs_20ma": round(vol_today / vol_20ma, 2) if vol_20ma > 0 else 0,
                "latest_date": records[-1]["date"],
            })

    results.sort(key=lambda x: x["vol_vs_20ma"], reverse=True)
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
            f"量{r['vol_vs_20ma']}x(vs20MA) RSI:{r['rsi']} K:{r['k']} D:{r['d']} "
            f"| 20MA:{r['ma20']} 60MA:{r['ma60']} 120MA:{r['ma120']} 240MA:{r['ma240']}"
        )

    prompt = (
        f"以下是 {TODAY_STR} 台股盤後，依趨勢+動能條件選出的個股（前10名）：\n\n"
        + "\n".join(lines)
        + "\n\n請用繁體中文分析，格式規定如下（嚴格遵守）："
        "\n每支股票一行，格式：代號 名稱｜KD/MACD交叉意義｜短線操作注意"
        "\n例如：2330 台積電｜KD低檔交叉、MACD翻多，動能啟動｜留意920支撐，失守減碼"
        "\n不要使用 markdown，不要分段，不要標題，直接列出10行即可。"
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
            f"收:{r['close']}  量:{r['vol_vs_20ma']}x  "
            f"RSI:{r['rsi']}  K:{r['k']}  20MA:{r['ma20']}"
        )

    lines.append(
        "\n⚙️ <i>條件：>MA240 + MA20>60>120 + KD黃金交叉(K<70) + MACD黃金交叉 + RSI50-75 + 量>20MA×1.5 + 股價>50</i>"
    )

    send_telegram("\n".join(lines))

    if analysis:
        time.sleep(0.5)
        ai_msg = f"🤖 <b>Claude 快析</b>\n\n{analysis}"
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
