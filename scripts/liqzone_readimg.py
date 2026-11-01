#!/usr/bin/env python3
"""
CoinGlass Liq Map のスクリーンショット(スマホ)から、価格軸と黄色の線を読む。
使い方: liqzone_readimg.py 画像 プロット上端のy 価格ラベル(上から順に、カンマ区切り)
  価格軸は、右端のラベル(黒い文字)の中心のyに対する最小二乗で当てはめる(残差を表示)。
  黄色の線: R>200, G>200, B<110 の行のうち、幅が MINW 以上のもの。黄緑: R>120, G>190, B<110, 幅 40 以上(参考)。
  ローソク足の高値・安値の目安も出す(時刻の突き合わせ用。黄緑の線を拾うことがあるので参考値)。
"""
import sys
import numpy as np
from PIL import Image


def read(path, y0, labels, y1=1167, minw=25):
    a = np.array(Image.open(path).convert("RGB")).astype(int)
    sub = a[:, 676:748, :]
    rows = np.where((sub.sum(axis=2) < 200).any(axis=1))[0]
    g, s, p = [], rows[0], rows[0]
    for r in rows[1:]:
        if r - p > 3:
            g.append((s, p)); s = r
        p = r
    g.append((s, p))
    cen = [(x + y) / 2 for x, y in g if y0 <= x <= y1]
    if len(cen) != len(labels):
        raise SystemExit(f"ラベル数が合いません: 検出 {len(cen)} 個 / 指定 {len(labels)} 個 {cen}")
    k, c = np.polyfit(cen, labels, 1)
    resid = [round(l - (k * y + c), 1) for y, l in zip(cen, labels)]
    px = lambda y: k * y + c
    plot = a[y0:y1, 30:674, :]

    def lines(m, w):
        cnt = m.sum(axis=1)
        ys = np.where(cnt >= w)[0]
        out = []
        if len(ys) == 0:
            return out
        gg, s, p = [], ys[0], ys[0]
        for r in ys[1:]:
            if r - p > 1:
                gg.append((s, p)); s = r
            p = r
        gg.append((s, p))
        for s0, e0 in gg:
            xs = np.where(m[s0:e0 + 1].any(axis=0))[0] + 30
            out.append((round(px((s0 + e0) / 2 + y0)), int(xs.min()), int(xs.max()), int(cnt[s0:e0 + 1].max())))
        return out

    ylw = (plot[:, :, 0] > 200) & (plot[:, :, 1] > 200) & (plot[:, :, 2] < 110)
    lime = (plot[:, :, 0] > 120) & (plot[:, :, 1] > 190) & (plot[:, :, 2] < 110)
    R = (a[:, :, 0] > 200) & (a[:, :, 1] < 100) & (a[:, :, 2] < 100)
    G = (a[:, :, 1] > 170) & (a[:, :, 0] < 90) & (a[:, :, 2] < 140) & (a[:, :, 1] - a[:, :, 2] > 60)
    cd = (R | G)[y0:y1, 35:672]
    ys = np.where(cd.any(axis=1))[0] + y0
    last = (R | G)[y0:y1, 650:668]
    yl = np.where(last.any(axis=1))[0] + y0
    return {"fit": (float(k), float(c)), "resid": resid, "yellow": lines(ylw, minw), "yellow_green": lines(lime, 40),
            "candle_high_low_approx": (round(px(ys.min())), round(px(ys.max()))),
            "last_candles": (round(px(yl.min())), round(px(yl.max()))) if len(yl) else None}


if __name__ == "__main__":
    path, y0, labels = sys.argv[1], int(sys.argv[2]), [float(x) for x in sys.argv[3].split(",")]
    r = read(path, y0, labels)
    for k, v in r.items():
        print(k, v)
