#!/usr/bin/env python3
"""
Stock Daily Digest — 每日股票資料抓取與分析
台股: TWSE API | 美股: yfinance
"""

import json
import os
import time
import requests
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, date
from pathlib import Path
import traceback

# ── 設定 ────────────────────────────────────────────────────────────────────

TAIWAN_STOCKS = {
    "2330": "台積電",
    "3037": "欣興",
}

US_STOCKS = {
    "NVDA": "NVIDIA",
    "TSLA": "Tesla",
}

ALERT_THRESHOLDS = {
    "change_pct": 5.0,   # 單日漲跌幅觸發門檻 (%)
    "rsi_overbought": 70,
    "rsi_oversold": 30,
}

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

DATA_DIR   = Path(__file__).parent.parent / "docs" / "data"
TODAY_STR  = datetime.utcnow().strftime("%Y-%m-%d")

# ── 工具函式 ─────────────────────────────────────────────────────────────────

def calc_rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff().dropna()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 2) if not rsi.empty else None

def calc_indicators(close: pd.Series) -> dict:
    """計算所有技術指標"""
    sma7  = close.rolling(7).mean().iloc[-1]
    sma20 = close.rolling(20).mean().iloc[-1]
    ema12 = close.ewm(span=12, adjust=False).mean().iloc[-1]
    ema26 = close.ewm(span=26, adjust=False).mean().iloc[-1]
    rsi   = calc_rsi(close)

    # 52 週高低點 (最多取 252 個交易日)
    window = close.tail(252)
    high52 = round(float(window.max()), 2)
    low52  = round(float(window.min()), 2)

    return {
        "sma7":   round(float(sma7),  2) if not pd.isna(sma7)  else None,
        "sma20":  round(float(sma20), 2) if not pd.isna(sma20) else None,
        "ema12":  round(float(ema12), 2) if not pd.isna(ema12) else None,
        "ema26":  round(float(ema26), 2) if not pd.isna(ema26) else None,
        "rsi14":  rsi,
        "high52": high52,
        "low52":  low52,
    }

def detect_alerts(symbol: str, name: str, price: float, change_pct: float,
                  indicators: dict) -> list[dict]:
    """偵測需要 Telegram 通知的異常訊號"""
    alerts = []
    abs_chg = abs(change_pct)

    if abs_chg >= ALERT_THRESHOLDS["change_pct"]:
        direction = "🚀 急漲" if change_pct > 0 else "💥 急跌"
        alerts.append({
            "type": "price_surge",
            "msg": f"{direction} {symbol} ({name}) 單日 {change_pct:+.2f}%，現價 {price}"
        })

    rsi = indicators.get("rsi14")
    if rsi is not None:
        if rsi >= ALERT_THRESHOLDS["rsi_overbought"]:
            alerts.append({
                "type": "rsi_overbought",
                "msg": f"📈 超買訊號 {symbol} ({name}) RSI={rsi:.1f}，留意回檔風險"
            })
        elif rsi <= ALERT_THRESHOLDS["rsi_oversold"]:
            alerts.append({
                "type": "rsi_oversold",
                "msg": f"📉 超賣訊號 {symbol} ({name}) RSI={rsi:.1f}，留意反彈機會"
            })

    high52 = indicators.get("high52")
    low52  = indicators.get("low52")
    if high52 and price >= high52 * 0.999:
        alerts.append({
            "type": "high52",
            "msg": f"🏆 52週新高 {symbol} ({name}) 現價 {price} 突破年高 {high52}"
        })
    if low52 and price <= low52 * 1.001:
        alerts.append({
            "type": "low52",
            "msg": f"⚠️ 52週新低 {symbol} ({name}) 現價 {price} 跌破年低 {low52}"
        })

    return alerts

# ── 台股資料 (TWSE) ──────────────────────────────────────────────────────────

def _fetch_twse_month(symbol: str, yyyymm: str) -> list:
    """抓單一月份的收盤紀錄，回傳 [(date_str, close), ...]"""
    try:
        url = (
            f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
            f"?stockNo={symbol}&date={yyyymm}01&response=json"
        )
        resp = requests.get(url, timeout=15)
        data = resp.json()
        if data.get("stat") != "OK":
            return []
        records = []
        for row in data.get("data", []):
            date_parts = row[0].replace("/", "-").split("-")
            year = int(date_parts[0]) + 1911
            date_str = f"{year}-{date_parts[1]}-{date_parts[2]}"
            close_str = row[6].replace(",", "")
            try:
                records.append((date_str, float(close_str)))
            except ValueError:
                continue
        return records
    except Exception:
        return []


def fetch_twse_history(symbol: str) -> pd.Series | None:
    """
    抓最近 3 個月的台股日收盤，確保有足夠資料點計算指標。
    """
    records = []
    now = datetime.utcnow()
    for i in range(3):                          # 本月、上月、上上月
        ym = (now.replace(day=1) - timedelta(days=1) * (i * 28))
        yyyymm = ym.strftime("%Y%m")
        month_data = _fetch_twse_month(symbol, yyyymm)
        records.extend(month_data)
        time.sleep(0.4)                         # 避免 rate limit

    if not records:
        return None

    series = pd.Series(
        {r[0]: r[1] for r in records},
        dtype=float,
    )
    series.index = pd.to_datetime(series.index)
    return series.sort_index()


def fetch_twse_quote(symbol: str) -> dict | None:
    """當日即時/最新報價 (盤後)"""
    try:
        url = (
            f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
            f"?ex_ch=tse_{symbol}.tw&json=1&delay=0"
        )
        resp = requests.get(url, timeout=10,
                            headers={"Referer": "https://mis.twse.com.tw/"})
        data = resp.json()
        items = data.get("msgArray", [])
        if not items:
            return None
        item = items[0]

        price = float(item.get("z", item.get("y", 0)) or item.get("y", 0))
        prev  = float(item.get("y", price))
        change     = round(price - prev, 2)
        change_pct = round((change / prev) * 100, 2) if prev else 0.0

        return {
            "price":      round(price, 2),
            "change":     change,
            "change_pct": change_pct,
            "volume":     int(float(item.get("v", 0) or 0)),
            "open":       round(float(item.get("o", price) or price), 2),
            "high":       round(float(item.get("h", price) or price), 2),
            "low":        round(float(item.get("l", price) or price), 2),
        }
    except Exception:
        traceback.print_exc()
        return None


def process_taiwan_stock(symbol: str, name: str) -> dict | None:
    print(f"  台股 {symbol} ({name})...")
    time.sleep(0.5)  # 避免 rate limit

    history = fetch_twse_history(symbol)
    if history is None or len(history) < 5:
        print(f"    ⚠ 歷史資料不足，跳過")
        return None

    # 嘗試取當日報價
    quote = fetch_twse_quote(symbol)
    if quote:
        price      = quote["price"]
        change     = quote["change"]
        change_pct = quote["change_pct"]
        volume     = quote["volume"]
        open_p     = quote["open"]
        high_p     = quote["high"]
        low_p      = quote["low"]
    else:
        # 使用歷史最後一筆
        price      = float(history.iloc[-1])
        prev_price = float(history.iloc[-2]) if len(history) > 1 else price
        change     = round(price - prev_price, 2)
        change_pct = round((change / prev_price) * 100, 2) if prev_price else 0.0
        volume     = 0
        open_p = high_p = low_p = price

    indicators = calc_indicators(history)
    alerts = detect_alerts(symbol, name, price, change_pct, indicators)

    return {
        "symbol":     symbol,
        "name":       name,
        "market":     "TW",
        "currency":   "TWD",
        "price":      price,
        "change":     change,
        "change_pct": change_pct,
        "volume":     volume,
        "open":       open_p,
        "high":       high_p,
        "low":        low_p,
        "indicators": indicators,
        "alerts":     alerts,
        "updated_at": TODAY_STR,
    }

# ── 美股資料 (yfinance) ───────────────────────────────────────────────────────

def process_us_stock(symbol: str, name: str) -> dict | None:
    print(f"  美股 {symbol} ({name})...")
    try:
        ticker = yf.Ticker(symbol)
        hist   = ticker.history(period="1y")

        if hist.empty or len(hist) < 20:
            print(f"    ⚠ 資料不足，跳過")
            return None

        close  = hist["Close"]
        price  = round(float(close.iloc[-1]), 2)
        prev   = round(float(close.iloc[-2]), 2) if len(close) > 1 else price
        change     = round(price - prev, 2)
        change_pct = round((change / prev) * 100, 2) if prev else 0.0

        last_row = hist.iloc[-1]
        volume   = int(last_row["Volume"])
        open_p   = round(float(last_row["Open"]), 2)
        high_p   = round(float(last_row["High"]), 2)
        low_p    = round(float(last_row["Low"]),  2)

        indicators = calc_indicators(close)
        alerts = detect_alerts(symbol, name, price, change_pct, indicators)

        return {
            "symbol":     symbol,
            "name":       name,
            "market":     "US",
            "currency":   "USD",
            "price":      price,
            "change":     change,
            "change_pct": change_pct,
            "volume":     volume,
            "open":       open_p,
            "high":       high_p,
            "low":        low_p,
            "indicators": indicators,
            "alerts":     alerts,
            "updated_at": TODAY_STR,
        }
    except Exception:
        traceback.print_exc()
        return None

# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(msg: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  ⚠ Telegram 未設定，跳過")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       msg,
        "parse_mode": "HTML",
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.ok
    except Exception:
        traceback.print_exc()
        return False


def build_daily_summary(stocks: list[dict]) -> str:
    lines = [f"📊 <b>每日股票摘要</b> — {TODAY_STR}\n"]

    tw = [s for s in stocks if s["market"] == "TW"]
    us = [s for s in stocks if s["market"] == "US"]

    if tw:
        lines.append("🇹🇼 <b>台股</b>")
        for s in tw:
            arrow = "▲" if s["change_pct"] >= 0 else "▼"
            lines.append(
                f"  {arrow} {s['symbol']} {s['name']}  "
                f"NT${s['price']:,.0f}  {s['change_pct']:+.2f}%  "
                f"RSI {s['indicators'].get('rsi14', 'N/A')}"
            )

    if us:
        lines.append("\n🇺🇸 <b>美股</b>")
        for s in us:
            arrow = "▲" if s["change_pct"] >= 0 else "▼"
            lines.append(
                f"  {arrow} {s['symbol']} {s['name']}  "
                f"${s['price']:,.2f}  {s['change_pct']:+.2f}%  "
                f"RSI {s['indicators'].get('rsi14', 'N/A')}"
            )

    return "\n".join(lines)


def send_notifications(stocks: list[dict]):
    print("\n📨 發送 Telegram 通知...")

    # 每日固定摘要
    summary = build_daily_summary(stocks)
    send_telegram(summary)

    # 異常警報
    for s in stocks:
        for alert in s.get("alerts", []):
            send_telegram(f"🔔 <b>股票警報</b>\n{alert['msg']}")
            time.sleep(0.3)

# ── 資料儲存 ──────────────────────────────────────────────────────────────────

def save_data(stocks: list[dict]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 今日快照
    today_file = DATA_DIR / f"{TODAY_STR}.json"
    with open(today_file, "w", encoding="utf-8") as f:
        json.dump({"date": TODAY_STR, "stocks": stocks}, f,
                  ensure_ascii=False, indent=2)
    print(f"  ✓ 今日資料: {today_file}")

    # 更新 latest.json（前端主要讀這個）
    latest_file = DATA_DIR / "latest.json"
    with open(latest_file, "w", encoding="utf-8") as f:
        json.dump({"date": TODAY_STR, "stocks": stocks}, f,
                  ensure_ascii=False, indent=2)
    print(f"  ✓ latest.json 更新")

    # 更新 index.json（日期清單，供前端切換日期）
    index_file = DATA_DIR / "index.json"
    existing = []
    if index_file.exists():
        with open(index_file, "r", encoding="utf-8") as f:
            existing = json.load(f).get("dates", [])

    if TODAY_STR not in existing:
        existing.append(TODAY_STR)
    existing.sort(reverse=True)

    # 只保留最近 180 天
    cutoff = (datetime.utcnow() - timedelta(days=180)).strftime("%Y-%m-%d")
    existing = [d for d in existing if d >= cutoff]

    with open(index_file, "w", encoding="utf-8") as f:
        json.dump({"dates": existing}, f, ensure_ascii=False, indent=2)
    print(f"  ✓ index.json 更新（共 {len(existing)} 筆）")

    # 清理超過 180 天的舊檔
    for p in DATA_DIR.glob("20*.json"):
        if p.name < f"{cutoff}.json" and p.name not in ("latest.json", "index.json"):
            p.unlink()
            print(f"  🗑 刪除舊檔: {p.name}")

# ── 主程式 ────────────────────────────────────────────────────────────────────

def main():
    print(f"=== Stock Daily Digest {TODAY_STR} ===\n")
    stocks = []

    print("【台股】")
    for symbol, name in TAIWAN_STOCKS.items():
        result = process_taiwan_stock(symbol, name)
        if result:
            stocks.append(result)

    print("\n【美股】")
    for symbol, name in US_STOCKS.items():
        result = process_us_stock(symbol, name)
        if result:
            stocks.append(result)

    if not stocks:
        print("❌ 沒有任何資料，結束")
        return

    print(f"\n✅ 共處理 {len(stocks)} 檔股票")

    print("\n💾 儲存資料...")
    save_data(stocks)

    send_notifications(stocks)

    print("\n=== 完成 ===")


if __name__ == "__main__":
    main()
