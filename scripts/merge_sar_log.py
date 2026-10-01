#!/usr/bin/env python3
"""
sar_log.jsonl 差し替え用ファイルを作るスクリプト(手元PCで実行)

  入力 : 同じフォルダの sar_log_rebuilt.jsonl(再構築済み、〜2026-10-01 06:00)
         GitHub上の最新 sar_log.jsonl(自動ダウンロード)
  出力 : sar_log.jsonl                 … 差し替え用(再構築分 + 修正後の確定足の行)
         sar_log_v1_unconfirmed.jsonl  … 旧ログの保管用(GitHubの現物をそのまま保存)

使い方:  python merge_sar_log.py
"""
import json, sys
from datetime import datetime, timezone
import requests

URL = "https://raw.githubusercontent.com/bluefujii0016/btc-oi-recorder/refs/heads/main/data/sar_log.jsonl"

raw = requests.get(URL, timeout=60)
raw.raise_for_status()
with open("sar_log_v1_unconfirmed.jsonl", "w", encoding="utf-8", newline="\n") as f:
    f.write(raw.text)

live = [json.loads(l) for l in raw.text.splitlines() if l.strip()]
rebuilt = [json.loads(l) for l in open("sar_log_rebuilt.jsonl", encoding="utf-8") if l.strip()]
last_rebuilt_t = max(r["t"] for r in rebuilt)

# 修正後(確定足)の行のうち、再構築ファイルより後の足だけを採用。同じtは後の行を優先
after = {}
for r in live:
    if r.get("bar_confirmed") and r["t"] > last_rebuilt_t:
        after[r["t"]] = r

# 再構築ファイルの最終足と、稼働中ログの同じ足が一致しているかを確認
live_same = [r for r in live if r.get("bar_confirmed") and r["t"] == last_rebuilt_t]
rb_last = [r for r in rebuilt if r["t"] == last_rebuilt_t][0]
if live_same:
    a, b = live_same[-1], rb_last
    ok = abs(a["sar"] - b["sar"]) < 1e-6 and a["dots_since_flip"] == b["dots_since_flip"] and a["trend"] == b["trend"]
    print(f"接続点 {datetime.fromtimestamp(last_rebuilt_t, tz=timezone.utc):%m-%d %H:%M} UTC の一致確認: {'OK' if ok else 'NG(差し替えを中止してください)'}")
    if not ok:
        sys.exit(1)

rows = []
for r in rebuilt:
    r.setdefault("interval", "15min")
    # recorded_at を参照するツール向けに、足の確定時刻(t+15分)を入れておく(実際の記録時刻ではない)
    r.setdefault("recorded_at", datetime.fromtimestamp(r["t"] + 900, tz=timezone.utc).isoformat())
    rows.append(r)
rows += [after[t] for t in sorted(after)]

ts = [r["t"] for r in rows]
assert ts == sorted(ts) and len(ts) == len(set(ts)), "時刻の重複または順序の乱れがあります"
gaps = sum(1 for x, y in zip(ts, ts[1:]) if y - x != 900)

with open("sar_log.jsonl", "w", encoding="utf-8", newline="\n") as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")

print(f"旧ログ保管 : sar_log_v1_unconfirmed.jsonl  {len(live)}行")
print(f"差し替え用 : sar_log.jsonl  {len(rows)}行(再構築 {len(rebuilt)} + 修正後 {len(after)})  欠損 {gaps}")
print(f"最終行の足 : {datetime.fromtimestamp(ts[-1], tz=timezone.utc):%m-%d %H:%M} UTC")
print("→ 次の自動実行(xx:00/15/30/45)より前にGitHubへアップロードしてください")
