# Бэктест текущих настроек 12 стратегий на минутных свечах + сравнение со случайными входами
import numpy as np,sys,json
from datetime import datetime,timezone,timedelta
sys.stdout.reconfigure(encoding='utf-8')
MSK=3*3600; COMM=0.0005; HS=0.00011
CFG={'SBER': ['BBG004730N88', 'mean', 0.012, 0.015, 0], 'GMKN': ['BBG004731489', 'breakout', 0.005, 0.015, 0], 'TRNFP': ['BBG00475KHX6', 'mean', 0.005, 0.02, 0], 'IRAO': ['BBG004S68473', 'trend', 0.012, 0.015, 0], 'MOEX': ['BBG004730JJ5', 'mean', 0.008, 0.02, 0], 'GAZP': ['BBG004730RP0', 'trend', 0.012, 0.015, 25], 'ALRS': ['BBG004S68B31', 'breakout', 0.012, 0.015, 35], 'VTBR': ['BBG004730ZJ9', 'trend', 0.012, 0.015, 20], 'TATN': ['BBG004RVFFC0', 'mean', 0.012, 0.02, 0], 'ROSN': ['BBG004731354', 'trend', 0.008, 0.015, 50], 'NVTK': ['BBG00475KKY8', 'trend', 0.012, 0.02, 0], 'AFLT': ['BBG004S683W7', 'trend', 0.012, 0.02, 0]}
def load(tk):
    return np.load(f'npz/{CFG[tk][0]}.npz')['a']
def roll_mean(x,n):
    c=np.cumsum(np.r_[0,x]);r=np.full(len(x),np.nan);r[n-1:]=(c[n:]-c[:-n])/n;return r
def roll_std(x,n):
    m=roll_mean(x,n);m2=roll_mean(x*x,n);return np.sqrt(np.maximum(m2-m*m,0))
def roll_ext(x,n,f):
    from numpy.lib.stride_tricks import sliding_window_view as sw
    r=np.full(len(x),np.nan);r[n-1:]=f(sw(x,n),axis=1);return r
def signals(a,mode,minscore=0):
    o,h,l,c,v=a[:,1],a[:,2],a[:,3],a[:,4],a[:,5];n=len(c);s=np.zeros(n);sc=np.zeros(n)
    if mode=='trend':
        f,sl=roll_mean(c,9),roll_mean(c,21);m=np.full(n,0.);m[4:]=(c[4:]/c[:-4]-1)*100
        gap=np.abs((f-sl)/sl)*1000;sc=(np.clip(gap,0,100)+np.clip(np.abs(m)*15,0,100))/2
        s[(f>sl)&(m>0)]=1;s[(f<sl)&(m<0)]=-1;s[:30]=0
    elif mode=='mean':
        m,sd=roll_mean(c,20),roll_std(c,20);z=np.where(sd>0,(c-m)/np.where(sd>0,sd,1),0);sc=np.minimum(np.abs(z)*30,100)
        s[z<=-1.8]=1;s[z>=1.8]=-1;s[:25]=0
    else:
        ph=np.r_[np.nan,roll_ext(h,20,np.max)[:-1]];pl=np.r_[np.nan,roll_ext(l,20,np.min)[:-1]];av=roll_mean(v,20);vr=np.where(av>0,v/np.where(av>0,av,1),0)
        br=np.where(c>ph,c-ph,np.where(c<pl,pl-c,0))*100;sc=(np.clip(br,0,100)+np.clip(vr*30,0,100))/2
        s[(c>ph)&(vr>=1.2)]=1;s[(c<pl)&(vr>=1.2)]=-1;s[:25]=0
    s[sc<minscore]=0
    return s
def tradable(a):
    t=a[:,0]+MSK;mn=(t%86400)//60;wd=((t//86400)+3)%7
    return (wd<5)&(((mn>=600)&(mn<1130))|((mn>=1145)&(mn<1430)))
def exit_of(a,i,side,sl,tp):
    """Вход по открытию бара i. Возвращает (индекс выхода, чистая доходность) или None, если не закрылась."""
    o,h,l=a[:,1],a[:,2],a[:,3];e=o[i]*(1+side*HS);slp=e*(1-side*sl);tpp=e*(1+side*tp);w=256;j=i;n=len(o)
    while j<n:
        k=min(n,j+w)
        if side>0: hs=l[j:k]<=slp;ht=h[j:k]>=tpp
        else: hs=h[j:k]>=slp;ht=l[j:k]<=tpp
        mask=hs|ht
        if mask.any():
            q=j+int(np.argmax(mask))
            if hs[q-j]:
                px=(min(o[q],slp) if side>0 else max(o[q],slp))*(1-side*HS)
            else: px=tpp
            return q,side*(px/e-1)-2*COMM
        j=k;w*=2
    return None
def run(a,s,sl,tp,dirs=(1,-1)):
    T=tradable(a);idx=np.flatnonzero((s!=0)&T);n=len(a);pos=0;out=[]
    for i in idx:
        if i<pos or i+1>=n: continue
        side=int(s[i])
        if side not in dirs: continue
        r=exit_of(a,i+1,side,sl,tp)
        if r is None: break
        q,ret=r;out.append((a[i+1,0],a[q,0],side,ret));pos=q+2
    return out
def rnd_baseline(a,trades,sl,tp,rng):
    T=np.flatnonzero(tradable(a));ts=a[:,0];res=[]
    month=lambda x:datetime.fromtimestamp(x+MSK,timezone.utc).strftime('%Y-%m')
    by={}
    m=np.array([(int(x+MSK)//86400) for x in ts[T]])
    for t0,_,side,_ in trades:
        d=int(t0+MSK)//86400;lo,hi=np.searchsorted(m,[d-15,d+15]);
        if hi<=lo: continue
        i=T[rng.integers(lo,hi)]
        if i+1>=len(a): continue
        r=exit_of(a,i+1,side,sl,tp)
        if r: res.append((a[i+1,0],a[r[0],0],side,r[1]))
    return res
def boot(rets,weeks,B=2000,seed=1):
    rng=np.random.default_rng(seed);u,inv=np.unique(weeks,return_inverse=True);S=np.bincount(inv,rets,len(u));N=np.bincount(inv,minlength=len(u))
    idx=rng.integers(0,len(u),(B,len(u)));m=S[idx].sum(1)/N[idx].sum(1);return np.percentile(m,[2.5,97.5])
def stat(tr,label):
    tr=np.array(tr);
    if len(tr)==0: print(label,'0');return
    r=tr[:,3]*100;w=((tr[:,0]+MSK)//(7*86400));lo,hi=boot(r,w)
    print(f'{label:<28} n={len(r):<6} ср.={r.mean():+.3f}% CI[{lo:+.3f},{hi:+.3f}] wr={np.mean(r>0):.2f} сумма={r.sum():+.0f}% hold_ч={np.median(tr[:,1]-tr[:,0])/3600:.1f}')
if __name__=='__main__':
    rng=np.random.default_rng(7);ALL=[];RND=[];rows={}
    for tk,(figi,mode,sl,tp,ms) in CFG.items():
        a=load(tk);s=signals(a,mode,ms);tr=run(a,s,sl,tp);rows[tk]=tr
        rb=[]
        for _ in range(5): rb+=rnd_baseline(a,tr,sl,tp,rng)
        stat(tr,f'{tk} {mode} {sl*100:.1f}/{tp*100:.1f}');stat(rb,f'   случайные {tk}')
        ALL+=tr;RND+=rb
    print();stat(ALL,'ВСЕ текущие');stat(RND,'ВСЕ случайные (те же SL/TP)')
    A=np.array(ALL)
    stat(A[A[:,2]>0].tolist(),'только лонг');stat(A[A[:,2]<0].tolist(),'только шорт')
    R=np.array(RND);stat(R[R[:,2]>0].tolist(),'случ. лонг');stat(R[R[:,2]<0].tolist(),'случ. шорт')
    print('\nпо месяцам (сумма % на сделку 1 ед.; сделки, wr)')
    mon=[datetime.fromtimestamp(x+MSK,timezone.utc).strftime('%Y-%m') for x in A[:,1]]
    for m in sorted(set(mon)):
        k=np.array(mon)==m;x=A[k]
        print(m,'n',k.sum(),'сумма%',round(x[:,3].sum()*100,1),'лонг',round(x[x[:,2]>0,3].sum()*100,1),'шорт',round(x[x[:,2]<0,3].sum()*100,1),'wr',round((x[:,3]>0).mean(),2))
    d=((A[:,1]+MSK)//86400).astype(int);u,inv=np.unique(d,return_inverse=True);dd=np.bincount(inv,A[:,3]*100)
    print('дней',len(u),'плюсовых',round((dd>0).mean(),3),'худший%',round(dd.min(),1),'лучший%',round(dd.max(),1),'ср.день%',round(dd.mean(),3))
    print('сверка 20.10-03.11 бэктест ₽ при 8000 на сделку:',round(A[(A[:,1]>=datetime(2026,10,20,tzinfo=timezone.utc).timestamp())][:,3].sum()*8000), 'n',int((A[:,1]>=datetime(2026,10,20,tzinfo=timezone.utc).timestamp()).sum()))
