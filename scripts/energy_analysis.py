"""
IoT 電能分析框架
資料來源：七云物聯（IoT7），每 10 分鐘一筆

三種狀態定義：
  RUNNING          — 電流 > 0，機器運轉中
  POWER_OUT        — 時間戳斷點（兩筆間隔 > 20 分鐘），感應器無電 = 停電
  UNEXPECTED_STOP  — 有電（有 log）但電流 = 0，且落在常態工作時窗內

輸出：
  analysis/output/daily_summary.csv   每日統計
  analysis/output/baseline_report.md  基準報告 + 異常日列表

使用方式：
  1. 複製 config.example.json → config.json，填入設定值
  2. 把原始 CSV 放入 data/raw/
  3. cd <project_root>
  4. python scripts/energy_analysis.py

新增 raw data 後重新執行即可，輸出自動更新。
"""

import json
import pandas as pd
from pathlib import Path
import sys

# ── 路徑 ──────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent.parent
RAW_DIR    = BASE_DIR / "data" / "raw"
OUT_DIR    = BASE_DIR / "analysis" / "output"
OUT_DIR.mkdir(parents=True, exist_ok=True)
PROJECT_CFG = BASE_DIR / "config.json"


def load_project_config() -> dict:
    if not PROJECT_CFG.exists():
        print(f"[ERROR] 找不到專案設定檔：{PROJECT_CFG}")
        print("請複製 config.example.json → config.json 並填入設定值")
        sys.exit(1)
    return json.loads(PROJECT_CFG.read_text(encoding="utf-8"))


_cfg = load_project_config()

# ── Google Sheets 推送設定（從 config.json 讀取）─────────────────────────────
GSHEET_ENABLED        = _cfg.get("gsheet_enabled", False)
GSHEET_SPREADSHEET_ID = _cfg.get("gsheet_spreadsheet_id", "")
GSHEET_TAB_NAME       = _cfg.get("gsheet_tab_name", "電能採集")
GSHEET_CREDENTIALS    = Path.home() / ".config" / "gspread" / "service_account.json"

# ── 參數（可調整） ────────────────────────────────────────────────────────────
CURRENT_THRESHOLD   = 0.1   # 電流 > 此值 = 運轉中 (A)
GAP_THRESHOLD_MIN   = 20    # 時間戳間隔 > 此值 = 停電 (分鐘)，20 分鐘 = 容錯一個週期
WORK_WINDOW_MIN_PCT = 0.6   # 某小時在 ≥60% 天有運轉 → 納入工作時窗
ANOMALY_STD_FACTOR  = 1.5   # 低於「中位數 - N*標準差」= 異常日

# ── 手動覆蓋工作時窗（None = 自動推導）──────────────────────────────────────
# 格式：[(開始, 結束), ...]，每段為左閉右開區間，精度 10 分鐘
# 例如：上班 07:30-17:30，午休 11:30-12:30 → [("07:30", "11:30"), ("12:30", "17:30")]
MANUAL_WORK_WINDOW_RANGES = None

# ── 手動覆蓋閾值（None = 使用自動推導；填入後優先使用）────────────────────────
MANUAL_FIRST_START_LATE     = None   # 開機晚於此時間 = 異常，格式 "HH:MM"
MANUAL_LAST_STOP_EARLY      = None   # 收工早於此時間 = 異常
MANUAL_RUNNING_LOW          = None   # 每日運轉低於此時數（小時）= 異常
MANUAL_UNEXPECTED_STOP_HIGH = None   # 非預期停機超過此時數（小時）= 異常


# ─────────────────────────────────────────────────────────────────────────────
# 1. 載入資料
# ─────────────────────────────────────────────────────────────────────────────
def load_raw_data() -> pd.DataFrame:
    files = sorted(RAW_DIR.glob("電能採集*.csv"))
    if not files:
        raise FileNotFoundError(f"找不到 CSV 檔案：{RAW_DIR}")

    df = pd.concat(
        [pd.read_csv(f, encoding="utf-8-sig") for f in files],
        ignore_index=True,
    )
    df["時間"] = pd.to_datetime(df["時間"])
    df = df.drop_duplicates("時間").sort_values("時間").reset_index(drop=True)
    print(f"  載入 {len(files)} 個檔案，共 {len(df)} 筆記錄")
    print(f"  時間範圍：{df['時間'].min()} ～ {df['時間'].max()}")
    return df


# ─────────────────────────────────────────────────────────────────────────────
# 2. 偵測停電（時間戳斷點）
# ─────────────────────────────────────────────────────────────────────────────
def detect_power_outages(df: pd.DataFrame) -> pd.DataFrame:
    """
    相鄰兩筆時間戳若間隔 > GAP_THRESHOLD_MIN，
    則在這段期間補入一筆 POWER_OUT 記錄。
    """
    df["gap_min"] = df["時間"].diff().dt.total_seconds().div(60)
    gaps = df[df["gap_min"] > GAP_THRESHOLD_MIN].copy()

    if gaps.empty:
        print("  無停電事件（無時間戳斷點）")
        return pd.DataFrame(columns=["outage_start", "outage_end", "outage_hours", "date"])

    records = []
    for _, row in gaps.iterrows():
        start = row["時間"] - pd.Timedelta(minutes=row["gap_min"])
        end   = row["時間"]
        records.append({
            "outage_start": start,
            "outage_end":   end,
            "outage_hours": round(row["gap_min"] / 60, 2),
            "date":         start.date(),
        })

    outage_df = pd.DataFrame(records)
    print(f"  找到 {len(outage_df)} 段停電，共 {outage_df['outage_hours'].sum():.1f} 小時")
    return outage_df


# ─────────────────────────────────────────────────────────────────────────────
# 3. 標記每筆記錄狀態
# ─────────────────────────────────────────────────────────────────────────────
def classify_records(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["running"]   = df["電流A(A)"] > CURRENT_THRESHOLD
    df["date"]      = df["時間"].dt.date
    df["time_slot"] = df["時間"].dt.strftime("%H:%M")
    return df


# ─────────────────────────────────────────────────────────────────────────────
# 4. 推導常態工作時窗
# ─────────────────────────────────────────────────────────────────────────────
def derive_work_window(df: pd.DataFrame) -> dict:
    total_days = df["date"].nunique()
    slot_any   = df.groupby(["date", "time_slot"])["running"].any()
    slot_pct   = (slot_any.groupby("time_slot").sum() / total_days).round(2)

    if MANUAL_WORK_WINDOW_RANGES is not None:
        work_slots = []
        for (s, e) in MANUAL_WORK_WINDOW_RANGES:
            h, m = map(int, s.split(":"))
            eh, em = map(int, e.split(":"))
            cur = h * 60 + m
            end = eh * 60 + em
            while cur < end:
                work_slots.append(f"{cur // 60:02d}:{cur % 60:02d}")
                cur += 10
        source = "手動設定"
    else:
        work_slots = sorted(slot_pct[slot_pct >= WORK_WINDOW_MIN_PCT].index.tolist())
        source = "自動推導"

    wh_str = _slots_to_ranges(work_slots) if work_slots else "無法判斷"
    print(f"  工作時窗（{source}）：{wh_str}")

    return {
        "work_slots":   work_slots,
        "slot_run_pct": slot_pct.to_dict(),
        "total_days":   total_days,
    }


def _slots_to_ranges(slots: list) -> str:
    if not slots:
        return ""
    def to_min(s):
        h, m = map(int, s.split(":"))
        return h * 60 + m
    def to_str(m):
        return f"{m // 60:02d}:{m % 60:02d}"

    mins = sorted(to_min(s) for s in slots)
    segments, start, prev = [], mins[0], mins[0]
    for m in mins[1:]:
        if m != prev + 10:
            segments.append(f"{to_str(start)}～{to_str(prev + 10)}")
            start = m
        prev = m
    segments.append(f"{to_str(start)}～{to_str(prev + 10)}")
    return "、".join(segments)


# ─────────────────────────────────────────────────────────────────────────────
# 5. 每日統計
# ─────────────────────────────────────────────────────────────────────────────
def _work_overlap_min(seg_start, seg_end, day, work_slots: list) -> float:
    mins = 0.0
    for slot_str in work_slots:
        slot_s = pd.Timestamp(f"{day} {slot_str}")
        slot_e = slot_s + pd.Timedelta(minutes=10)
        ov_s = max(seg_start, slot_s)
        ov_e = min(seg_end, slot_e)
        if ov_e > ov_s:
            mins += (ov_e - ov_s).total_seconds() / 60
    return mins


def calc_daily_summary(
    df: pd.DataFrame,
    outage_df: pd.DataFrame,
    work_hours: list,
) -> pd.DataFrame:

    df = df.copy()
    df["in_work_window"] = df["time_slot"].isin(work_hours)
    INTERVAL_H = 10 / 60

    def agg(g):
        running_times = g[g["running"]]["時間"]
        return pd.Series({
            "running_hours":         round(g["running"].sum() * INTERVAL_H, 2),
            "unexpected_stop_hours": round(((~g["running"]) & g["in_work_window"]).sum() * INTERVAL_H, 2),
            "first_start":           running_times.min() if not running_times.empty else pd.NaT,
            "last_stop":             running_times.max() if not running_times.empty else pd.NaT,
            "data_records":          len(g),
        })

    daily = df.groupby("date").apply(agg).reset_index()

    if not outage_df.empty:
        split_rows = []
        split_work_rows = []
        for _, row in outage_df.iterrows():
            start, end = row["outage_start"], row["outage_end"]
            cur = start
            while cur.date() < end.date():
                midnight = pd.Timestamp(cur.date()) + pd.Timedelta(days=1)
                split_rows.append({"date": cur.date(), "outage_hours": (midnight - cur).total_seconds() / 3600})
                wmin = _work_overlap_min(cur, midnight, cur.date(), work_hours)
                if wmin > 0:
                    split_work_rows.append({"date": cur.date(), "work_outage_hours": wmin / 60})
                cur = midnight
            if (end - cur).total_seconds() > 0:
                split_rows.append({"date": cur.date(), "outage_hours": (end - cur).total_seconds() / 3600})
                wmin = _work_overlap_min(cur, end, cur.date(), work_hours)
                if wmin > 0:
                    split_work_rows.append({"date": cur.date(), "work_outage_hours": wmin / 60})

        pow_daily = (
            pd.DataFrame(split_rows)
            .groupby("date")["outage_hours"]
            .sum().round(2)
            .reset_index()
            .rename(columns={"outage_hours": "power_out_hours"})
        )

        if split_work_rows:
            pow_work_daily = (
                pd.DataFrame(split_work_rows)
                .groupby("date")["work_outage_hours"]
                .sum().round(2)
                .reset_index()
                .rename(columns={"work_outage_hours": "power_out_work_hours"})
            )
            pow_daily = pow_daily.merge(pow_work_daily, on="date", how="left")
        else:
            pow_daily["power_out_work_hours"] = 0.0

        blacked_out = set(pow_daily["date"]) - set(daily["date"])
        if blacked_out:
            empty = pd.DataFrame([{
                "date": d, "running_hours": 0.0,
                "unexpected_stop_hours": 0.0,
                "first_start": pd.NaT, "last_stop": pd.NaT,
                "data_records": 0,
            } for d in blacked_out])
            daily = pd.concat([daily, empty], ignore_index=True).sort_values("date").reset_index(drop=True)
            print(f"  補入全天停電日：{sorted(blacked_out)}")

        daily = daily.merge(pow_daily, on="date", how="left")
    else:
        daily["power_out_hours"] = 0.0

    daily["power_out_hours"]      = daily["power_out_hours"].fillna(0.0)
    daily["power_out_work_hours"] = daily.get("power_out_work_hours", pd.Series(0.0, index=daily.index)).fillna(0.0)

    cols = [
        "date", "running_hours", "power_out_hours", "power_out_work_hours",
        "unexpected_stop_hours", "first_start", "last_stop", "data_records",
    ]
    return daily[cols]


# ─────────────────────────────────────────────────────────────────────────────
# 6. 驗證資料一致性
# ─────────────────────────────────────────────────────────────────────────────
def validate_totals(outage_df: pd.DataFrame, daily: pd.DataFrame):
    if outage_df.empty:
        print("  無停電事件，跳過驗證")
        return

    total_from_events = outage_df["outage_hours"].sum()
    total_from_daily  = daily["power_out_hours"].sum()
    diff = abs(total_from_events - total_from_daily)

    if diff < 0.1:
        print(f"  [OK] 停電時數一致：{total_from_daily:.2f}h")
    else:
        print(f"  [WARNING] 停電時數不一致！事件加總 {total_from_events:.2f}h vs 每日加總 {total_from_daily:.2f}h（差 {diff:.2f}h）")


# ─────────────────────────────────────────────────────────────────────────────
# 7. 敘述統計 & 異常閾值
# ─────────────────────────────────────────────────────────────────────────────
def calc_descriptive_stats(daily: pd.DataFrame) -> dict:
    def to_min(t):
        if pd.isnull(t): return None
        t = pd.to_datetime(t)
        return t.hour * 60 + t.minute

    def fmt(m):
        if m is None or pd.isnull(m): return "--:--"
        return f"{int(m)//60:02d}:{int(m)%60:02d}"

    def parse_manual(s):
        if s is None: return None
        h, m = map(int, s.split(":"))
        return h * 60 + m

    daily = daily.copy()
    daily["fs_min"] = daily["first_start"].apply(to_min)
    daily["ls_min"] = daily["last_stop"].apply(to_min)

    rh = daily["running_hours"]
    po = daily["power_out_hours"]
    us = daily["unexpected_stop_hours"]

    normal_days = daily[daily["running_hours"] >= rh.quantile(0.25)]
    fs = normal_days["fs_min"].dropna()
    ls = normal_days["ls_min"].dropna()

    auto_running_low          = round(rh.quantile(0.15), 2)
    auto_first_start_late_min = fs.mean() + 2 * fs.std()
    auto_last_stop_early_min  = ls.mean() - 2 * ls.std()
    auto_unexpected_high      = round(us.mean() + 2 * us.std(), 2)

    final_running_low     = MANUAL_RUNNING_LOW          if MANUAL_RUNNING_LOW          is not None else auto_running_low
    final_fs_late_min     = parse_manual(MANUAL_FIRST_START_LATE)  if MANUAL_FIRST_START_LATE  is not None else auto_first_start_late_min
    final_ls_early_min    = parse_manual(MANUAL_LAST_STOP_EARLY)   if MANUAL_LAST_STOP_EARLY   is not None else auto_last_stop_early_min
    final_unexpected_high = MANUAL_UNEXPECTED_STOP_HIGH if MANUAL_UNEXPECTED_STOP_HIGH is not None else auto_unexpected_high

    return {
        "running_hours": {
            "mean": round(rh.mean(), 2), "median": round(rh.median(), 2),
            "std":  round(rh.std(), 2),  "min":    round(rh.min(), 2),
            "max":  round(rh.max(), 2),
            "p10":  round(rh.quantile(0.10), 2), "p25": round(rh.quantile(0.25), 2),
            "p75":  round(rh.quantile(0.75), 2), "p90": round(rh.quantile(0.90), 2),
        },
        "first_start": {
            "mean": fmt(fs.mean()), "std_min": round(fs.std(), 1),
            "earliest": fmt(fs.min()), "latest": fmt(fs.max()),
        },
        "last_stop": {
            "mean": fmt(ls.mean()), "std_min": round(ls.std(), 1),
            "earliest": fmt(ls.min()), "latest": fmt(ls.max()),
        },
        "power_outage": {
            "days_with_outage":   int((po > 0).sum()),
            "pct_days":           round((po > 0).mean() * 100, 1),
            "median_when_occurs": round(po[po > 0].median(), 2) if (po > 0).any() else 0,
        },
        "unexpected_stop": {
            "mean": round(us.mean(), 2),
            "days_with_stop": int((us > 0).sum()),
        },
        "thresholds": {
            "auto_running_low":          auto_running_low,
            "auto_first_start_late":     fmt(auto_first_start_late_min),
            "auto_last_stop_early":      fmt(auto_last_stop_early_min),
            "auto_unexpected_stop_high": auto_unexpected_high,
            "running_low":               final_running_low,
            "first_start_late":          fmt(final_fs_late_min),
            "last_stop_early":           fmt(final_ls_early_min),
            "unexpected_stop_high":      final_unexpected_high,
            "manual_flags": {
                "running_low":          MANUAL_RUNNING_LOW          is not None,
                "first_start_late":     MANUAL_FIRST_START_LATE     is not None,
                "last_stop_early":      MANUAL_LAST_STOP_EARLY      is not None,
                "unexpected_stop_high": MANUAL_UNEXPECTED_STOP_HIGH is not None,
            },
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# 8. 建立基準 & 標記異常
# ─────────────────────────────────────────────────────────────────────────────
def calc_baseline_and_anomaly(daily: pd.DataFrame) -> tuple:
    median_h = daily["running_hours"].median()
    std_h    = daily["running_hours"].std()
    lower    = max(0, median_h - ANOMALY_STD_FACTOR * std_h)

    daily = daily.copy()
    daily["anomaly_hours"] = (median_h - daily["running_hours"]).clip(lower=0).round(2)
    daily["is_anomaly"]    = daily["running_hours"] < lower

    baseline = {
        "median_running_hours":  round(median_h, 2),
        "std_running_hours":     round(std_h, 2),
        "anomaly_lower_bound":   round(lower, 2),
        "total_power_out_hours": round(daily["power_out_hours"].sum(), 2),
        "anomaly_days":          int(daily["is_anomaly"].sum()),
        "total_days":            len(daily),
    }
    return daily, baseline


# ─────────────────────────────────────────────────────────────────────────────
# 9. 輸出
# ─────────────────────────────────────────────────────────────────────────────
def write_outputs(daily: pd.DataFrame, baseline: dict, work_window: dict, stats: dict):
    csv_path = OUT_DIR / "daily_summary.csv"
    daily.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"  → {csv_path}")

    work_slots = work_window["work_slots"]
    wh_str = _slots_to_ranges(work_slots) if work_slots else "無法判斷"

    slot_pct = work_window["slot_run_pct"]
    hour_bar = ""
    for h in range(24):
        slots_in_hour = [f"{h:02d}:{m:02d}" for m in range(0, 60, 10)]
        pct = sum(slot_pct.get(s, 0) for s in slots_in_hour) / len(slots_in_hour)
        in_ww = any(s in work_slots for s in slots_in_hour)
        bar  = "█" * int(pct * 20)
        mark = " <- 工作時窗" if in_ww else ""
        hour_bar += f"  {h:02d}:xx  {bar:<20s}  {pct*100:4.0f}%{mark}\n"

    anomaly_rows = daily[daily["is_anomaly"]].copy()
    if anomaly_rows.empty:
        anomaly_table = "| -- | 本期無異常日 | -- | -- | -- |\n"
    else:
        anomaly_table = ""
        for _, r in anomaly_rows.iterrows():
            anomaly_table += (
                f"| {r['date']} "
                f"| {r['running_hours']}h "
                f"| {r['power_out_hours']}h "
                f"| {r['unexpected_stop_hours']}h "
                f"| {r['anomaly_hours']}h |\n"
            )

    md = f"""# 電能基準報告

> 自動產出，每次執行 energy_analysis.py 即更新

## 資料概況
- 分析區間：{daily['date'].min()} ～ {daily['date'].max()}（共 {baseline['total_days']} 天）
- 狀態定義：
  - **RUNNING**：電流 > {CURRENT_THRESHOLD} A
  - **POWER_OUT**：時間戳斷點 > {GAP_THRESHOLD_MIN} 分鐘（感應器無電）
  - **UNEXPECTED_STOP**：有 log 但電流 = 0，且在工作時窗內

---

## 常態基準

| 指標 | 數值 |
|------|------|
| 常態每日運轉時數（中位數） | {baseline['median_running_hours']} 小時 |
| 標準差 | {baseline['std_running_hours']} 小時 |
| 異常判定下限（中位數 - {ANOMALY_STD_FACTOR}σ） | < {baseline['anomaly_lower_bound']} 小時 |
| 期間總停電時數 | {baseline['total_power_out_hours']} 小時 |
| 異常日 | {baseline['anomaly_days']} 天 / {baseline['total_days']} 天 |

---

## 常態工作時窗：{wh_str}

```
{hour_bar}```

---

## 敘述統計

### 每日運轉時數
| 指標 | 數值 |
|------|------|
| 平均 | {stats['running_hours']['mean']} 小時 |
| 中位數 | {stats['running_hours']['median']} 小時 |
| 標準差 | {stats['running_hours']['std']} 小時 |
| 最小 / 最大 | {stats['running_hours']['min']} / {stats['running_hours']['max']} 小時 |
| P10 / P25 | {stats['running_hours']['p10']} / {stats['running_hours']['p25']} 小時 |
| P75 / P90 | {stats['running_hours']['p75']} / {stats['running_hours']['p90']} 小時 |

### 開機時間
| 指標 | 數值 |
|------|------|
| 平均開機時間 | {stats['first_start']['mean']} |
| 標準差 | ± {stats['first_start']['std_min']} 分鐘 |
| 最早 / 最晚 | {stats['first_start']['earliest']} / {stats['first_start']['latest']} |

### 收工時間
| 指標 | 數值 |
|------|------|
| 平均收工時間 | {stats['last_stop']['mean']} |
| 標準差 | ± {stats['last_stop']['std_min']} 分鐘 |
| 最早 / 最晚 | {stats['last_stop']['earliest']} / {stats['last_stop']['latest']} |

### 停電狀況
| 指標 | 數值 |
|------|------|
| 有停電的天數 | {stats['power_outage']['days_with_outage']} 天（{stats['power_outage']['pct_days']}%） |
| 發生時中位數時長 | {stats['power_outage']['median_when_occurs']} 小時 |

### 非預期停機
| 指標 | 數值 |
|------|------|
| 每日平均 | {stats['unexpected_stop']['mean']} 小時 |
| 有發生的天數 | {stats['unexpected_stop']['days_with_stop']} 天 |

---

## 異常檢測閾值

| 指標 | 異常觸發條件 | 來源 |
|------|------------|------|
| 每日運轉時數 | **< {stats['thresholds']['running_low']}h** | {'手動設定' if stats['thresholds']['manual_flags']['running_low'] else '自動推導'} |
| 開機時間 | **晚於 {stats['thresholds']['first_start_late']}** | {'手動設定' if stats['thresholds']['manual_flags']['first_start_late'] else '自動推導'} |
| 收工時間 | **早於 {stats['thresholds']['last_stop_early']}** | {'手動設定' if stats['thresholds']['manual_flags']['last_stop_early'] else '自動推導'} |
| 非預期停機 | **> {stats['thresholds']['unexpected_stop_high']}h** | {'手動設定' if stats['thresholds']['manual_flags']['unexpected_stop_high'] else '自動推導'} |

---

## 異常日列表（運轉時數 < {baseline['anomaly_lower_bound']} 小時）

| 日期 | 運轉時數 | 停電時數 | 非預期停機 | 缺少時數 |
|------|---------|---------|-----------|---------|
{anomaly_table}
---
*由 scripts/energy_analysis.py 自動產出*
"""
    md_path = OUT_DIR / "baseline_report.md"
    md_path.write_text(md, encoding="utf-8")
    print(f"  → {md_path}")


# ─────────────────────────────────────────────────────────────────────────────
# 推送到 Google Sheets
# ─────────────────────────────────────────────────────────────────────────────
def push_to_gsheet(daily: pd.DataFrame):
    if not GSHEET_ENABLED:
        return

    try:
        import gspread
    except ImportError:
        print("  [SKIP] 請先安裝：pip3 install gspread google-auth")
        return

    if not GSHEET_CREDENTIALS.exists():
        print(f"  [SKIP] 找不到憑證檔：{GSHEET_CREDENTIALS}")
        print("  請依照 README 說明完成 Google Service Account 設定")
        return

    try:
        gc = gspread.service_account(filename=str(GSHEET_CREDENTIALS))
        sh = gc.open_by_key(GSHEET_SPREADSHEET_ID)
        ws = sh.worksheet(GSHEET_TAB_NAME)

        df = daily.copy()
        df["date"]        = df["date"].astype(str)
        df["first_start"] = df["first_start"].astype(str).replace("NaT", "")
        df["last_stop"]   = df["last_stop"].astype(str).replace("NaT", "")
        df["is_anomaly"]  = df["is_anomaly"].astype(str)
        df = df.fillna("")

        rows = [df.columns.tolist()] + df.values.tolist()
        ws.clear()
        ws.update(rows, value_input_option="RAW")
        print(f"  → Google Sheets「{GSHEET_TAB_NAME}」已更新（{len(df)} 列）")

    except Exception as e:
        print(f"  [WARNING] Google Sheets 推送失敗：{e}")
        print("  本地檔案仍已正常輸出，可手動上傳")


# ─────────────────────────────────────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────────────────────────────────────
def main():
    print("\n── 載入資料 ──")
    df = load_raw_data()

    print("\n── 偵測停電 ──")
    outage_df = detect_power_outages(df)

    print("\n── 標記狀態 ──")
    df = classify_records(df)

    print("\n── 推導工作時窗 ──")
    work_window = derive_work_window(df)

    print("\n── 計算每日統計 ──")
    daily = calc_daily_summary(df, outage_df, work_window["work_slots"])

    print("\n── 建立基準 & 標記異常 ──")
    daily, baseline = calc_baseline_and_anomaly(daily)

    print("\n── 計算敘述統計 ──")
    stats = calc_descriptive_stats(daily)

    print("\n── 驗證資料一致性 ──")
    validate_totals(outage_df, daily)

    print("\n── 輸出報告 ──")
    write_outputs(daily, baseline, work_window, stats)

    print("\n── 推送 Google Sheets ──")
    push_to_gsheet(daily)

    print(f"""
╔══════════════════════════════╗
  常態運轉：{baseline['median_running_hours']} 小時／天
  期間停電：{baseline['total_power_out_hours']} 小時
  異常日數：{baseline['anomaly_days']} 天
  輸出目錄：analysis/output/
╚══════════════════════════════╝
""")


if __name__ == "__main__":
    main()
