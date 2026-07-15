"""
從本機「七云物聯」App 的快取設定，手動更新 ~/.config/iot7/config.json 的 token。

原理：
  App 每次登入/連線成功後，會把 token 存在 macOS 的 App 偏好設定（UserDefaults）裡，
  鍵名為 flutter.token / flutter.token2。這支腳本直接讀那份設定，
  不需要手動找檔案、不需要重新輸入帳密。

  download_iot7.py 執行時，tokenString 過期也會自動嘗試同樣的讀取邏輯，
  所以平常不需要手動跑這支腳本；這裡留著是給你想手動確認、或除錯時用。

使用方式：
  1. 打開「七云物聯」App，確認能正常連線（畫面有資料，代表 App 內部 token 是新的）
  2. 執行：python3 scripts/refresh_token_from_app.py
  3. 看到「已更新」訊息即完成

注意：
  - 僅適用於 macOS，且電腦上有裝這個 App（讀的是 macOS defaults）
  - 若你平常是用手機開 App、很少在這台電腦上開，這支腳本會抓不到新 token，
    需改用其他方式（例如 mitmproxy 截手機 App 連網封包）取得
  - 若 App 很久沒開過，快取的 token 可能還是舊的、一樣過期，
    這種情況下要先打開 App 讓它自己重新登入一次，再執行本腳本
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

APP_DOMAIN = "com.iot7.qiyunwulian"
CONFIG_PATH = Path.home() / ".config" / "iot7" / "config.json"


def read_app_defaults() -> dict:
    result = subprocess.run(
        ["defaults", "read", APP_DOMAIN],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"[ERROR] 讀不到 App 設定（App domain: {APP_DOMAIN}）")
        print("請確認電腦上有安裝並至少打開過一次「七云物聯」App")
        sys.exit(1)
    return result.stdout


def extract_token(raw: str, key: str) -> str:
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith(f'"{key}"') or line.startswith(f"{key} "):
            value = line.split("=", 1)[1].strip().strip(";").strip('"')
            return value.replace("Bearer ", "").strip()
    return ""


def main():
    raw = read_app_defaults()
    token = extract_token(raw, "flutter.token")
    token2 = extract_token(raw, "flutter.token2")

    if not token or not token2:
        print("[ERROR] 找不到 flutter.token / flutter.token2，App 內部設定鍵名可能變了")
        print(f"請手動執行：defaults read {APP_DOMAIN} | grep -iaE 'token|eyJ'")
        sys.exit(1)

    old = {}
    if CONFIG_PATH.exists():
        backup = CONFIG_PATH.with_name(
            f"config.json.bak.{datetime.now().strftime('%Y%m%d%H%M%S')}")
        backup.write_text(CONFIG_PATH.read_text())
        print(f"舊設定已備份：{backup}")
        old = json.loads(CONFIG_PATH.read_text())

    old["tokenString"] = token
    old["tokenString2"] = token2
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(old, indent=2, ensure_ascii=False))
    print(f"已更新：{CONFIG_PATH}")


if __name__ == "__main__":
    main()
