"""
七云物聯（IoT7）自動下載腳本
設備類型：電能計量模組（三相）

功能：
  1. 讀取 token，過期自動用 refresh token 更新
  2. 計算缺少的日期範圍（上次下載到今天）
  3. 從 API 下載資料，轉成 CSV 格式
  4. 存入 data/raw/，自動觸發 energy_analysis.py

使用方式：
  1. 複製 config.example.json → config.json，填入設備資訊
  2. cd <project_root>
  3. python3 scripts/download_iot7.py

Token 管理：
  - tokenString：4 天有效。每次下載時，過期會先試 refresh token 刷新，
    失敗的話自動 fallback 讀本機「七云物聯」App 存在 macOS 系統裡的快取
    （只要 App 最近開過、能正常連線即可，不用重新登入）
  - tokenString2（refresh token）：99 天有效，過期需重新登入 App
  - 兩個 token 存在 ~/.config/iot7/config.json
"""

import json
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
import subprocess
import sys

# ── 路徑 ──────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent.parent
RAW_DIR     = BASE_DIR / "data" / "raw"
CONFIG_PATH = Path.home() / ".config" / "iot7" / "config.json"
PROJECT_CFG = BASE_DIR / "config.json"


def load_project_config() -> dict:
    if not PROJECT_CFG.exists():
        print(f"[ERROR] 找不到專案設定檔：{PROJECT_CFG}")
        print("請複製 config.example.json → config.json 並填入設定值")
        sys.exit(1)
    return json.loads(PROJECT_CFG.read_text(encoding="utf-8"))


# ── 從 config.json 讀取設定 ───────────────────────────────────────────────────
_cfg        = load_project_config()
DEVICE_ID   = _cfg["device_id"]
DEVICE_NAME = _cfg["device_name"]
API_BASE    = _cfg["api_base"]

# ── API 欄位 → CSV 欄位對應 + 單位換算 ─────────────────────────────────────────
# API 回傳的數值是整數（小單位），需除以換算係數才是 CSV 中的單位
COLUMN_MAP = [
    # (api_column, csv_column, scale_factor)
    ("acav",    "電壓A(V)",          10.0),
    ("acbv",    "電壓B(V)",          10.0),
    ("accv",    "電壓C(V)",          10.0),
    ("acac",    "電流A(A)",         1000.0),
    ("acbc",    "電流B(A)",         1000.0),
    ("accc",    "電流C(A)",         1000.0),
    ("acayggl", "功率A(W)",          10.0),
    ("acbyggl", "功率B(W)",          10.0),
    ("accyggl", "功率C(W)",          10.0),
    ("aczyggl", "總功率(W)",          10.0),
    ("acawggl", "無功功率A(Var)",     10.0),
    ("acbwggl", "無功功率B(Var)",     10.0),
    ("accwggl", "無功功率C(Var)",     10.0),
    ("aczwggl", "總無功功率(Var)",    10.0),
    ("acpl",    "頻率(Hz)",          100.0),
    ("acae",    "功率因數A",         1000.0),
    ("acbe",    "功率因數B",         1000.0),
    ("acce",    "功率因數C",         1000.0),
    ("acaygdl", "電能A(kWh)",       1000.0),
    ("acbygdl", "電能B(kWh)",       1000.0),
    ("accygdl", "電能C(kWh)",       1000.0),
    ("acygzdl", "總電能(kWh)",      1000.0),
    ("acawgdl", "無功電能A(kVarh)", 1000.0),
    ("acbwgdl", "無功電能B(kVarh)", 1000.0),
    ("accwgdl", "無功電能C(kVarh)", 1000.0),
    ("acwgzdl", "總無功電能(kVarh)",1000.0),
    ("sign",    "是否標記",            1.0),
]


# ─────────────────────────────────────────────────────────────────────────────
# Token 管理
# ─────────────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(f"[ERROR] 找不到設定檔：{CONFIG_PATH}")
        print("請先手動執行一次登入並儲存 token（見 README）")
        sys.exit(1)
    return json.loads(CONFIG_PATH.read_text())


def save_config(config: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False))


def token_expires_in(token: str) -> int:
    """回傳 token 距離過期的秒數（負數 = 已過期）"""
    import base64
    payload = token.split(".")[1]
    padding = 4 - len(payload) % 4
    decoded = json.loads(base64.b64decode(payload + "=" * padding))
    return decoded["exp"] - int(datetime.now().timestamp())


def read_token_from_app() -> dict:
    """從本機「七云物聯」App 的系統快取讀 token（不用登入，只要 App 最近開過且能連線）

    僅適用於 macOS，且電腦上有裝該 App（讀的是 macOS 的 defaults/UserDefaults）。
    若你平常是用手機開 App，這個 fallback 不會有作用，需改用其他方式取得 token
    （例如用 mitmproxy 截手機 App 連網時的封包）。
    """
    result = subprocess.run(
        ["defaults", "read", "com.iot7.qiyunwulian"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return {}

    tokens = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith('"flutter.token"'):
            tokens["tokenString"] = line.split("=", 1)[1].strip().strip(";").strip('"').replace("Bearer ", "").strip()
        elif line.startswith('"flutter.token2"'):
            tokens["tokenString2"] = line.split("=", 1)[1].strip().strip(";").strip('"').replace("Bearer ", "").strip()
    return tokens


def get_valid_token(config: dict) -> str:
    """取得有效的 tokenString：先看現有的、過期就試 refresh token、
    再不行就 fallback 讀本機 App 快取（見 read_token_from_app）"""
    token   = config.get("tokenString", "")
    refresh = config.get("tokenString2", "")

    refresh_remaining_days = token_expires_in(refresh) / 86400
    if refresh_remaining_days < 14:
        print(f"  [警告] tokenString2 剩餘 {refresh_remaining_days:.0f} 天即將過期！")
        print(f"  請盡快重新登入 App 並更新 {CONFIG_PATH}")

    if token_expires_in(token) > 300:
        return token

    print("  tokenString 即將過期，嘗試用 refresh token 刷新...")
    if token_expires_in(refresh) > 0:
        resp = requests.post(
            f"{API_BASE}/user/refreshtoken",
            headers={"Content-Type": "application/json"},
            json={"refreshToken": refresh},
            timeout=15
        )
        data = resp.json()
        if data.get("code") == 0:
            new_token = data["data"]["tokenString"]
            config["tokenString"] = new_token
            save_config(config)
            print("  tokenString 已更新（refresh token）")
            return new_token
        print(f"  [ERROR] refresh token 刷新失敗：{data}")
    else:
        print("  [ERROR] tokenString2 也過期了")

    print("  改嘗試讀本機「七云物聯」App 快取的 token...")
    app_tokens = read_token_from_app()
    app_token = app_tokens.get("tokenString", "")
    if app_token and token_expires_in(app_token) > 300:
        config.update(app_tokens)
        save_config(config)
        print("  tokenString 已更新（App 本機快取）")
        return app_token

    print("[ERROR] App 快取裡也沒有有效 token，請打開「七云物聯」App 連線一次後再重跑")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# 計算需要下載的日期範圍
# ─────────────────────────────────────────────────────────────────────────────
def get_date_range() -> tuple[datetime, datetime]:
    """根據 data/raw/ 已有的檔案，決定要下載哪段日期"""
    csv_files = sorted(RAW_DIR.glob("電能採集*.csv"))
    if csv_files:
        last_df  = pd.read_csv(csv_files[-1], encoding="utf-8-sig")
        last_dt  = pd.to_datetime(last_df["時間"].iloc[-1])
        start_dt = last_dt + timedelta(minutes=10)
    else:
        start_dt = datetime.now() - timedelta(days=30)

    end_dt = datetime.now()

    if start_dt >= end_dt:
        print("  資料已是最新，無需下載")
        return None, None

    # API 超過 3 天會自動降為 1 小時級距，硬性截斷到 2.9 天
    MAX_SPAN = timedelta(hours=69, minutes=36)
    if end_dt - start_dt > MAX_SPAN:
        end_dt = start_dt + MAX_SPAN
        print("  [注意] 範圍超過 3 天，截斷至 2.9 天（下次執行會繼續補）")

    print(f"  下載範圍：{start_dt.strftime('%Y-%m-%d %H:%M')} ～ {end_dt.strftime('%Y-%m-%d %H:%M')}")
    return start_dt, end_dt


# ─────────────────────────────────────────────────────────────────────────────
# 下載 & 轉換
# ─────────────────────────────────────────────────────────────────────────────
def download_data(token: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    params = {
        "DeviceId":   DEVICE_ID,
        "start_time": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "end_time":   end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "accuracy":   "10m",
        "only_sign":  "false",
    }
    resp = requests.get(
        f"{API_BASE}/v2/history/period",
        headers={
            "auth":            f"Bearer {token}",
            "user-agent":      "Dart/3.6 (dart:io)",
            "accept-encoding": "gzip",
        },
        params=params,
        timeout=30
    )
    data = resp.json()
    if data.get("code") != 0 or not data.get("data"):
        print(f"[ERROR] API 回傳異常：{data}")
        sys.exit(1)

    series  = data["data"][0]["Series"][0]
    raw_df  = pd.DataFrame(series["values"], columns=series["columns"])

    if raw_df.empty:
        print("  API 回傳空資料（此段時間無紀錄）")
        return pd.DataFrame()

    out = pd.DataFrame()
    out["時間"] = pd.to_datetime(raw_df["time"])
    for api_col, csv_col, scale in COLUMN_MAP:
        if api_col in raw_df.columns:
            if api_col == "sign":
                out[csv_col] = raw_df[api_col].fillna("否")
            else:
                out[csv_col] = (pd.to_numeric(raw_df[api_col], errors="coerce") / scale).round(3)

    out["設備ID"]   = DEVICE_ID
    out["設備名稱"] = DEVICE_NAME
    out["備註"]     = ""

    print(f"  下載完成：{len(out)} 筆記錄")
    return out


def save_csv(df: pd.DataFrame, start_dt: datetime, end_dt: datetime):
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"電能採集 {start_dt.strftime('%m%d')}_{end_dt.strftime('%m%d')}自動.csv"
    out_path = RAW_DIR / filename
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"  → 已存：{out_path}")
    return out_path


# ─────────────────────────────────────────────────────────────────────────────
# 主程序
# ─────────────────────────────────────────────────────────────────────────────
def main():
    print("── 載入設定 ──")
    config = load_config()
    token  = get_valid_token(config)

    print("\n── 計算下載範圍 ──")
    start_dt, end_dt = get_date_range()
    if start_dt is None:
        print("無新資料，結束。")
        return

    print("\n── 下載資料 ──")
    df = download_data(token, start_dt, end_dt)
    if df.empty:
        print("無資料，結束。")
        return

    print("\n── 儲存 CSV ──")
    save_csv(df, start_dt, end_dt)

    print("\n── 執行分析 ──")
    result = subprocess.run(
        [sys.executable, str(BASE_DIR / "scripts" / "energy_analysis.py")],
        capture_output=True, text=True
    )
    print(result.stdout)
    if result.returncode != 0:
        print(f"[WARNING] 分析腳本回傳錯誤：{result.stderr}")


if __name__ == "__main__":
    main()
