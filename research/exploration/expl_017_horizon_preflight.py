"""Static EXPL-017 formal horizon containment preflight; no market data."""
from __future__ import annotations
import datetime as dt, hashlib, json
DAY=dt.timedelta(days=1); WEEK=dt.timedelta(days=7); ANCHOR=dt.date(2021,1,1); END=dt.date(2024,1,1)
SPLITS=(("train",dt.date(2021,1,1),dt.date(2022,1,1)),("oos",dt.date(2022,1,1),dt.date(2023,1,1)),("holdout",dt.date(2023,1,1),END))
EXPECTED={"train":(53,44),"oos":(52,51),"holdout":(52,51)}
def build():
 rows=[]; e=ANCHOR
 while e<END:
  d=e-DAY; x=e+WEEK; name,start,stop=next(s for s in SPLITS if s[1]<=e<s[2])
  warm=name=="train" and sum(r["split"]=="train" for r in rows)<8
  included=not warm and x<stop and x<END
  rows.append({"split":name,"decision":d.isoformat(),"execution":e.isoformat(),"endpoint":x.isoformat(),"ic_included":included,"reason":"warmup" if warm else "contained" if included else "cross_boundary_or_dataset"})
  e+=WEEK
 return rows
def artifact():
 rows=build(); counts={s:(sum(r["split"]==s for r in rows),sum(r["split"]==s and r["ic_included"] for r in rows)) for s,_,_ in SPLITS}
 if len(rows)!=157 or counts!=EXPECTED or any(r["ic_included"] and (dt.date.fromisoformat(r["endpoint"])>=END or not any(a<=dt.date.fromisoformat(r["endpoint"])<b and r["split"]==n for n,a,b in SPLITS)) for r in rows):raise RuntimeError("containment")
 return {"artifact_class":"EXPL-017_FORMAL_HORIZON_PREFLIGHT","status":"PASS","input_spec":{"anchor":"2021-01-01","cadence_days":7,"endpoint":"execution+7d","dataset_boundary_exclusive":"2024-01-01"},"expected_counts":EXPECTED,"rows":rows,"runtime_lifecycle_rule":"IC-eligible selected symbols require execution and endpoint opens; terminal/liquidation cannot substitute; missing endpoint is DATA_UNAVAILABLE."}
def write(path):
 data=artifact();path.write_text(json.dumps(data,indent=2,sort_keys=True)+"\n");return data
