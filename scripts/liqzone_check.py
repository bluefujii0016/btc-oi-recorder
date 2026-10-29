#!/usr/bin/env python3
"""
清算帯推定 フェーズ1 ステップ4: 欠損・停止の検知と Discord 通知

確認すること(異常のときだけ通知):
  1. 収集の停止      : OI/価格/清算の最新の確定足が STALE_SEC(既定3時間)より古い
  2. 連続欠損        : 直近7日の OI・価格・清算(各系列)の5分の格子で、GAP_BARS(既定12本=1時間)以上の連続欠損
  3. 収集ジョブの失敗 : 環境変数 COLLECT_OUTCOME が failure

通知先は環境変数 LIQZONE_DISCORD_WEBHOOK_URL(SAR通知とは別チャンネル用)。
未設定なら通知せず、標準出力に結果だけ出す(既存の DISCORD_WEBHOOK_URL は使わない)。
同じ異常は24時間に1回まで。異常が解消したら「復旧」を1回送る。
状態は data/liqzone/quality/alert_state.json に保存し、結果は quality_log.jsonl に追記する(異常時のみ)。
常に終了コード0(通知の失敗でジョブを赤くしない)。標準ライブラリのみ。
"""
import csv
import glob
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "liqzone")
BAR = 300
STALE_SEC = int(os.environ.get("LIQZONE_STALE_SEC", 3 * 3600))
GAP_BARS = int(os.environ.get("LIQZONE_GAP_BARS", 12))
LOOKBACK_DAYS = 7
RESEND_SEC = 24 * 3600
WEBHOOK = os.environ.get("LIQZONE_DISCORD_WEBHOOK_URL", "").strip()
SERIES = {"oi_5m": "OI", "ohlcv_5m": "価格", "liq_5m": "清算"}


def utc(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%m-%d %H:%M")


def times(kind, now):
    """直近 LOOKBACK_DAYS+1 日分のファイルから t の昇順リストを返す。清算は symbol ごとに分ける。"""
    out = {}
    start = (datetime.fromtimestamp(now, timezone.utc).date() - timedelta(days=LOOKBACK_DAYS + 1)).isoformat()
    for f in sorted(glob.glob(os.path.join(ROOT, "raw", kind, "*.csv"))):
        if os.path.basename(f)[:-4] < start:
            continue
        with open(f, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                out.setdefault(r.get("symbol", ""), set()).add(int(r["t"]))
    return {k: sorted(v) for k, v in out.items()}


def find_problems(now, collect_outcome=""):
    problems = []
    since = now - LOOKBACK_DAYS * 86400
    for kind, label in SERIES.items():
        series = times(kind, now)
        if not series:
            problems.append((f"empty:{kind}", f"{label}: データがありません"))
            continue
        for sym, ts in series.items():
            name = f"{label}" + (f"({sym})" if sym else "")
            last = ts[-1]
            if now - (last + BAR) > STALE_SEC:
                hours = (now - last - BAR) / 3600
                problems.append((f"stale:{kind}:{sym}", f"{name}: 最新の足が {utc(last)} UTC で止まっています(約{hours:.1f}時間前)"))
            recent = [t for t in ts if t >= since]
            for a, b in zip(recent, recent[1:]):
                n = (b - a) // BAR - 1
                if n >= GAP_BARS:
                    problems.append((f"gap:{kind}:{sym}:{a}", f"{name}: {utc(a + BAR)} UTC から {n}本({n * 5}分)連続で欠損"))
    if collect_outcome == "failure":
        problems.append(("collect_failed", "収集ジョブ(collect_live)が失敗しました。Actions のログを確認してください"))
    return problems


def post(text):
    body = json.dumps({"content": text[:1900]}).encode()
    req = urllib.request.Request(WEBHOOK, data=body, headers={"Content-Type": "application/json", "User-Agent": "liqzone-check"})
    urllib.request.urlopen(req, timeout=30).read()


def send_test():
    """LIQZONE_TEST=1: 疎通確認用。固定のテスト文を1通送るだけで、状態もログも書かない。"""
    if not WEBHOOK:
        print("LIQZONE_DISCORD_WEBHOOK_URL が未設定です")
        return 1
    post("🧪 清算帯データ収集: Discord 通知のテストです(異常ではありません)")
    print("テスト通知を送信しました")
    return 0


def main():
    if os.environ.get("LIQZONE_TEST") == "1":
        return send_test()
    now = int(time.time())
    qdir = os.path.join(ROOT, "quality")
    os.makedirs(qdir, exist_ok=True)
    state_path = os.path.join(qdir, "alert_state.json")
    try:
        state = json.load(open(state_path, encoding="utf-8"))
    except Exception:
        state = {}
    problems = find_problems(now, os.environ.get("COLLECT_OUTCOME", ""))
    keys = sorted(k for k, _ in problems)
    print(f"異常 {len(problems)}件" if problems else "異常なし")
    for _, msg in problems:
        print(f"  - {msg}")

    msgs, sent_at = [], state.get("sent_at", {})
    for k, msg in problems:
        if now - sent_at.get(k, 0) >= RESEND_SEC:
            msgs.append(msg)
    recovered = bool(state.get("active")) and not problems

    try:
        if WEBHOOK:
            if msgs:
                post("⚠️ 清算帯データ収集の異常\n" + "\n".join(f"・{m}" for m in msgs))
                for k, msg in problems:
                    if msg in msgs:
                        sent_at[k] = now
            elif recovered:
                post("✅ 清算帯データ収集: 異常は解消しました")
        elif msgs or recovered:
            print("LIQZONE_DISCORD_WEBHOOK_URL が未設定のため通知しません")
    except Exception as e:  # 通知の失敗でジョブを止めない。次回の実行で再送される
        print(f"通知の送信に失敗: {e}")
        msgs = []
        sent_at = state.get("sent_at", {})

    if problems:
        with open(os.path.join(qdir, "quality_log.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "job": "check", "problems": [m for _, m in problems]}, ensure_ascii=False) + "\n")
    new_state = {"active": keys, "sent_at": {k: v for k, v in sent_at.items() if k in keys}}
    if new_state != {k: state.get(k) for k in new_state}:
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(new_state, f, ensure_ascii=False, sort_keys=True, indent=1)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"check 失敗: {e}")
        sys.exit(0)
