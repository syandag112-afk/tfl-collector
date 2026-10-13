"""TfL bus live-data collector (all London bus routes).
Python 3.12, standard library only (storage injected by collector.py).

Actions (event["action"]):
  collect (default, every minute)  - poll TfL predictions, infer stop passages, save to S3
  rollup  (daily 04:20 London)     - build per-route CSVs (scheduled/expected/actual) for a service day
  Function URL GET ?token=..&date=YYYY-MM-DD&route=94  -> redirect to that day's CSV
Storage (S3 bucket env BUCKET, keys by service date; day rolls at 04:00 London):
  state/inflight.json.gz                      predictions currently in flight
  meta/lines.json                             bus line list (refreshed daily)
  timetables/{route}/{direction}.json.gz      cached weekly
  raw/{date}/{HHMMSS}.csv.gz                  passages inferred in that run
  exports/{date}/{route}.csv                  daily CSV per route
"""
import csv, gzip, io, json, os, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.tfl.gov.uk"
BUCKET = os.environ.get("BUCKET", "")
ROUTES = os.environ.get("ROUTES", "ALL").strip() or "ALL"
APP_KEY = os.environ.get("TFL_APP_KEY", "").strip()
ARRIVE_S = int(os.environ.get("ARRIVE_THRESHOLD_S", "90"))  # last timeToStation <= this when it vanished => arrived
TT_PER_RUN = int(os.environ.get("TIMETABLES_PER_RUN", "20"))  # timetable fetches per minute run
TT_MAX_AGE_D = int(os.environ.get("TIMETABLE_MAX_AGE_DAYS", "7"))
EXPORT_TOKEN = os.environ.get("EXPORT_TOKEN", "")
STALE_S = int(os.environ.get("STALE_S", "1200"))  # forget predictions not refreshed for 10 min (missed runs)
RAW_FIELDS = ["route", "direction", "naptan", "stop_name", "platform", "vehicle", "trip_id",
              "destination", "first_expected", "expected", "actual", "actual_source"]

_s3 = None
def s3():
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.client("s3")
    return _s3

# ---------- time helpers (no tzdata needed: UK DST rule built in) ----------
def _last_sunday(y, m):
    d = datetime(y, m + 1, 1, tzinfo=timezone.utc) - timedelta(days=1) if m < 12 else datetime(y, 12, 31, tzinfo=timezone.utc)
    return d - timedelta(days=(d.weekday() + 1) % 7)

def london_offset(utc):
    y = utc.year
    start = _last_sunday(y, 3).replace(hour=1)
    end = _last_sunday(y, 10).replace(hour=1)
    return timedelta(hours=1) if start <= utc < end else timedelta(0)

def to_london(utc):
    return (utc + london_offset(utc)).replace(tzinfo=None)

def service_date(utc):
    """Service day in London local time; rolls at 04:00."""
    return (to_london(utc) - timedelta(hours=4)).date()

def parse_iso(s):
    if not s:
        return None
    s = s.rstrip("Z")
    if "." in s:
        a, b = s.split(".", 1)
        s = a + "." + b[:6]
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except ValueError:
        return None

def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

# ---------- HTTP ----------
def tfl_get(path, params=None, timeout=40):
    q = dict(params or {})
    if APP_KEY:
        q["app_key"] = APP_KEY
    url = API + path + ("?" + urllib.parse.urlencode(q) if q else "")
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip", "User-Agent": "headway-desk-collector"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(2 + attempt * 3)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(1)

# ---------- S3 helpers ----------
def s3_get_json(key, default=None):
    try:
        body = s3().get_object(Bucket=BUCKET, Key=key)["Body"].read()
    except Exception as e:  # NoSuchKey or similar
        if "NoSuchKey" in str(e) or "Not Found" in str(e) or "404" in str(e):
            return default
        raise
    if key.endswith(".gz"):
        body = gzip.decompress(body)
    return json.loads(body)

def s3_put_json(key, obj):
    body = json.dumps(obj, separators=(",", ":")).encode()
    if key.endswith(".gz"):
        body = gzip.compress(body, 6)
    s3().put_object(Bucket=BUCKET, Key=key, Body=body)

def s3_list(prefix):
    keys, token = [], None
    while True:
        kw = {"Bucket": BUCKET, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        r = s3().list_objects_v2(**kw)
        keys += [o["Key"] for o in r.get("Contents", [])]
        if not r.get("IsTruncated"):
            return keys
        token = r["NextContinuationToken"]

# ---------- lines ----------
_LINES = {}
def get_lines(now):
    today = str(service_date(now))
    if _LINES.get("date") == today:
        return _LINES["lines"]
    meta = s3_get_json("meta/lines.json")
    if not meta or meta.get("date") != today:
        lines = sorted(x["id"] for x in tfl_get("/Line/Mode/bus"))
        meta = {"date": today, "lines": lines}
        s3_put_json("meta/lines.json", meta)
    _LINES.update(meta)
    return meta["lines"]

def wanted_routes(all_lines):
    if ROUTES.upper() == "ALL":
        return set(all_lines)
    want = {r.strip().lower() for r in ROUTES.split(",") if r.strip()}
    return {l for l in all_lines if l.lower() in want}

def fetch_predictions(routes, all_lines):
    if ROUTES.upper() == "ALL" or len(routes) > 60:
        preds = tfl_get("/Mode/bus/Arrivals", timeout=50)
    else:
        preds, ids = [], sorted(routes)
        for i in range(0, len(ids), 10):
            preds += tfl_get("/Line/%s/Arrivals" % ",".join(ids[i:i + 10]))
    return [p for p in preds if p.get("lineId") in routes]

# ---------- passage inference ----------
def pred_key(p):
    return "%s|%s|%s" % (p.get("vehicleId", ""), p.get("naptanId", ""), p.get("lineId", ""))

def infer(state, preds, now):
    """state: {key: [first_exp, exp, tts, seen, route, dir, naptan, name, platform, vehicle, trip, dest]}
    Returns (new_state, passages)."""
    now_ts = int(now.timestamp())
    current = {}
    for p in preds:
        exp = parse_iso(p.get("expectedArrival"))
        if exp is None or not p.get("vehicleId") or not p.get("naptanId"):
            continue
        k = pred_key(p)
        old = state.get(k)
        e = int(exp.timestamp())
        first = old[0] if old else e
        current[k] = [first, e, int(p.get("timeToStation") or 0), now_ts, p.get("lineId", ""),
                      p.get("direction", ""), p["naptanId"], p.get("stationName", ""),
                      p.get("platformName", ""), p["vehicleId"], str(p.get("tripId", "") or ""),
                      p.get("destinationName", "")]
    passages = []
    for k, v in state.items():
        if k in current:
            continue
        first, exp, tts, seen = v[0], v[1], v[2], v[3]
        if now_ts - seen > STALE_S:
            continue  # we were not watching; can't say
        # remaining time at last sighting, re-based to now
        remaining = exp - now_ts
        if tts <= ARRIVE_S or remaining <= ARRIVE_S:
            actual = min(exp, now_ts)
            actual = max(actual, seen - 60)
            passages.append({"route": v[4], "direction": v[5], "naptan": v[6], "stop_name": v[7],
                             "platform": v[8], "vehicle": v[9], "trip_id": v[10], "destination": v[11],
                             "first_expected": iso(datetime.fromtimestamp(first, timezone.utc)),
                             "expected": iso(datetime.fromtimestamp(exp, timezone.utc)),
                             "actual": iso(datetime.fromtimestamp(actual, timezone.utc)),
                             "actual_source": "inferred"})
        # else: prediction withdrawn far from stop (curtailed / diverted / lost) -> not a passage
    return current, passages

def rows_to_csv_gz(rows, fields):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return gzip.compress(buf.getvalue().encode(), 6)

# ---------- timetables ----------
def fetch_timetable(route, direction):
    seq = tfl_get("/Line/%s/Route/Sequence/%s" % (route, direction))
    olr = seq.get("orderedLineRoutes") or []
    stop_names = {}
    for sps in seq.get("stopPointSequences") or []:
        for sp in sps.get("stopPoint") or []:
            stop_names[sp.get("id")] = sp.get("name")
    origins = []
    for r in olr:
        n = r.get("naptanIds") or []
        if n and n[0] not in origins:
            origins.append(n[0])
    out = {"route": route, "direction": direction, "fetched": iso(datetime.now(timezone.utc)),
           "stop_names": stop_names, "stops": {}}
    # stops[naptan][daytype] = sorted minutes after service-day midnight (hour<4 => +24h)
    for origin in origins[:3]:  # main + up to 2 branch origins
        try:
            tt = tfl_get("/Line/%s/Timetable/%s" % (route, origin), {"direction": direction})
        except urllib.error.HTTPError:
            continue
        for rt in (tt.get("timetable") or {}).get("routes") or []:
            intervals = {}
            for si in rt.get("stationIntervals") or []:
                offs = {origin: 0.0}
                for iv in si.get("intervals") or []:
                    offs[iv["stopId"]] = float(iv.get("timeToArrival") or 0)
                intervals[int(si.get("id", 0))] = offs
            for sch in rt.get("schedules") or []:
                name = sch.get("name", "")
                for j in sch.get("knownJourneys") or []:
                    h, m = int(j["hour"]), int(j["minute"])
                    dep = h * 60 + m + (1440 if h < 4 else 0)
                    for stop, off in intervals.get(int(j.get("intervalId", 0)), {}).items():
                        out["stops"].setdefault(stop, {}).setdefault(name, set()).add(round(dep + off, 1))
    for stop in out["stops"]:
        for name in out["stops"][stop]:
            out["stops"][stop][name] = sorted(out["stops"][stop][name])
    return out

_TT_IDLE = {}
def refresh_timetables(routes, now, budget):
    hour = now.strftime("%Y%m%d%H")
    if _TT_IDLE.get("h") == hour:
        return 0, 0  # checked this hour in this warm container, nothing due
    idx = s3_get_json("meta/timetable_index.json", {})
    cutoff = now - timedelta(days=TT_MAX_AGE_D)
    todo = []
    for r in sorted(routes):
        for d in ("outbound", "inbound"):
            k = "%s/%s" % (r, d)
            t = parse_iso(idx.get(k))
            if t is None or t < cutoff:
                todo.append((t or datetime(2000, 1, 1, tzinfo=timezone.utc), r, d))
    todo.sort()
    done = 0
    for _, r, d in todo[:budget]:
        try:
            s3_put_json("timetables/%s/%s.json.gz" % (r, d), fetch_timetable(r, d))
            idx["%s/%s" % (r, d)] = iso(now)
            done += 1
        except Exception as e:
            print("timetable failed", r, d, repr(e)[:200])
            idx["%s/%s" % (r, d)] = iso(now - timedelta(days=TT_MAX_AGE_D) + timedelta(hours=6))  # retry in 6h
    if todo[:budget]:
        s3_put_json("meta/timetable_index.json", idx)
    else:
        _TT_IDLE["h"] = hour
    return done, max(0, len(todo) - budget)

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
def daytype_matches(name, weekday):
    n = name.lower()
    hits = set()
    for part in n.replace("&", " and ").split(" and "):
        part = part.strip()
        found = [i for i, d in enumerate(DAYS) if d in part]
        if " to " in part and len(found) >= 2:
            a, b = found[0], found[-1]
            hits |= set(range(a, b + 1)) if a <= b else set(range(a, 7)) | set(range(0, b + 1))
        else:
            hits |= set(found)
    if "weekday" in n:
        hits |= {0, 1, 2, 3, 4}
    if "weekend" in n:
        hits |= {5, 6}
    return weekday in hits

def schedule_for(tt, naptan, weekday):
    by = (tt or {}).get("stops", {}).get(naptan)
    if not by:
        return None
    for name, times in by.items():
        if daytype_matches(name, weekday):
            return times
    return None

def nearest(times, minute, window=60):
    if not times:
        return None
    import bisect
    i = bisect.bisect_left(times, minute)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(times) and (best is None or abs(times[j] - minute) < abs(best - minute)):
            best = times[j]
    return best if best is not None and abs(best - minute) <= window else None

# ---------- daily rollup ----------
EXPORT_FIELDS = ["date", "route", "direction", "stop_name", "naptan", "vehicle", "trip_id", "scheduled",
                 "expected_first", "expected", "actual", "actual_source", "delay_vs_scheduled_s", "dwell_estimate_s"]

def local_hms(utc_str):
    t = parse_iso(utc_str)
    return to_london(t).strftime("%H:%M:%S") if t else ""

def build_route_rows(day, rows, timetables):
    """rows: raw passage dicts for one route on service date `day` (date obj)."""
    wd = day.weekday()
    midnight_local = datetime(day.year, day.month, day.day)
    out = []
    for r in rows:
        a = parse_iso(r["actual"])
        loc = to_london(a)
        minute = (loc - midnight_local).total_seconds() / 60.0
        times = schedule_for(timetables.get(r["direction"]), r["naptan"], wd)
        s = nearest(times, minute)
        sched = ""
        delay = ""
        if s is not None:
            sched = (midnight_local + timedelta(minutes=s)).strftime("%H:%M")
            delay = str(int(round((minute - s) * 60)))
        out.append({"date": str(day), "route": r["route"], "direction": r["direction"],
                    "stop_name": r["stop_name"], "naptan": r["naptan"], "vehicle": r["vehicle"],
                    "trip_id": r["trip_id"], "scheduled": sched,
                    "expected_first": local_hms(r["first_expected"]), "expected": local_hms(r["expected"]),
                    "actual": local_hms(r["actual"]), "actual_source": r["actual_source"],
                    "delay_vs_scheduled_s": delay, "_t": a, "_m": minute, "_s": s})
    # dwell estimate: excess link time = (actual gap from previous stop) - (scheduled gap), same vehicle+trip
    out.sort(key=lambda x: (x["vehicle"], x["trip_id"], x["_t"]))
    for prev, cur in zip(out, out[1:]):
        if (prev["vehicle"], prev["trip_id"]) == (cur["vehicle"], cur["trip_id"]) and prev["_s"] is not None and cur["_s"] is not None:
            gap = (cur["_m"] - prev["_m"]) - (cur["_s"] - prev["_s"])
            if 0 < cur["_m"] - prev["_m"] < 30:
                cur["dwell_estimate_s"] = str(int(round(max(0, gap) * 60)))
    out.sort(key=lambda x: (x["direction"], x["naptan"], x["_t"]))
    return out

def rollup(day):
    """Two passes so memory stays small: raw files -> per-route temp files in /tmp -> per-route CSV."""
    import shutil, tempfile
    ds = str(day)
    keys = s3_list("raw/%s/" % ds)
    tmp = tempfile.mkdtemp(dir="/tmp" if os.path.isdir("/tmp") else None)
    handles = {}
    try:
        for k in keys:
            body = gzip.decompress(s3().get_object(Bucket=BUCKET, Key=k)["Body"].read()).decode()
            for row in csv.DictReader(io.StringIO(body)):
                rt = row["route"]
                h = handles.get(rt)
                if h is None:
                    h = handles[rt] = gzip.open(os.path.join(tmp, "%s.jsonl.gz" % rt.replace("/", "_")), "wt", 1)
                h.write(json.dumps(row, separators=(",", ":")) + "\n")
        for h in handles.values():
            h.close()
        n = 0
        for rt in handles:
            with gzip.open(os.path.join(tmp, "%s.jsonl.gz" % rt.replace("/", "_")), "rt") as f:
                rows = [json.loads(line) for line in f]
            tts = {d: s3_get_json("timetables/%s/%s.json.gz" % (rt, d)) for d in ("outbound", "inbound")}
            out = build_route_rows(day, rows, tts)
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(out)
            # stored gzipped; browsers / S3 console downloads decompress automatically (Content-Encoding)
            s3().put_object(Bucket=BUCKET, Key="exports/%s/%s.csv" % (ds, rt), Body=gzip.compress(buf.getvalue().encode(), 6),
                            ContentType="text/csv", ContentEncoding="gzip")
            n += len(out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(json.dumps({"rollup": ds, "routes": len(handles), "rows": n, "raw_files": len(keys)}))
    return {"date": ds, "routes": len(handles), "rows": n}

# ---------- collect ----------
def collect(now=None):
    t0 = time.time()
    now = now or datetime.now(timezone.utc)
    all_lines = get_lines(now)
    routes = wanted_routes(all_lines)
    state_obj = s3_get_json("state/inflight.json.gz", {"t": 0, "p": {}})
    if state_obj.get("t", 0) > now.timestamp():
        print("newer state exists; skipping overlapping run")
        return {"skipped": True}
    preds = fetch_predictions(routes, all_lines)
    new_state, passages = infer(state_obj.get("p", {}), preds, now)
    if passages:
        key = "raw/%s/%s.csv.gz" % (service_date(now), to_london(now).strftime("%H%M%S"))
        s3().put_object(Bucket=BUCKET, Key=key, Body=rows_to_csv_gz(passages, RAW_FIELDS))
    s3_put_json("state/inflight.json.gz", {"t": now.timestamp(), "p": new_state})
    tt_done, tt_left = refresh_timetables(routes, now, TT_PER_RUN) if TT_PER_RUN > 0 else (0, 0)
    res = {"routes": len(routes), "predictions": len(preds), "in_flight": len(new_state),
           "passages": len(passages), "timetables_fetched": tt_done, "timetables_pending": tt_left,
           "secs": round(time.time() - t0, 1)}
    print(json.dumps(res))
    return res

# ---------- function URL export ----------
def http_export(event):
    q = event.get("queryStringParameters") or {}
    if not EXPORT_TOKEN or q.get("token") != EXPORT_TOKEN:
        return {"statusCode": 403, "body": "forbidden"}
    ds = q.get("date") or str(service_date(datetime.now(timezone.utc)) - timedelta(days=1))
    route = q.get("route", "")
    if not route:
        keys = s3_list("exports/%s/" % ds)
        return {"statusCode": 200, "headers": {"Content-Type": "text/plain"},
                "body": "\n".join(k.split("/")[-1][:-4] for k in keys) or "no exports for " + ds}
    key = "exports/%s/%s.csv" % (ds, route)
    url = s3().generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": key,
                                      "ResponseContentDisposition": 'attachment; filename="%s_%s.csv"' % (route, ds)},
                                      ExpiresIn=900)
    return {"statusCode": 302, "headers": {"Location": url}, "body": ""}

def lambda_handler(event, context=None):
    event = event or {}
    if "requestContext" in event and "http" in event.get("requestContext", {}):
        return http_export(event)
    action = event.get("action", "collect")
    if action == "rollup":
        day = event.get("date")
        if day:
            day = datetime.strptime(day, "%Y-%m-%d").date()
        else:
            day = service_date(datetime.now(timezone.utc)) - timedelta(days=1)
        return rollup(day)
    return collect()

handler = lambda_handler
