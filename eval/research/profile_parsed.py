import json, sys, glob, os, statistics as st, collections, re
from transformers import AutoTokenizer
tok = AutoTokenizer.from_pretrained("BAAI/bge-small-en-v1.5")
d = sys.argv[1]
rows=[]; heads=collections.Counter(); bytype=collections.defaultdict(list)
alltok=[]
for p in sorted(glob.glob(os.path.join(d,"*.json"))):
    j=json.load(open(p,encoding="utf-8"))
    secs=j["sections"]
    toks=[len(tok.encode(s["text"],add_special_tokens=False)) for s in secs]
    alltok += [(t,j["type"],j["source_id"],s["heading"]) for t,s in zip(toks,secs)]
    total=sum(toks)
    bytype[j["type"]].append(total)
    rows.append((j["source_id"],j["type"],len(secs),total,max(toks),min(toks)))
    if j["type"]=="scheme_page":
        for s in secs: heads[re.sub(r"^\S+ (Cap|cap) ","",s["heading"] or "None")]+=1
print(f"{'source':34}{'type':12}{'secs':>5}{'tok':>7}{'max':>6}{'min':>5}")
for r in rows: print(f"{r[0]:34}{r[1]:12}{r[2]:>5}{r[3]:>7}{r[4]:>6}{r[5]:>5}")
print("\nTOTAL docs",len(rows),"sections",sum(r[2] for r in rows),"tokens",sum(r[3] for r in rows))
for t,v in bytype.items(): print(t, "docs",len(v),"tokens sum",sum(v),"median/doc",st.median(v))
ts=[a[0] for a in alltok]
qs=st.quantiles(ts,n=20)
print("\nsection tokens: median",st.median(ts),"p90",qs[17],"p95",qs[18],"max",max(ts))
for lim in (64,128,256,384,512): print(f"  > {lim}: {sum(t>lim for t in ts)}")
print("\nlargest sections:"); [print(" ",a) for a in sorted(alltok,reverse=True)[:12]]
print("\nsmallest sections:"); [print(" ",a) for a in sorted(alltok)[:15]]
print("\nGroww headings frequency:"); [print(f"  {c:3} {h}") for h,c in heads.most_common()]
