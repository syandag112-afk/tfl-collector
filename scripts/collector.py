"""GitHub Actions collector for Headway Desk (no AWS).
Reuses tfl_core.py (the old Lambda logic) with a local-disk store instead of S3.
  python scripts/collector.py collect           # every 5 min
  python scripts/collector.py rollup [DATE]     # daily; default = yesterday's service day
  python scripts/collector.py prune             # delete raw >14 d, exports >90 d
  python scripts/collector.py check [MINUTES]   # fail if no raw data landed in last N minutes
Storage:
  data/raw/<service-date>/<HHMMSS>.csv.gz      committed (inferred stop passages per run)
  data/exports/<service-date>/<route>.csv.gz   committed (scheduled vs expected vs actual)
  .cache/{state,meta,timetables}/              actions/cache only, never committed
"""
import gzip, io, json, os, shutil, sys, time
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(__file__))
import tfl_core as core

ROOT = os.environ.get("STORE_ROOT", os.getcwd())
CACHE_PREFIXES = ("state/", "meta/", "timetables/")

def _path(key):
    base = ".cache" if key.startswith(CACHE_PREFIXES) else "data"
    if key.startswith("exports/") and key.endswith(".csv"):
        key += ".gz"  # stored gzipped, name says so
    return os.path.join(ROOT, base, key)

class _Body:
    def __init__(self, b): self.b = b
    def read(self): return self.b

class LocalStore:
    """Just enough of the boto3 S3 client API for tfl_core."""
    def get_object(self, Bucket, Key):
        p = _path(Key)
        if not os.path.exists(p):
            raise FileNotFoundError("NoSuchKey " + Key)
        with open(p, "rb") as f:
            return {"Body": _Body(f.read())}
    def put_object(self, Bucket, Key, Body, **kw):
        if not Body:
            raise ValueError("refusing to write empty object " + Key)
        p = _path(Key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "wb") as f:
            f.write(Body)
        os.replace(tmp, p)  # atomic: never leaves a half-written file
    def list_objects_v2(self, Bucket, Prefix, **kw):
        base = os.path.dirname(_path(Prefix + "x"))
        out = []
        if os.path.isdir(base):
            for n in sorted(os.listdir(base)):
                out.append({"Key": Prefix + n})
        return {"Contents": out, "IsTruncated": False}

core._s3 = LocalStore()

# --- guard: never save an empty or failed snapshot ---
_orig_fetch = core.fetch_predictions
def _fetch(routes, all_lines):
    preds = _orig_fetch(routes, all_lines)
    if not preds:
        raise RuntimeError("TfL returned 0 predictions; not saving anything this run")
    return preds
core.fetch_predictions = _fetch

# --- dedupe passages in rollup (overlapping runs / retries) ---
_orig_build = core.build_route_rows
def _build(day, rows, tts):
    seen, uniq = set(), []
    for r in rows:
        k = (r["vehicle"], r["naptan"], r["route"], r["actual"][:16])
        if k not in seen:
            seen.add(k); uniq.append(r)
    return _orig_build(day, uniq, tts)
core.build_route_rows = _build

def prune(raw_days=14, export_days=90):
    today = core.service_date(datetime.now(timezone.utc))
    removed = []
    for sub, keep in (("raw", raw_days), ("exports", export_days)):
        base = os.path.join(ROOT, "data", sub)
        if not os.path.isdir(base):
            continue
        for d in os.listdir(base):
            try:
                day = datetime.strptime(d, "%Y-%m-%d").date()
            except ValueError:
                continue
            if (today - day).days > keep:
                shutil.rmtree(os.path.join(base, d)); removed.append(sub + "/" + d)
    print(json.dumps({"pruned": removed}))

def check(minutes=30):
    newest = 0
    base = os.path.join(ROOT, "data", "raw")
    for dp, _, files in os.walk(base):
        for f in files:
            if f.endswith(".csv.gz"):
                newest = max(newest, os.path.getmtime(os.path.join(dp, f)))
    # mtime is reset by git checkout, so also read the commit log time file
    stamp = os.path.join(ROOT, "data", "last_success.txt")
    if os.path.exists(stamp):
        with open(stamp) as f:
            newest = max(newest, float(f.read().split()[0]))
    age = (time.time() - newest) / 60 if newest else 1e9
    print(json.dumps({"minutes_since_last_data": round(age, 1)}))
    if age > minutes:
        sys.exit("ALERT: no data has landed for %.0f minutes (limit %d)" % (age, minutes))

def main():
    a = sys.argv[1:] or ["collect"]
    if a[0] == "collect":
        res = core.collect()
        if not res.get("skipped"):
            os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
            with open(os.path.join(ROOT, "data", "last_success.txt"), "w") as f:
                f.write("%d %s predictions=%s passages=%s\n" % (time.time(), core.iso(datetime.now(timezone.utc)),
                                                                res["predictions"], res["passages"]))
    elif a[0] == "rollup":
        day = datetime.strptime(a[1], "%Y-%m-%d").date() if len(a) > 1 else \
            core.service_date(datetime.now(timezone.utc)) - timedelta(days=1)
        core.rollup(day)
    elif a[0] == "prune":
        prune()
    elif a[0] == "check":
        check(int(a[1]) if len(a) > 1 else 30)
    else:
        sys.exit("unknown command " + a[0])

if __name__ == "__main__":
    main()
