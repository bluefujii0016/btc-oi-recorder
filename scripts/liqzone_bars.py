#!/usr/bin/env python3
"""
CoinGlass Liq Map のスクリーンショットから、横線(帯)とローソク足の経路を抽出する。
  帯: 行ごとに、背景(68,0,83)でない連続区間の左端x・右端x・色(viridis 上の位置)の推移を取り、
      同じ形の隣り合う行を1本の帯にまとめる。ローソク足(赤(223,61,72)・緑(0,201,129))の画素は欠測として扱う。
  ローソク足: 列ごとの高値・安値(価格)。x座標→時刻の校正に使う。
"""
import glob, json, os, re, sys
import numpy as np
from PIL import Image

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "liqzone")
BG = np.array([68, 0, 83])
X0, X1 = 30, 675          # プロット領域のx範囲 [X0, X1)
Y1 = 1167                 # 下端(ナビゲーションバーの上)
CANDLE = [np.array([223, 61, 72]), np.array([0, 201, 129])]

# viridis の参照(色→強さの順序づけに使う)。matplotlib の viridis を256段で
def _viridis():
    import matplotlib
    cm = matplotlib.colormaps["viridis"]
    return (np.array([cm(i / 255.0)[:3] for i in range(256)]) * 255).round().astype(int)
LUT = _viridis()


def intensity(px):
    """画素(N,3) → viridis上の位置(0〜1)。背景に近いほど0。"""
    d = ((px[:, None, :] - LUT[None, :, :]) ** 2).sum(axis=2)
    return d.argmin(axis=1) / 255.0


def load(png, y0):
    a = np.array(Image.open(png).convert("RGB")).astype(int)
    return a[y0:Y1, X0:X1, :]


def masks(plot, tol=14):
    cand = np.zeros(plot.shape[:2], bool)
    for c in CANDLE:
        cand |= (np.abs(plot - c).sum(axis=2) <= tol)
    bar = (np.abs(plot - BG).sum(axis=2) >= 6) & ~cand
    return cand, bar


def candle_columns(plot, cand, k, c, y0):
    """列ごとのローソク足の高値・安値(価格)。ローソク足の画素が無い列は NaN。"""
    H, W = cand.shape
    hi = np.full(W, np.nan); lo = np.full(W, np.nan)
    for x in range(W):
        ys = np.where(cand[:, x])[0]
        if len(ys):
            hi[x] = k * (ys.min() + y0) + c
            lo[x] = k * (ys.max() + y0) + c
    return hi, lo


def extract_bars(plot, cand, bar, k, c, y0, min_len=25):
    """行ごとの区間 → 同じ形の隣り合う行を1本の帯にまとめる。"""
    H, W = bar.shape
    rows = []
    inten = np.full((H, W), np.nan)
    flat = plot.reshape(-1, 3)
    iv = intensity(flat).reshape(H, W)
    inten[bar] = iv[bar]
    for y in range(H):
        known = bar[y] | (~cand[y])         # 候補: 背景かバー(ローソク足は欠測)
        xs = np.where(bar[y])[0]
        if len(xs) < min_len:
            rows.append(None); continue
        # 欠測(ローソク足)は両側がバーなら埋める。左端・右端は既知画素から決める
        x_start, x_end = int(xs.min()), int(xs.max())
        seg = inten[y, x_start:x_end + 1].copy()
        # 欠測を線形補間
        idx = np.arange(len(seg)); ok = ~np.isnan(seg)
        if ok.sum() < min_len:
            rows.append(None); continue
        seg = np.interp(idx, idx[ok], seg[ok])
        # 右端の強さ(現在の累積量の目安): 右端20画素の中央値
        rows.append({"y": y, "x0": x_start, "x1": x_end, "seg": seg, "right": float(np.median(seg[-20:])), "n": int(ok.sum())})
    bars, cur = [], None
    for r in rows:
        if r is None:
            if cur: bars.append(cur); cur = None
            continue
        if cur and abs(r["x0"] - cur["x0"]) <= 2 and abs(r["right"] - cur["right_mean"]) <= 0.04:
            cur["rows"].append(r)
            cur["right_mean"] = float(np.mean([q["right"] for q in cur["rows"]]))
        else:
            if cur: bars.append(cur)
            cur = {"rows": [r], "x0": r["x0"], "right_mean": r["right"]}
    if cur: bars.append(cur)
    out = []
    for b in bars:
        ys = [r["y"] for r in b["rows"]]
        best = max(b["rows"], key=lambda r: r["n"])      # 欠測の少ない行を代表にする
        ymid = (min(ys) + max(ys)) / 2 + y0
        out.append({"price": round(k * ymid + c, 1), "y_top": min(ys) + y0, "y_bot": max(ys) + y0, "thick_px": len(ys),
                    "x0": int(np.median([r["x0"] for r in b["rows"]])) + X0, "x1": int(np.median([r["x1"] for r in b["rows"]])) + X0,
                    "right_intensity": round(b["right_mean"], 3),
                    "profile": [round(float(v), 3) for v in best["seg"][::8]], "profile_step_px": 8})
    return out


def geometry(sample_id):
    """校正済みの価格軸(k, c)とプロット上端 y0 を、標本のJSONと README の取り決めから返す。"""
    G = {  # id: (y0, k, c)   price = k*y + c    (k, c は liqzone_readimg.py の当てはめ)
        "20261030_0106": (370, -11.140940, 88920.0211),
        "20261030_1336": (375, -12.609200, 89586.1902),
        "20261030_2140": (375, -10.081062, 86134.4545),
        "20261031_0130": (457, -9.235210, 85893.1790),
        "20261031_1332": (470, -8.371924, 84836.8406),
        "20261031_2255": (538, -7.990323, 85290.9953),
        "20261101_0254": (538, -7.652210, 85131.0557),
        "20261101_0931": (538, -8.139406, 85851.5426),
        "20261102_2349": (455, -9.399389, 86326.8872),
    }
    return G[sample_id]


def png_for(sample_id):
    return glob.glob(os.path.join(ROOT, "calibration", sample_id + "UTC_binance_btcusdt_24h_model1_th0.90.png"))[0]


if __name__ == "__main__":
    sid = sys.argv[1]
    y0, k, c = geometry(sid)
    plot = load(png_for(sid), y0)
    cand, bar = masks(plot)
    bars = extract_bars(plot, cand, bar, k, c, y0)
    print(sid, "bars:", len(bars))
    for b in bars:
        print(f"{b['price']:9.1f}  thick {b['thick_px']:2d}  x {b['x0']:3d}-{b['x1']:3d}  right {b['right_intensity']:.2f}  profile {b['profile'][:6]}...")
