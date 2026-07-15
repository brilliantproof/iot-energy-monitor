# IoT Energy Monitor

> **⚠️ This is an independent public template, not the production deployment.**
> The actual running pipeline (with full historical data, real device config, and the live launchd schedule) lives at `~/analytics-hub/projects/rejyk/` — a separate, private codebase. This repo and that one are **not synced** and must not be merged or moved into each other; do that once already caused the production config to be lost. Treat this folder purely as a clean, shareable reference version of the code.

> **For AI assistants reading this file:**
> This project is a self-contained Python data pipeline. It connects to an IoT7 (七云物聯) electrical sensor API, downloads 10-minute interval data, classifies machine states, detects anomalies, and generates reports. All sensitive values (device ID, API credentials, Google Sheet ID) are stored in a local `config.json` that is gitignored. The entry point for downloading + analyzing is `scripts/download_iot7.py`. For analysis only, run `scripts/energy_analysis.py`.

A Python pipeline that pulls raw three-phase electrical data from IoT7 sensors every 10 minutes, detects power outages and unexpected machine stops, and produces daily summary reports with anomaly detection.

Built for a real factory environment.

---

## How It Works

```
IoT7 API
  → download_iot7.py     # Download data, refresh token
  → data/raw/*.csv       # Raw sensor records
  → energy_analysis.py   # Classify, aggregate, detect anomalies
  → analysis/output/     # Daily CSV + Markdown report
  → Google Sheets        # Optional push
```

**`download_iot7.py`**
- Auto-refreshes JWT token (main token: 4-day TTL, refresh token: 99-day TTL)
- Calculates the missing date range from existing CSVs and downloads only the gap
- Triggers `energy_analysis.py` after saving

**`energy_analysis.py`**

Each record is classified into one of three states:

| State | Definition |
|---|---|
| `RUNNING` | Current above threshold |
| `POWER_OUT` | Timestamp gap > 20 min (sensor lost power) |
| `UNEXPECTED_STOP` | Log exists but current = 0 within working hours |

Additional features:
- Auto-derives normal working hours from history (or accepts manual override)
- Calculates daily running hours, outage hours, unexpected stop hours
- Flags anomaly days using median ± 1.5σ
- Outputs `daily_summary.csv` and `baseline_report.md`
- Optional Google Sheets push

---

## Requirements

```bash
pip install pandas requests gspread google-auth
```

---

## Setup

### 1. Project config

```bash
cp config.example.json config.json
```

Edit `config.json` with your device details:

```json
{
  "device_id":             "YOUR_DEVICE_ID",
  "device_name":           "YOUR_DEVICE_NAME",
  "api_base":              "https://server.qiyunwulian.com:12341",
  "gsheet_enabled":        false,
  "gsheet_spreadsheet_id": "YOUR_GOOGLE_SHEET_ID",
  "gsheet_tab_name":       "Sheet1"
}
```

### 2. IoT7 Token

Token file lives outside the project at `~/.config/iot7/config.json`:

```json
{
  "tokenString":  "YOUR_JWT_TOKEN",
  "tokenString2": "YOUR_REFRESH_TOKEN"
}
```

To get these initially, and whenever `tokenString` expires: open the IoT7 app on the same Mac that runs this script, confirm it connects normally, then run `python3 scripts/refresh_token_from_app.py`. It reads the token straight out of the app's local macOS cache (`defaults read com.iot7.qiyunwulian`) — no login, no packet sniffing needed.

`download_iot7.py` also does this automatically: when `tokenString` expires, it first tries the refresh-token endpoint, and if that fails, falls back to reading the app's local cache the same way. So in practice you only need to keep opening the app occasionally (to check readings) — the script picks up the refreshed token on its own. You only need to run `refresh_token_from_app.py` manually if you want to force-check, or for debugging.

You only need to manually re-login in the app (and update `tokenString2`) when the refresh token itself expires (~99 days).

Note: this only works if you use the app on the same Mac. If you mainly check readings from your phone, the local-cache method won't see the phone app's token — you'd need a different capture method (e.g. proxying the phone's traffic) instead.

### 3. Google Sheets (optional)

Set `"gsheet_enabled": true` in `config.json` and use a **Service Account** (not OAuth user credentials — those expire every 7 days while the OAuth app is in testing mode, which silently breaks unattended schedules):

1. In Google Cloud Console, create a Service Account and download its JSON key
2. Save the key to `~/.config/gspread/service_account.json`
3. Share your Google Sheet with the service account's email (Editor access)

See the [gspread authentication guide](https://docs.gspread.org/en/latest/oauth2.html#service-account).

---

## Usage

```bash
# Download latest data and run analysis
python3 scripts/download_iot7.py

# Run analysis only (when CSVs already exist in data/raw/)
python3 scripts/energy_analysis.py
```

---

## Output

| File | Description |
|---|---|
| `data/raw/電能採集_*.csv` | Raw sensor data (gitignored) |
| `analysis/output/daily_summary.csv` | Daily stats: running hours, outage hours, first start, last stop |
| `analysis/output/baseline_report.md` | Baseline report with working window chart and anomaly list |

---

## Tuning

All thresholds are at the top of `energy_analysis.py`:

| Parameter | Default | Description |
|---|---|---|
| `CURRENT_THRESHOLD` | `0.1` A | Below this = not running |
| `GAP_THRESHOLD_MIN` | `20` min | Timestamp gap longer than this = power outage |
| `WORK_WINDOW_MIN_PCT` | `0.6` | Min frequency for a time slot to count as a working hour |
| `ANOMALY_STD_FACTOR` | `1.5` | Anomaly threshold = median − N×std |
| `MANUAL_WORK_WINDOW_RANGES` | `None` | Override working hours, e.g. `[("07:30", "11:30"), ("12:30", "17:30")]` |

---

## Project Structure

```
iot-energy-monitor/
├── config.example.json
├── .gitignore
├── scripts/
│   ├── download_iot7.py
│   └── energy_analysis.py
├── data/
│   └── raw/              # gitignored
└── analysis/
    └── output/           # gitignored
```

---

---

# IoT 電能監控與異常分析

> **給 AI 助理的說明：**
> 這是一個自包含的 Python 資料管線。串接七云物聯（IoT7）電能感應器 API，下載每 10 分鐘一筆的三相電流資料，進行機器狀態分類與異常偵測，輸出每日統計報告。所有敏感值（設備 ID、API 憑證、Google Sheet ID）存於本地 `config.json`，已被 gitignore 排除。主入口：`scripts/download_iot7.py`（下載＋分析）；只跑分析用 `scripts/energy_analysis.py`。

每 10 分鐘從七云物聯感應器抓取三相電能資料，自動偵測停電與非預期停機，產出每日統計與異常報告。

實際應用於工廠生產設備監控。

---

## 運作流程

```
IoT7 API
  → download_iot7.py     # 下載資料、刷新 token
  → data/raw/*.csv       # 原始電能記錄
  → energy_analysis.py   # 狀態分類、統計、異常偵測
  → analysis/output/     # 每日統計 CSV + Markdown 報告
  → Google Sheets        # 選配推送
```

**`download_iot7.py`**
- JWT token 自動刷新（主 token 4 天、refresh token 99 天）
- 根據已有 CSV 自動推算缺漏日期範圍，只下載差值
- 存檔後自動呼叫 `energy_analysis.py`

**`energy_analysis.py`**

每筆記錄分類為三種狀態：

| 狀態 | 定義 |
|---|---|
| `RUNNING` | 電流 > 閾值，機器運轉中 |
| `POWER_OUT` | 時間戳斷點 > 20 分鐘，感應器無電 = 停電 |
| `UNEXPECTED_STOP` | 有 log 但電流 = 0，且在工作時窗內 |

其他功能：
- 從歷史資料自動推導常態工作時窗（也可手動設定）
- 每日運轉時數、停電時數、非預期停機時數統計
- 中位數 ± 1.5σ 異常日判定
- 輸出 `daily_summary.csv` 與 `baseline_report.md`
- 選配推送至 Google Sheets

---

## 安裝

```bash
pip install pandas requests gspread google-auth
```

---

## 設定步驟

### 1. 專案設定檔

```bash
cp config.example.json config.json
```

編輯 `config.json`，填入你的設備資訊：

```json
{
  "device_id":             "YOUR_DEVICE_ID",
  "device_name":           "YOUR_DEVICE_NAME",
  "api_base":              "https://server.qiyunwulian.com:12341",
  "gsheet_enabled":        false,
  "gsheet_spreadsheet_id": "YOUR_GOOGLE_SHEET_ID",
  "gsheet_tab_name":       "Sheet1"
}
```

### 2. IoT7 Token

Token 存放於專案目錄之外的 `~/.config/iot7/config.json`：

```json
{
  "tokenString":  "YOUR_JWT_TOKEN",
  "tokenString2": "YOUR_REFRESH_TOKEN"
}
```

第一次取得、或之後 `tokenString` 過期時：在跑這支腳本的同一台 Mac 上打開「七云物聯」App，確認能正常連線，然後執行 `python3 scripts/refresh_token_from_app.py`。它直接讀 App 存在 macOS 系統裡的本機快取（`defaults read com.iot7.qiyunwulian`），不用登入、不用截封包。

`download_iot7.py` 本身也會自動做同樣的事：`tokenString` 過期時先試 refresh token 端點，失敗的話就自動 fallback 讀 App 本機快取。所以實務上你只需要維持「偶爾開 App 看數據」的習慣，腳本會自己抓到新 token；只有想手動確認或除錯時才需要自己跑 `refresh_token_from_app.py`。

只有 `tokenString2`（refresh token，約 99 天有效）過期時，才需要重新登入 App。

注意：這個方法只在你用**同一台 Mac** 開 App 時有效。如果你平常主要用手機看數據，本機快取讀不到手機 App 的 token，需要改用其他方式（例如截手機網路流量）取得。

### 3. Google Sheets（選配）

在 `config.json` 設定 `"gsheet_enabled": true`，並使用 **Service Account**（勿用 OAuth 使用者授權 — OAuth 應用在測試模式下 refresh token 只有 7 天壽命，會讓無人值守排程無聲失效）：

1. 在 Google Cloud Console 建立 Service Account 並下載 JSON 金鑰
2. 金鑰存到 `~/.config/gspread/service_account.json`
3. 把 Google Sheet 分享給 Service Account 的 email（編輯者權限）

參考 [gspread 官方認證說明](https://docs.gspread.org/en/latest/oauth2.html#service-account)。

---

## 執行

```bash
# 下載最新資料並執行分析
python3 scripts/download_iot7.py

# 只跑分析（data/raw/ 已有 CSV 時）
python3 scripts/energy_analysis.py
```

---

## 輸出

| 檔案 | 說明 |
|---|---|
| `data/raw/電能採集_*.csv` | 原始感應器資料（gitignored） |
| `analysis/output/daily_summary.csv` | 每日統計：運轉時數、停電時數、開收機時間 |
| `analysis/output/baseline_report.md` | 基準報告、工作時窗圖、異常日列表 |

---

## 參數調整

所有閾值在 `energy_analysis.py` 頂部：

| 參數 | 預設 | 說明 |
|---|---|---|
| `CURRENT_THRESHOLD` | `0.1` A | 低於此值 = 未運轉 |
| `GAP_THRESHOLD_MIN` | `20` min | 時間戳斷點超過此值 = 停電 |
| `WORK_WINDOW_MIN_PCT` | `0.6` | 該時段在 ≥60% 天有運轉才納入工作時窗 |
| `ANOMALY_STD_FACTOR` | `1.5` | 異常下限 = 中位數 - N×標準差 |
| `MANUAL_WORK_WINDOW_RANGES` | `None` | 手動指定工作時窗，例如 `[("07:30", "11:30"), ("12:30", "17:30")]` |

---

## 專案結構

```
iot-energy-monitor/
├── config.example.json       # 設定範本，複製為 config.json 後填入真實值
├── config.json               # 本地設定（gitignored，不會上傳）
├── .gitignore
├── scripts/
│   ├── download_iot7.py      # 資料下載 + token 管理
│   └── energy_analysis.py    # 分析 + 報告輸出
├── data/
│   └── raw/                  # 原始 CSV（gitignored）
└── analysis/
    └── output/               # 產出報告（gitignored）
```
