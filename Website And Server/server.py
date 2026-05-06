from flask import Flask, request, jsonify, render_template, Response
from flask_socketio import SocketIO
import sqlite3, csv, io, json
from collections import OrderedDict
from datetime import datetime
import time

app = Flask(__name__)
app.config['SECRET_KEY'] = 'physiotrack'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

MEASURE_SECONDS = 15

# ── Direction logic per leg ───────────────────────────────────
# Movements that go DOWN (angle decreases from 90 toward 0)
def down_movements(leg):
    if leg == "right":
        return ["plantarflexion", "inversion"]
    else:  # left leg — inversion/eversion axes are swapped
        return ["plantarflexion", "eversion"]

def up_movements(leg):
    if leg == "right":
        return ["dorsiflexion", "eversion"]
    else:
        return ["dorsiflexion", "inversion"]

def is_down(movement, leg):
    return movement in down_movements(leg)

# ── Calibration hints per movement per leg ────────────────────
CAL_HINTS = {
    "right": {
        "rest":          "Sit still with foot flat — captures neutral angle",
        "dorsiflexion":  "Pull toes upward toward shin",
        "plantarflexion":"Point toes downward",
        "inversion":     "Turn sole inward (toward centre)",
        "eversion":      "Turn sole outward (away from centre)",
    },
    "left": {
        "rest":          "Sit still with foot flat — captures neutral angle",
        "dorsiflexion":  "Pull toes upward toward shin",
        "plantarflexion":"Point toes downward",
        "inversion":     "Turn sole inward (toward centre)",
        "eversion":      "Turn sole outward (away from centre)",
    }
}

SESSION_HINTS = {
    "right": {
        "dorsiflexion":  "Pull toes UP toward shin ⬆️",
        "plantarflexion":"Point toes DOWN ⬇️",
        "inversion":     "Turn sole INWARD ↙️",
        "eversion":      "Turn sole OUTWARD ↘️",
    },
    "left": {
        "dorsiflexion":  "Pull toes UP toward shin ⬆️",
        "plantarflexion":"Point toes DOWN ⬇️",
        "inversion":     "Turn sole INWARD ↙️",
        "eversion":      "Turn sole OUTWARD ↘️",
    }
}

default_thresholds = {
    "dorsiflexion":   (140, 140),
    "plantarflexion": (40,  40),
    "inversion":      (40,  40),
    "eversion":       (140, 140)
}

# ── Session state ─────────────────────────────────────────────
current_session = {
    "session_active":   False,
    "movement_index":   0,
    "measuring":        False,
    "measure_start":    0,
    "peak_this_window": 0.0,
    "movements":        ["dorsiflexion","plantarflexion","inversion","eversion"],
    "thresholds":       dict(default_thresholds),
    "person_name":      "",
    "leg":              "right",
    "data": {
        "dorsiflexion":   [],
        "plantarflexion": [],
        "inversion":      [],
        "eversion":       []
    }
}

calibration = {
    "phase":     "",
    "capturing": False,
    "peak":      0.0,
    "results":   {},
    "rest_dp":   90.0,
    "rest_ie":   90.0
}

latest_feedback = "Waiting..."
last_esp_ping   = 0

# ── DB setup ─────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect('rehab.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sessions (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        person_name      TEXT,
        leg              TEXT DEFAULT 'right',
        avg_dorsi        REAL, avg_plantar REAL, avg_inv  REAL, avg_eve  REAL,
        max_dorsi        REAL, max_plantar REAL, max_inv  REAL, max_eve  REAL,
        thr_dorsi_low    REAL, thr_dorsi_high   REAL,
        thr_plantar_low  REAL, thr_plantar_high REAL,
        thr_inv_low      REAL, thr_inv_high     REAL,
        thr_eve_low      REAL, thr_eve_high     REAL,
        rest_dp          REAL, rest_ie          REAL,
        timestamp        TEXT
    )''')
    cols = [r[1] for r in c.execute("PRAGMA table_info(sessions)").fetchall()]
    migrations = [
        ("person_name",      "TEXT DEFAULT ''"),
        ("leg",              "TEXT DEFAULT 'right'"),
        ("thr_dorsi_low",    "REAL DEFAULT 0"),
        ("thr_dorsi_high",   "REAL DEFAULT 0"),
        ("thr_plantar_low",  "REAL DEFAULT 0"),
        ("thr_plantar_high", "REAL DEFAULT 0"),
        ("thr_inv_low",      "REAL DEFAULT 0"),
        ("thr_inv_high",     "REAL DEFAULT 0"),
        ("thr_eve_low",      "REAL DEFAULT 0"),
        ("thr_eve_high",     "REAL DEFAULT 0"),
        ("rest_dp",          "REAL DEFAULT 0"),
        ("rest_ie",          "REAL DEFAULT 0"),
    ]
    for col, definition in migrations:
        if col not in cols:
            c.execute(f"ALTER TABLE sessions ADD COLUMN {col} {definition}")
    conn.commit()
    conn.close()

init_db()

def get_conn():
    conn = sqlite3.connect('rehab.db')
    conn.row_factory = sqlite3.Row
    return conn

def normalise_row(r):
    return {
        "id":               r["id"],
        "person_name":      r["person_name"]      or "Unknown",
        "leg":              r["leg"]               or "right",
        "avg_dorsi":        r["avg_dorsi"]         or 0.0,
        "avg_plantar":      r["avg_plantar"]       or 0.0,
        "avg_inv":          r["avg_inv"]           or 0.0,
        "avg_eve":          r["avg_eve"]           or 0.0,
        "max_dorsi":        r["max_dorsi"]         or 0.0,
        "max_plantar":      r["max_plantar"]       or 0.0,
        "max_inv":          r["max_inv"]           or 0.0,
        "max_eve":          r["max_eve"]           or 0.0,
        "thr_dorsi_low":    r["thr_dorsi_low"]     or 0.0,
        "thr_plantar_low":  r["thr_plantar_low"]   or 0.0,
        "thr_inv_low":      r["thr_inv_low"]       or 0.0,
        "thr_eve_low":      r["thr_eve_low"]       or 0.0,
        "rest_dp":          r["rest_dp"]            or 0.0,
        "timestamp":        r["timestamp"]          or "",
    }

# ── Pages ────────────────────────────────────────────────────
@app.route('/')
def home():
    return render_template("index.html")

@app.route('/data')
def data():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM sessions ORDER BY id DESC")
    raw_rows = c.fetchall()
    conn.close()
    rows = [normalise_row(r) for r in raw_rows]

    # Group by patient → then by leg
    # Structure: { patient_name: { "left": [...], "right": [...] } }
    patient_legs = OrderedDict()
    for r in reversed(rows):
        name = r["person_name"]
        leg  = r["leg"]
        if name not in patient_legs:
            patient_legs[name] = OrderedDict()
        if leg not in patient_legs[name]:
            patient_legs[name][leg] = []
        patient_legs[name][leg].append(r)

    # Graph data: { "name__leg": { labels, dorsi, plantar, inv, eve } }
    graphs = {}
    for name, legs in patient_legs.items():
        for leg, sessions in legs.items():
            if len(sessions) < 2:
                continue
            key = f"{name}__{leg}"
            graphs[key] = {
                "labels":  [s["timestamp"][:10] for s in sessions],
                "dorsi":   [round(s["avg_dorsi"],   1) for s in sessions],
                "plantar": [round(s["avg_plantar"],  1) for s in sessions],
                "inv":     [round(s["avg_inv"],      1) for s in sessions],
                "eve":     [round(s["avg_eve"],      1) for s in sessions],
            }

    return render_template("data.html", rows=rows,
                           graphs=json.dumps(graphs),
                           graphs_dict=graphs,
                           patient_legs=patient_legs)

@app.route('/graph/<path:name_leg>')
def graph_page(name_leg):
    # name_leg format: "PatientName__left" or "PatientName__right"
    if "__" in name_leg:
        parts = name_leg.rsplit("__", 1)
        name, leg = parts[0], parts[1]
    else:
        name, leg = name_leg, "right"

    conn = get_conn()
    c = conn.cursor()
    c.execute("""SELECT * FROM sessions
                 WHERE LOWER(person_name)=LOWER(?) AND leg=?
                 ORDER BY id ASC""", (name, leg))
    raw_rows = c.fetchall()
    conn.close()
    if not raw_rows:
        return "No data found", 404
    sessions = [normalise_row(r) for r in raw_rows]
    graph = {
        "labels":  [s["timestamp"][:10] for s in sessions],
        "dorsi":   [round(s["avg_dorsi"],   1) for s in sessions],
        "plantar": [round(s["avg_plantar"],  1) for s in sessions],
        "inv":     [round(s["avg_inv"],      1) for s in sessions],
        "eve":     [round(s["avg_eve"],      1) for s in sessions],
    }
    return render_template("graph.html",
                           name=sessions[0]["person_name"],
                           leg=leg,
                           graph=json.dumps(graph))

# ── User lookup ──────────────────────────────────────────────
@app.route('/check_user', methods=['POST'])
def check_user():
    name = request.json.get("name", "").strip()
    leg  = request.json.get("leg",  "right").strip()
    conn = get_conn()
    c = conn.cursor()
    c.execute("""SELECT COUNT(*) as cnt FROM sessions
                 WHERE LOWER(person_name)=LOWER(?) AND leg=?""", (name, leg))
    cnt = c.fetchone()["cnt"]
    conn.close()
    return jsonify({"exists": cnt > 0, "name": name, "leg": leg})

@app.route('/load_user_thresholds', methods=['POST'])
def load_user_thresholds():
    name = request.json.get("name", "").strip()
    leg  = request.json.get("leg",  "right").strip()
    conn = get_conn()
    c = conn.cursor()
    c.execute("""SELECT * FROM sessions WHERE LOWER(person_name)=LOWER(?) AND leg=?
                 ORDER BY id DESC LIMIT 1""", (name, leg))
    r = c.fetchone()
    conn.close()
    if not r:
        return jsonify({"error": "Not found"}), 404

    current_session["person_name"] = r["person_name"]
    current_session["leg"]         = leg
    current_session["thresholds"]  = {
        "dorsiflexion":   (r["thr_dorsi_low"]   or 140, r["thr_dorsi_low"]   or 140),
        "plantarflexion": (r["thr_plantar_low"]  or 40,  r["thr_plantar_low"]  or 40),
        "inversion":      (r["thr_inv_low"]      or 40,  r["thr_inv_low"]      or 40),
        "eversion":       (r["thr_eve_low"]      or 140, r["thr_eve_low"]      or 140),
    }
    calibration["rest_dp"] = r["rest_dp"] or 90.0
    calibration["rest_ie"] = r["rest_ie"] or 90.0

    return jsonify({
        "name": r["person_name"], "leg": leg,
        "peaks": {
            "dorsiflexion":   r["thr_dorsi_low"]  or 0,
            "plantarflexion": r["thr_plantar_low"] or 0,
            "inversion":      r["thr_inv_low"]     or 0,
            "eversion":       r["thr_eve_low"]     or 0,
        },
        "rest_dp": r["rest_dp"] or 0
    })

@app.route('/get_hints', methods=['POST'])
def get_hints():
    leg = request.json.get("leg", "right")
    return jsonify({
        "cal_hints":     CAL_HINTS[leg],
        "session_hints": SESSION_HINTS[leg]
    })

# ── ESP heartbeat ────────────────────────────────────────────
@app.route('/esp_heartbeat', methods=['POST'])
def esp_heartbeat():
    global last_esp_ping
    last_esp_ping = time.time()
    return jsonify({"status": "ok"})

@app.route('/esp_status')
def esp_status():
    connected = (time.time() - last_esp_ping) < 5
    return jsonify({"connected": connected})

# ── Upload from ESP ──────────────────────────────────────────
@app.route('/upload', methods=['POST'])
def upload():
    global latest_feedback, last_esp_ping
    last_esp_ping = time.time()
    angleDP = request.json['angleDP']
    angleIE = request.json['angleIE']
    leg     = current_session.get("leg", "right")

    # Calibration
    if calibration["phase"]:
        mv = calibration["phase"]
        if mv == "rest":
            if calibration["capturing"]:
                calibration["rest_dp_sum"] = calibration.get("rest_dp_sum", 0) + angleDP
                calibration["rest_ie_sum"] = calibration.get("rest_ie_sum", 0) + angleIE
                calibration["rest_count"]  = calibration.get("rest_count",  0) + 1
            socketio.emit('cal_peak', {"movement":"rest","peak":round(angleDP,1),
                                       "current":round(angleDP,1),"capturing":calibration["capturing"]})
        else:
            angle = angleDP if mv in ["dorsiflexion","plantarflexion"] else angleIE
            if calibration["capturing"]:
                if is_down(mv, leg):
                    if calibration["peak"] == 180.0 or angle < calibration["peak"]:
                        calibration["peak"] = angle
                else:
                    if angle > calibration["peak"]:
                        calibration["peak"] = angle
            socketio.emit('cal_peak', {"movement":mv,"peak":round(calibration["peak"],1),
                                       "current":round(angle,1),"capturing":calibration["capturing"]})

    # Session
    if not current_session["session_active"] or not current_session["measuring"]:
        if current_session["session_active"]:
            idx = current_session["movement_index"]
            if idx < 4:
                mv    = current_session["movements"][idx]
                angle = angleDP if mv in ["dorsiflexion","plantarflexion"] else angleIE
                peak  = current_session["thresholds"][mv][0]
                socketio.emit('live_angle', {"movement":mv,"angle":round(angle,1),"target":peak})
        return jsonify({"feedback": "waiting"})

    idx      = current_session["movement_index"]
    movement = current_session["movements"][idx]
    angle    = angleDP if movement in ["dorsiflexion","plantarflexion"] else angleIE

    if is_down(movement, leg):
        if angle < current_session["peak_this_window"]:
            current_session["peak_this_window"] = angle
    else:
        if angle > current_session["peak_this_window"]:
            current_session["peak_this_window"] = angle

    peak      = current_session["thresholds"][movement][0]
    elapsed   = time.time() - current_session["measure_start"]
    remaining = max(0, MEASURE_SECONDS - elapsed)

    socketio.emit('live_angle', {
        "movement":  movement,
        "angle":     round(angle, 1),
        "target":    peak,
        "best":      round(current_session["peak_this_window"], 1),
        "remaining": round(remaining, 1)
    })

    if elapsed >= MEASURE_SECONDS:
        best = round(max(0.0, min(180.0, current_session["peak_this_window"])), 1)
        current_session["data"][movement].append(best)
        current_session["measuring"]        = False
        current_session["peak_this_window"] = 0.0
        current_session["movement_index"]  += 1

        if current_session["movement_index"] >= 4:
            latest_feedback = "Cycle Complete"
            socketio.emit('feedback', {"text": "Cycle Complete", "type": "complete"})
        else:
            next_move = current_session["movements"][current_session["movement_index"]]
            socketio.emit('feedback', {"text": f"Good Job! Next: {next_move}", "type": "good"})
            latest_feedback = "waiting"
        return jsonify({"feedback": "waiting"})

    return jsonify({"feedback": "waiting"})

# ── Status ───────────────────────────────────────────────────
@app.route('/status')
def status():
    return jsonify({"feedback": latest_feedback,
                    "movement_index": current_session["movement_index"],
                    "measuring": current_session["measuring"]})

# ── Calibration ──────────────────────────────────────────────
@app.route('/calibrate/start', methods=['POST'])
def calibrate_start():
    data     = request.json
    movement = data.get("movement", "")
    name     = data.get("name", "")
    leg      = data.get("leg", "right")
    current_session["person_name"] = name
    current_session["leg"]         = leg
    calibration["phase"]           = movement
    calibration["capturing"]       = True
    if movement == "rest":
        calibration["rest_dp_sum"] = 0
        calibration["rest_ie_sum"] = 0
        calibration["rest_count"]  = 0
    elif is_down(movement, leg):
        calibration["peak"] = 180.0
    else:
        calibration["peak"] = 0.0
    return jsonify({"message": f"Capturing {movement}"})

@app.route('/calibrate/capture_rest', methods=['POST'])
def calibrate_capture_rest():
    calibration["capturing"] = False
    n = calibration.get("rest_count", 1) or 1
    calibration["rest_dp"] = round(calibration.get("rest_dp_sum", 0) / n, 1)
    calibration["rest_ie"] = round(calibration.get("rest_ie_sum", 0) / n, 1)
    calibration["phase"]   = ""
    return jsonify({"rest_dp": calibration["rest_dp"], "rest_ie": calibration["rest_ie"]})

@app.route('/calibrate/capture', methods=['POST'])
def calibrate_capture():
    calibration["capturing"] = False
    movement = calibration["phase"]
    peak     = calibration["peak"]
    calibration["results"][movement] = peak
    current_session["thresholds"][movement] = (round(peak, 1), round(peak, 1))
    return jsonify({"movement": movement, "peak": round(peak, 1)})

@app.route('/calibrate/done', methods=['POST'])
def calibrate_done():
    calibration["phase"]     = ""
    calibration["capturing"] = False
    return jsonify({"message": "Calibration complete"})

# ── Export CSV ───────────────────────────────────────────────
@app.route('/export_csv')
def export_csv():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM sessions ORDER BY id ASC")
    rows = c.fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Session","Name","Leg",
        "Avg Dorsi","Avg Plantar","Avg Inv","Avg Eve",
        "Max Dorsi","Max Plantar","Max Inv","Max Eve",
        "Peak Dorsi","Peak Plantar","Peak Inv","Peak Eve",
        "Rest Angle","Timestamp"])
    for r in rows:
        writer.writerow([r["id"], r["person_name"] or "Unknown", r["leg"] or "right",
            round(r["avg_dorsi"] or 0,1),   round(r["avg_plantar"] or 0,1),
            round(r["avg_inv"] or 0,1),     round(r["avg_eve"] or 0,1),
            round(r["max_dorsi"] or 0,1),   round(r["max_plantar"] or 0,1),
            round(r["max_inv"] or 0,1),     round(r["max_eve"] or 0,1),
            round(r["thr_dorsi_low"] or 0,1), round(r["thr_plantar_low"] or 0,1),
            round(r["thr_inv_low"] or 0,1),   round(r["thr_eve_low"] or 0,1),
            round(r["rest_dp"] or 0,1), r["timestamp"] or ""])
    return Response(output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=physiotrack_records.csv"})

# ── Clear records ────────────────────────────────────────────
@app.route('/clear_records', methods=['POST'])
def clear_records():
    conn = sqlite3.connect('rehab.db')
    c = conn.cursor()
    c.execute("DELETE FROM sessions")
    c.execute("DELETE FROM sqlite_sequence WHERE name='sessions'")
    conn.commit()
    conn.close()
    return jsonify({"message": "All records cleared"})

# ── Session routes ───────────────────────────────────────────
@app.route('/start_session', methods=['POST'])
def start_session():
    global latest_feedback
    current_session["session_active"]   = True
    current_session["movement_index"]   = 0
    current_session["measuring"]        = False
    current_session["peak_this_window"] = 0.0
    for key in current_session["data"]:
        current_session["data"][key] = []
    latest_feedback = "Ready"
    return jsonify({"message": latest_feedback})

@app.route('/start_measuring', methods=['POST'])
def start_measuring():
    global latest_feedback
    leg  = current_session.get("leg", "right")
    current_session["measuring"]        = True
    current_session["measure_start"]    = time.time()
    idx  = current_session["movement_index"]
    move = current_session["movements"][idx]
    current_session["peak_this_window"] = 180.0 if is_down(move, leg) else 0.0
    latest_feedback = f"Measuring {move}..."
    return jsonify({"feedback": latest_feedback, "movement": move,
                    "duration": MEASURE_SECONDS,
                    "hint": SESSION_HINTS[leg][move]})

@app.route('/continue', methods=['POST'])
def continue_cycle():
    global latest_feedback
    current_session["movement_index"]   = 0
    current_session["measuring"]        = False
    current_session["peak_this_window"] = 0.0
    latest_feedback = "Ready"
    return jsonify({"message": latest_feedback})

@app.route('/end_session', methods=['POST'])
def end_session():
    global latest_feedback

    def calc(values):
        if not values: return (0, 0)
        return (sum(values)/len(values), max(values))

    d = current_session["data"]
    avg_d, max_d = calc(d["dorsiflexion"])
    avg_p, max_p = calc(d["plantarflexion"])
    avg_i, max_i = calc(d["inversion"])
    avg_e, max_e = calc(d["eversion"])

    t = current_session["thresholds"]
    td_l, _ = t["dorsiflexion"]
    tp_l, _ = t["plantarflexion"]
    ti_l, _ = t["inversion"]
    te_l, _ = t["eversion"]

    conn = sqlite3.connect('rehab.db')
    c = conn.cursor()
    c.execute("""INSERT INTO sessions
       (person_name, leg,
        avg_dorsi, avg_plantar, avg_inv, avg_eve,
        max_dorsi, max_plantar, max_inv, max_eve,
        thr_dorsi_low, thr_dorsi_high, thr_plantar_low, thr_plantar_high,
        thr_inv_low, thr_inv_high, thr_eve_low, thr_eve_high,
        rest_dp, rest_ie, timestamp)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (current_session["person_name"], current_session.get("leg","right"),
         avg_d, avg_p, avg_i, avg_e,
         max_d, max_p, max_i, max_e,
         td_l, td_l, tp_l, tp_l, ti_l, ti_l, te_l, te_l,
         calibration.get("rest_dp", 0), calibration.get("rest_ie", 0),
         datetime.now()))
    conn.commit()
    conn.close()

    current_session["session_active"]   = False
    current_session["measuring"]        = False
    current_session["peak_this_window"] = 0.0
    current_session["thresholds"]       = dict(default_thresholds)
    calibration["results"]              = {}
    calibration["phase"]                = ""
    calibration["capturing"]            = False
    latest_feedback = "Thank you for using PhysioTrack"
    return jsonify({"message": latest_feedback})

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)
