#!/usr/bin/env python3
"""
清算帯推定 フェーズ2: 探索期間での「帯に触れたあとの動き」の集計(探索期間のみ)

  1) 推定の清算帯に触れたイベントを抽出する
  2) 損切り幅×利確幅のグリッドで、OCO放置の損益(手数料控除後)と勝率・最大連敗を出す
  3) 比較対象(プラセボ): 帯の価格を上に±1.0%ずらして同じ処理をした結果。
     本物の帯がプラセボより良くなければ、清算帯に固有の優位性はない
結果は data/liqzone/est/explore_<params_id>/ に保存する。確認期間・最終確認期間のデータは読み込まない。
"""
import itertools
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import liqzone_est as E

SL = [0.3, 0.5, 0.75, 1.0]
TP = [0.3, 0.5, 0.75, 1.0, 1.5]


def streak(x):
    """損益がマイナスの最大連続回数"""
    best = cur = 0
    for v in x:
        cur = cur + 1 if v < 0 else 0
        best = max(best, cur)
    return best


def table(df, ev, label, fee):
    rows = []
    for s, tp in itertools.product(SL, TP):
        r = E.evaluate(df, ev, s, tp, fee_rt_pct=fee)
        rows.append({"label": label, "sl%": s, "tp%": tp, "n": len(r), "win%": round((r > 0).mean() * 100, 1),
                     "exp%": round(r.mean(), 4), "sum%": round(r.sum(), 1), "max_lose_streak": streak(r)})
    return pd.DataFrame(rows)


def main():
    p = dict(E.DEFAULT)
    pid = E.params_id(p)
    out = os.path.join(E.ROOT, "est", f"explore_{pid}")
    os.makedirs(out, exist_ok=True)
    df, miss = E.load_explore()
    print(f"params_id={pid} 期間 {len(df)}本 欠損補完 {miss}")
    res = {}
    for label, shift in (("real", 0.0), ("placebo+1.0", 1.0), ("placebo-1.0", -1.0)):
        ev = E.run(df, p, shift_pct=shift)
        res[label] = ev
        ev.to_csv(os.path.join(out, f"events_{label}.csv"), index=False)
        print(f"{label}: イベント {len(ev)}件(上の帯 {int((ev.side == 'U').sum())} / 下の帯 {int((ev.side == 'L').sum())})")
    tabs = []
    for fee in (0.05, 0.10):
        for label, ev in res.items():
            t = table(df, ev, label, fee)
            t["fee_rt%"] = fee
            tabs.append(t)
    allt = pd.concat(tabs, ignore_index=True)
    allt.to_csv(os.path.join(out, "grid.csv"), index=False)
    with open(os.path.join(out, "params.json"), "w") as f:
        json.dump(p, f, ensure_ascii=False, indent=1)
    pd.set_option("display.width", 200)
    for fee in (0.05,):
        for label in res:
            t = allt[(allt.label == label) & (allt["fee_rt%"] == fee)]
            print(f"\n== {label} / 手数料(往復){fee}% / 時間切れ4時間 ==")
            print(t.drop(columns=["label", "fee_rt%"]).to_string(index=False))


if __name__ == "__main__":
    main()
