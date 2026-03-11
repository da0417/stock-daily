# 📈 Stock Daily Digest

每日自動抓取台股、美股資料，計算技術指標，透過 GitHub Pages 顯示儀表板，並以 Telegram 推送通知。

## 追蹤清單

| 市場 | 代號   | 名稱   |
|------|--------|--------|
| 台股 | 2330   | 台積電 |
| 台股 | 3037   | 欣興   |
| 美股 | NVDA   | NVIDIA |
| 美股 | TSLA   | Tesla  |

## 功能

- 每天 UTC+8 06:00 自動執行（GitHub Actions）
- 台股收盤資料：TWSE 官方 API
- 美股資料：yfinance
- 技術指標：RSI(14)、SMA(7/20)、EMA(12/26)、52週高低點
- Telegram 通知：每日摘要 + 急漲跌(≥5%) + RSI超買超賣 + 52週突破
- 深色主題靜態儀表板（GitHub Pages）
- 自動清理 180 天前舊資料

## 部署步驟

### 1. Fork / Clone 此 Repo

```bash
git clone https://github.com/YOUR_NAME/stock-daily.git
cd stock-daily
```

### 2. 設定 GitHub Pages

Repo → Settings → Pages → Source 選 **`main` branch / `docs` folder**

### 3. 設定 Telegram Bot

1. 在 Telegram 搜尋 `@BotFather` → `/newbot` → 取得 `BOT_TOKEN`
2. 把 Bot 加入你的群組或直接對話，取得 `CHAT_ID`
   - 取得方式：`https://api.telegram.org/bot<BOT_TOKEN>/getUpdates`

### 4. 設定 GitHub Secrets

Repo → Settings → Secrets and variables → Actions → New repository secret

| Secret 名稱         | 值               |
|---------------------|------------------|
| `TELEGRAM_BOT_TOKEN` | 你的 Bot Token  |
| `TELEGRAM_CHAT_ID`   | 你的 Chat ID    |

### 5. 手動觸發測試

Repo → Actions → Daily Stock Update → Run workflow

### 6. 修改自選清單

編輯 `scripts/fetch_stocks.py` 最上方的設定區塊：

```python
TAIWAN_STOCKS = {
    "2330": "台積電",
    "3037": "欣興",
    # 新增："0050": "元大台灣50",
}

US_STOCKS = {
    "NVDA": "NVIDIA",
    "TSLA": "Tesla",
    # 新增："AAPL": "Apple",
}
```

## 後續擴充（預留）

- [ ] GPT-4o-mini 每日 AI 分析報告
- [ ] 大盤指數（加權指數、S&P 500、NASDAQ）
- [ ] 成交量異常偵測

## 資料說明

- 台股資料：TWSE 盤後資料，非即時
- 美股資料：yfinance，收盤後更新
- 僅供參考，不構成任何投資建議
