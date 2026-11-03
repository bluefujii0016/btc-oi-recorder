import sys; import os; sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd, liqzone_bars as B, liqzone_calib as C, json
def candles(sid):
    y0,k,c=B.geometry(sid)
    plot=B.load(B.png_for(sid),y0); cand,bar=B.masks(plot)
    hi,lo=B.candle_columns(plot,cand,k,c,y0)
    has=~np.isnan(hi); runs=[];s=None
    for x in range(len(hi)):
        if has[x] and s is None: s=x
        if (not has[x]) and s is not None: runs.append((s,x-1)); s=None
    if s is not None: runs.append((s,len(hi)-1))
    runs=[(a,b) for a,b in runs if b-a>=4]
    return [(a+B.X0,b+B.X0,np.nanmax(hi[a:b+1]),np.nanmin(lo[a:b+1])) for a,b in runs]
def hourly(T):
    df,last=C.load_window(T)
    h=df.copy(); h['hr']=(h.index//3600)*3600
    return h.groupby('hr').agg(h=('h','max'),l=('l','min'))
if __name__=='__main__':
    for sid in ['20261030_0106','20261030_1336','20261030_2140','20261031_0130','20261031_1332','20261031_2255','20261101_0254','20261101_0931','20261102_2349']:
        cs=candles(sid); t=pd.Timestamp(sid[:8]+sid[9:],tz='UTC') if False else pd.Timestamp(f"{sid[:4]}-{sid[4:6]}-{sid[6:8]} {sid[9:11]}:{sid[11:13]}",tz='UTC')
        H=hourly(int(t.timestamp())); n=len(cs); best=[]
        for off in range(len(H)-n-6,len(H)+4):
            e=[abs(cs[j][2]-H.h.iloc[off+j])+abs(cs[j][3]-H.l.iloc[off+j]) for j in range(n) if 0<=off+j<len(H)]
            if len(e)>=20: best.append((np.median(e),off))
        best.sort(); m,off=best[0]
        last_row=off+n-1
        print(sid,'n',n,'widths',[b-a+1 for a,b,_,_ in cs][:3],'best med err',round(m),'last img candle ↔',pd.to_datetime(H.index[min(last_row,len(H)-1)],unit='s'),'(row offset past data end:',last_row-(len(H)-1),') 2nd',[ (round(x),o) for x,o in best[1:3]])
