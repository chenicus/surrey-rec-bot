"""
Surrey Rec Bot — Scheduler + Dashboard
Runs on Railway. Serves dashboard at your Railway URL.
"""

import asyncio
import os
import threading
from collections import deque
from datetime import datetime

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, jsonify, request

from bot import register   # uses Playwright (login requires JS execution)

PACIFIC = pytz.timezone("America/Vancouver")
PORT    = int(os.getenv("PORT", 8080))

BAD = "Drop In Badminton - Seniors Services"
TT  = "Drop In Table Tennis - Seniors Services"
GF  = "Guildford"
NW  = "Newton"

# In-memory log (last 300 entries, survives restarts in RAM only)
logs       = deque(maxlen=300)
# Tracks whether the priority (badminton) slot succeeded this week, for conflict logic
# keyed by e.g. "mon_bad_815"
conflicts  = {}

app = Flask(__name__)


# ─── Logging ───────────────────────────────────────────────────────────────────

def add_log(status: str, activity: str, location: str, note: str = ""):
    entry = {
        "time":     datetime.now(PACIFIC).strftime("%a %I:%M%p").lower(),
        "status":   status,   # "success" | "failed" | "skipped"
        "activity": activity,
        "location": location,
        "note":     note,
    }
    logs.appendleft(entry)
    icon = {"success": "✅", "failed": "❌", "skipped": "⏭"}.get(status, "•")
    print(f"{icon} [{entry['time']}] {activity} @ {location} — {status} {note}", flush=True)


# ─── Job runner ────────────────────────────────────────────────────────────────

def run_job(class_name: str, location: str,
            conflict_read: str | None = None,
            conflict_write: str | None = None):
    """
    Runs in a thread (APScheduler uses a thread pool).
    conflict_read:  if this key is True in `conflicts`, skip this job (priority already won)
    conflict_write: after running, store result under this key
    """
    short = "Badminton" if "Badminton" in class_name else "Table Tennis"

    # Skip if the priority badminton session already succeeded
    if conflict_read and conflicts.get(conflict_read) is True:
        add_log("skipped", short, location, f"(badminton secured — {conflict_read})")
        return

    try:
        success = asyncio.run(register(class_name, location))
    except Exception as e:
        add_log("failed", short, location, f"error: {e}")
        if conflict_write:
            conflicts[conflict_write] = False
        return

    if conflict_write:
        conflicts[conflict_write] = success

    add_log("success" if success else "failed", short, location)


# ─── Dashboard ─────────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Surrey Rec Bot</title>
<style>
  :root{--green:#3B6D11;--green-bg:#EAF3DE;--red:#A32D2D;--red-bg:#FCEBEB;
        --amber:#854F0B;--amber-bg:#FAEEDA;--gray:#5F5E5A;--gray-bg:#F1EFE8;
        --border:#e2e2e0;--text:#1a1a18;--muted:#6b6b67;--surface:#fafaf8}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       color:var(--text);background:var(--surface);padding:1.5rem;max-width:720px;margin:0 auto}
  h1{font-size:20px;font-weight:500;margin-bottom:4px}
  .sub{color:var(--muted);font-size:13px;margin-bottom:1.5rem}
  .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:1.5rem}
  .stat{background:#fff;border:0.5px solid var(--border);border-radius:8px;padding:12px 14px}
  .stat-label{font-size:11px;color:var(--muted);margin-bottom:4px}
  .stat-val{font-size:22px;font-weight:500}
  .green{color:var(--green)}.red{color:var(--red)}.amber{color:var(--amber)}.gray{color:var(--gray)}
  table{width:100%;border-collapse:collapse;font-size:13px;margin-bottom:1.5rem}
  thead th{text-align:left;padding:8px 10px;font-weight:500;font-size:11px;
           color:var(--muted);border-bottom:0.5px solid var(--border);text-transform:uppercase;letter-spacing:.05em}
  tbody td{padding:9px 10px;border-bottom:0.5px solid var(--border)}
  tbody tr:last-child td{border-bottom:none}
  .badge{display:inline-flex;align-items:center;font-size:11px;font-weight:500;
         padding:2px 8px;border-radius:4px;white-space:nowrap}
  .b-success{background:var(--green-bg);color:var(--green)}
  .b-failed{background:var(--red-bg);color:var(--red)}
  .b-skipped{background:var(--amber-bg);color:var(--amber)}
  .b-pending{background:var(--gray-bg);color:var(--gray)}
  .section-head{font-size:11px;font-weight:500;color:var(--muted);text-transform:uppercase;
                letter-spacing:.05em;padding:10px 10px 4px;background:var(--surface)}
  .log-box{background:#fff;border:0.5px solid var(--border);border-radius:8px;overflow:hidden}
  .log-title{font-size:13px;font-weight:500;padding:12px 14px;border-bottom:0.5px solid var(--border)}
  .log-entry{display:flex;gap:12px;padding:7px 14px;border-bottom:0.5px solid var(--border);font-size:12px}
  .log-entry:last-child{border-bottom:none}
  .log-time{color:var(--muted);min-width:80px;flex-shrink:0}
  .note{color:var(--muted);font-style:italic}
  @media(prefers-color-scheme:dark){
    body{background:#18181a;color:#f0f0ee}
    .stat,.log-box{background:#242428;border-color:#3a3a3e}
    .sub,.stat-label,.log-time,.section-head,.note{color:#888}
    thead th{color:#888;border-color:#3a3a3e}
    tbody td{border-color:#3a3a3e}
    .log-title{border-color:#3a3a3e}
    .log-entry{border-color:#3a3a3e}
  }
</style>
</head>
<body>
<h1>Surrey Rec — Registration Bot</h1>
<div class="sub" id="updated">Loading...</div>

<div class="stats">
  <div class="stat"><div class="stat-label">Registered this week</div>
    <div class="stat-val green" id="cnt-success">—</div></div>
  <div class="stat"><div class="stat-label">Skipped (priority)</div>
    <div class="stat-val amber" id="cnt-skipped">—</div></div>
  <div class="stat"><div class="stat-label">Failed / full</div>
    <div class="stat-val red" id="cnt-failed">—</div></div>
  <div class="stat"><div class="stat-label">Pending this week</div>
    <div class="stat-val gray" id="cnt-pending">—</div></div>
</div>

<table>
  <thead><tr>
    <th>Day</th><th>Session</th><th>Activity</th><th>Location</th><th>Bot runs</th><th>Status</th>
  </tr></thead>
  <tbody id="schedule-body"></tbody>
</table>

<div class="log-box">
  <div class="log-title">Recent activity</div>
  <div id="log-list"></div>
</div>

<div style="margin-top:1.5rem;padding:14px;background:#fff;border:0.5px solid var(--border);border-radius:8px">
  <div style="font-size:13px;font-weight:500;margin-bottom:10px">Run a test now</div>
  <div style="display:flex;gap:8px;flex-wrap:wrap">
    <button onclick="runTest('Drop In Table Tennis - Senior Services','Newton')"
      style="font-size:12px;padding:6px 14px;border-radius:6px;border:0.5px solid var(--border);
             background:var(--surface);cursor:pointer">
      🏓 Table Tennis — Newton SC
    </button>
    <button onclick="runTest('Drop In Table Tennis - Senior Services','Guildford')"
      style="font-size:12px;padding:6px 14px;border-radius:6px;border:0.5px solid var(--border);
             background:var(--surface);cursor:pointer">
      🏓 Table Tennis — Guildford
    </button>
    <button onclick="runTest('Drop In Badminton - Senior Services','Guildford')"
      style="font-size:12px;padding:6px 14px;border-radius:6px;border:0.5px solid var(--border);
             background:var(--surface);cursor:pointer">
      🏸 Badminton — Guildford
    </button>
  </div>
  <div id="test-status" style="margin-top:8px;font-size:12px;color:var(--muted)"></div>
</div>

<script>
const SCHEDULE = [
  {day:"Mon",time:"8:15–9:45am",  act:"Badminton",     loc:"Guildford", runs:"7:15am", key:"mon_bad_815"},
  {day:"Mon",time:"9:00–11:00am", act:"Table Tennis*",  loc:"Guildford", runs:"8:00am", key:"mon_tt_9",   cond:"mon_bad_815"},
  {day:"Mon",time:"10:00–11:30am",act:"Badminton",     loc:"Guildford", runs:"9:00am", key:"mon_bad_10"},
  {day:"Tue",time:"8:15–9:45am",  act:"Badminton",     loc:"Guildford", runs:"7:15am", key:"tue_bad_815"},
  {day:"Tue",time:"10:00–11:30am",act:"Badminton",     loc:"Guildford", runs:"9:00am", key:"tue_bad_10"},
  {day:"Tue",time:"1:00–3:15pm",  act:"Table Tennis",  loc:"Newton SC", runs:"12:00pm",key:"tue_tt_1"},
  {day:"Wed",time:"8:15–9:45am",  act:"Badminton",     loc:"Guildford", runs:"7:15am", key:"wed_bad_815"},
  {day:"Wed",time:"8:45–11:00am", act:"Table Tennis*",  loc:"Newton SC", runs:"7:45am", key:"wed_tt_845", cond:"wed_bad_815"},
  {day:"Wed",time:"10:00–11:30am",act:"Badminton",     loc:"Guildford", runs:"9:00am", key:"wed_bad_10"},
  {day:"Wed",time:"10:30am–12pm", act:"Badminton",     loc:"Newton RC", runs:"9:30am", key:"wed_bad_1030"},
  {day:"Wed",time:"2:30–4:30pm",  act:"Table Tennis",  loc:"Guildford", runs:"1:30pm", key:"wed_tt_230"},
  {day:"Thu",time:"8:15–9:45am",  act:"Badminton",     loc:"Guildford", runs:"7:15am", key:"thu_bad_815"},
  {day:"Thu",time:"8:45–11:00am", act:"Table Tennis*",  loc:"Newton SC", runs:"7:45am", key:"thu_tt_845", cond:"thu_bad_815"},
  {day:"Thu",time:"10:00–11:30am",act:"Badminton",     loc:"Guildford", runs:"9:00am", key:"thu_bad_10"},
  {day:"Thu",time:"3:00–4:30pm",  act:"Table Tennis",  loc:"Guildford", runs:"2:00pm", key:"thu_tt_3"},
  {day:"Fri",time:"8:15–9:45am",  act:"Badminton",     loc:"Guildford", runs:"7:15am", key:"fri_bad_815"},
  {day:"Fri",time:"10:00–11:30am",act:"Badminton",     loc:"Guildford", runs:"9:00am", key:"fri_bad_10"},
];

function badge(status){
  const map={success:"b-success",failed:"b-failed",skipped:"b-skipped",pending:"b-pending"};
  const labels={success:"Registered",failed:"Failed",skipped:"Skipped",pending:"Pending"};
  return `<span class="badge ${map[status]||'b-pending'}">${labels[status]||"Pending"}</span>`;
}

async function refresh(){
  const r = await fetch("/api/status");
  const {logs, conflicts, updated} = await r.json();

  document.getElementById("updated").textContent = "Last updated: " + updated;

  let counts = {success:0,failed:0,skipped:0,pending:0};
  const rows = SCHEDULE.map(s => {
    const logEntry = logs.find(l => l.key === s.key);
    const status = logEntry ? logEntry.status : "pending";
    counts[status] = (counts[status]||0) + 1;
    const condNote = s.cond ? ' <span class="note">if badminton fails</span>' : "";
    return `<tr>
      <td>${s.day}</td>
      <td>${s.time}</td>
      <td>${s.act}${condNote}</td>
      <td>${s.loc}</td>
      <td style="color:#888;font-size:12px">${s.runs}</td>
      <td>${badge(status)}</td>
    </tr>`;
  });
  document.getElementById("schedule-body").innerHTML = rows.join("");

  document.getElementById("cnt-success").textContent = counts.success||0;
  document.getElementById("cnt-skipped").textContent = counts.skipped||0;
  document.getElementById("cnt-failed").textContent  = counts.failed||0;
  document.getElementById("cnt-pending").textContent = counts.pending||0;

  const logHtml = logs.slice(0,50).map(l=>`
    <div class="log-entry">
      <span class="log-time">${l.time}</span>
      <span class="${{success:"green",failed:"red",skipped:"amber"}[l.status]||"gray"}">
        ${{success:"✅",failed:"❌",skipped:"⏭"}[l.status]||"•"}
        ${l.activity} @ ${l.location}
        ${l.note ? `<span class="note">— ${l.note}</span>` : ""}
      </span>
    </div>`).join("");
  document.getElementById("log-list").innerHTML = logHtml || '<div class="log-entry"><span class="log-time">—</span><span style="color:#888">No activity yet this session</span></div>';
}

refresh();
setInterval(refresh, 30_000);

async function runTest(className, location) {
  const el = document.getElementById("test-status");
  el.textContent = `⏳ Starting: ${className} @ ${location} — check Recent activity in ~30s...`;
  await fetch("/api/run", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({class_name: className, location: location})
  });
  setTimeout(refresh, 5000);
}
</script>
</body>
</html>"""


@app.route("/")
def dashboard():
    return DASHBOARD_HTML


@app.route("/api/status")
def status():
    now = datetime.now(PACIFIC).strftime("%a %b %d, %I:%M %p PT")
    return jsonify({
        "updated":   now,
        "conflicts": conflicts,
        "logs":      list(logs),
    })


@app.route("/api/run", methods=["POST"])
def run_now():
    """Manually trigger a registration job immediately."""
    data       = request.get_json(force=True)
    class_name = data.get("class_name", TT)
    location   = data.get("location",   GF)
    t = threading.Thread(target=run_job, args=[class_name, location], daemon=True)
    t.start()
    return jsonify({"started": True, "class": class_name, "location": location})


# ─── Schedule all 17 jobs ──────────────────────────────────────────────────────

def schedule_jobs(scheduler):
    tz = PACIFIC
    jobs = [
        # day_of_week: 0=Mon 1=Tue 2=Wed 3=Thu 4=Fri
        # Monday
        dict(dow=0, h=7,  m=15, cls=BAD, loc=GF, cw="mon_bad_815"),
        dict(dow=0, h=8,  m=0,  cls=TT,  loc=GF, cr="mon_bad_815"),
        dict(dow=0, h=9,  m=0,  cls=BAD, loc=GF),
        # Tuesday
        dict(dow=1, h=7,  m=15, cls=BAD, loc=GF),
        dict(dow=1, h=9,  m=0,  cls=BAD, loc=GF),
        dict(dow=1, h=12, m=0,  cls=TT,  loc=NW),
        # Wednesday
        dict(dow=2, h=7,  m=15, cls=BAD, loc=GF, cw="wed_bad_815"),
        dict(dow=2, h=7,  m=45, cls=TT,  loc=NW, cr="wed_bad_815"),
        dict(dow=2, h=9,  m=0,  cls=BAD, loc=GF),
        dict(dow=2, h=9,  m=30, cls=BAD, loc=NW),
        dict(dow=2, h=13, m=30, cls=TT,  loc=GF),
        # Thursday
        dict(dow=3, h=7,  m=15, cls=BAD, loc=GF, cw="thu_bad_815"),
        dict(dow=3, h=7,  m=45, cls=TT,  loc=NW, cr="thu_bad_815"),
        dict(dow=3, h=9,  m=0,  cls=BAD, loc=GF),
        dict(dow=3, h=14, m=0,  cls=TT,  loc=GF),
        # Friday
        dict(dow=4, h=7,  m=15, cls=BAD, loc=GF),
        dict(dow=4, h=9,  m=0,  cls=BAD, loc=GF),
    ]

    for j in jobs:
        scheduler.add_job(
            run_job,
            CronTrigger(day_of_week=j["dow"], hour=j["h"], minute=j["m"], timezone=tz),
            args=[j["cls"], j["loc"], j.get("cr"), j.get("cw")],
            misfire_grace_time=30,   # if delayed >30s, skip rather than pile up
        )

    print(f"✅ Scheduled {len(jobs)} registration jobs (Pacific time)", flush=True)


# ─── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    scheduler = BackgroundScheduler(timezone=PACIFIC)
    schedule_jobs(scheduler)
    scheduler.start()
    print(f"🚀 Dashboard running on port {PORT}", flush=True)
    app.run(host="0.0.0.0", port=PORT)
