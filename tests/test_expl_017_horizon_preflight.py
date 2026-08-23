import datetime as dt
from research.exploration import expl_017_horizon_preflight as p
def test_counts_boundaries_and_no_performance():
 a=p.artifact();assert len(a["rows"])==157 and a["expected_counts"]==p.EXPECTED
 rows={r["decision"]:r for r in a["rows"]};assert not rows["2021-12-30"]["ic_included"] and not rows["2022-12-29"]["ic_included"] and not rows["2023-12-28"]["ic_included"]
 assert all("return" not in k.lower() and "sharpe" not in k.lower() for k in a)
def test_deterministic_and_warmup():
 assert p.artifact()==p.artifact();train=[r for r in p.build() if r["split"]=="train"];assert sum(r["reason"]=="warmup" for r in train)==8
