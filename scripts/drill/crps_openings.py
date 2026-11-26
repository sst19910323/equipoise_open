"""CRPS / PIT of every drill opening tree vs the realised 15-day return. Zero-token: existing trees + god-view bars.
Two tree readings: point masses (discrete) and a Gaussian mixture with residual sigma (sigma_ref^2 - between-node var).
Skill = 1 - CRPS_tree / CRPS_climatology, climatology = N(0, realised SD of 15-day returns, last year)."""
import json, os, glob, csv, sys, math, statistics as st
sys.path.insert(0, "/opt/equipoise/scripts")
D = "/opt/equipoise_drill"
H = 15
def closes(sym):
    f = sorted(glob.glob(f"/opt/equipoise/data/market_data/{sym}/{sym}_5y_1d_*.csv"))
    if not f: return None
    with open(f[-1]) as fh: return [(r["date"][:10], float(r["close"])) for r in csv.DictReader(fh)]
def Phi(z): return 0.5 * (1 + math.erf(z / math.sqrt(2)))
def phi(z): return math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
def crps_norm(mu, sd, y):
    if sd <= 0: return abs(y - mu)
    z = (y - mu) / sd
    return sd * (z * (2 * Phi(z) - 1) + 2 * phi(z) - 1 / math.sqrt(math.pi))
def crps_points(pts, y):          # pts: [(p, x)] discrete; CRPS = E|X-y| - 0.5 E|X-X'|
    e1 = sum(p * abs(x - y) for p, x in pts)
    e2 = sum(p * q * abs(x - x2) for p, x in pts for q, x2 in pts)
    return e1 - 0.5 * e2
def crps_mix(comps, y, n=4001):   # comps: [(p, mu, sd)]; numeric integral of (F-1{x>=y})^2
    lo = min(m - 5 * s for _, m, s in comps); hi = max(m + 5 * s for _, m, s in comps); lo = min(lo, y - 0.01); hi = max(hi, y + 0.01)
    dx = (hi - lo) / (n - 1); acc = 0.0
    for i in range(n):
        x = lo + i * dx
        F = sum(p * Phi((x - m) / s) for p, m, s in comps)
        acc += (F - (1.0 if x >= y else 0.0)) ** 2 * dx
    return acc
rows = []
for root in sorted(glob.glob(f"{D}/*_*_*")):
    name = os.path.basename(root)
    if not os.path.isdir(root) or name.endswith("_preruling"): continue
    parts = name.split("_"); run = parts[-1]; ts = parts[-2]; sym = "_".join(parts[:-2])
    if run not in ("r1x", "r1y", "r1z", "r1w", "r1v", "b1", "c1", "v352"): continue
    sd_dir = f"{root}/data/store/{sym}/{ts}"
    if not os.path.exists(f"{sd_dir}/R1.Z.json") or not os.path.exists(f"{sd_dir}/R1.R.json"): continue
    for side in "ZR":
        c = json.load(open(f"{sd_dir}/R1.{side}.json"))["content"]
        trees = c.get("scenario_by_lens") or {sym: c["scenario"]}
        for L, sc in trees.items():
            cl = closes(L)
            if not cl: continue
            day = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
            i = max(j for j, (d, _) in enumerate(cl) if d < day)
            if i + H >= len(cl): continue
            px = [x for _, x in cl[:i + 1]]
            y = cl[i + H][1] / cl[i][1] - 1
            r15 = [px[k] / px[k - H] - 1 for k in range(max(H, len(px) - 250), len(px))]
            sd_ref = st.pstdev(r15)
            P0 = float(sc["P0"]); nodes = sc["nodes"]
            pts = [(float(n["p"]), float(n["P_T"]) / P0 - 1) for n in nodes]
            mean = sum(p * x for p, x in pts); var_b = sum(p * (x - mean) ** 2 for p, x in pts)
            sig_res = math.sqrt(max(sd_ref ** 2 - var_b, (0.2 * sd_ref) ** 2))
            c_pt = crps_points(pts, y); c_mx = crps_mix([(p, x, sig_res) for p, x in pts], y); c_cl = crps_norm(0.0, sd_ref, y)
            F = sum(p * Phi((y - x) / sig_res) for p, x in pts); pit = min(F, 1 - F)
            rows.append((name, side, L, y, mean, math.sqrt(var_b) / sd_ref, c_pt / c_cl, c_mx / c_cl, pit))
print("node run side lens | real15 treeMean treeSD/ref | CRPS_pt/clim CRPS_mix/clim | PIT tail")
for r in rows: print(f"{r[0][:32]:32s} {r[1]} {r[2]:5s} | {r[3]*100:+5.1f}% {r[4]*100:+5.1f}% {r[5]:.2f} | {r[6]:.2f} {r[7]:.2f} | {r[8]:.2f}")
print("\n== by run (mean over sides x lenses): n, CRPS_pt/clim, CRPS_mix/clim, share PIT<0.05, share PIT<0.25")
for run in ("r1x", "r1y", "r1z", "r1w", "r1v", "b1", "c1", "v352"):
    rr = [r for r in rows if r[0].endswith("_" + run)]
    if not rr: continue
    print(f"  {run:5s} n={len(rr):2d}  pt {st.mean(r[6] for r in rr):.2f}  mix {st.mean(r[7] for r in rr):.2f}  <0.05 {sum(r[8]<0.05 for r in rr)/len(rr):.2f}  <0.25 {sum(r[8]<0.25 for r in rr)/len(rr):.2f}")
print("\n== by side (all runs)")
for s in "ZR":
    rr = [r for r in rows if r[1] == s]
    print(f"  {s} n={len(rr)} pt {st.mean(r[6] for r in rr):.2f} mix {st.mean(r[7] for r in rr):.2f} meanPIT {st.mean(r[8] for r in rr):.2f}")
