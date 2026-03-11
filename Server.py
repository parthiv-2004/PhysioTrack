from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO
import sqlite3
from datetime import datetime
import time

app = Flask(__name__)
app.config['SECRET_KEY'] = 'physiotrack'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ── Default thresholds ────────────────────────────────────────
default_thresholds = {
    "dorsiflexion":   (105, 180),
    "plantarflexion": (0,   40),
    "inversion":      (55,  90),
    "eversion":       (105, 180)
}

# ── Session state ─────────────────────────────────────────────
current_session = {
    "session_active":   False,
    "movement_index":   0,
    "measuring":        False,   # True only while ESP should record for current movement
    "movements":        ["dorsiflexion","plantarflexion","inversion","eversion"],
    "thresholds":       dict(default_thresholds),
    "person_name":      "",
    "data": {
        "dorsiflexion":   [],
        "plantarflexion": [],
        "inversion":      [],
        "eversion":       []
    }
}

# ── Calibration state ─────────────────────────────────────────
calibration = {
    "phase":     "",
    "capturing": False,
    "peak":      0.0,
    "results":   {},
    "rest_dp":   90.0,   # resting angleDP (captured before movements)
    "rest_ie":   90.0    # resting angleIE
}

latest_feedback = "Waiting..."
last_esp_ping   = 0

# ── DB setup ─────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect('rehab.db')
    c = conn.cursor()
    # 18 columns after id = 18 value slots
    c.execute('''CREATE TABLE IF NOT EXISTS sessions (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        person_name      TEXT,
        avg_dorsi        REAL, avg_plantar REAL, avg_inv  REAL, avg_eve  REAL,
        max_dorsi        REAL, max_plantar REAL, max_inv  REAL, max_eve  REAL,
        thr_dorsi_low    REAL, thr_dorsi_high   REAL,
        thr_plantar_low  REAL, thr_plantar_high REAL,
        thr_inv_low      REAL, thr_inv_high     REAL,
        thr_eve_low      REAL, thr_eve_high     REAL,
        timestamp        TEXT
    )''')

    cols = [r[1] for r in c.execute("PRAGMA table_info(sessions)").fetchall()]
    migrations = [
        ("person_name",      "TEXT DEFAULT ''"),
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

# ── Pages ────────────────────────────────────────────────────
@app.route('/')
def home():
    return render_template("index.html")

@app.route('/data')
def data():
    conn = sqlite3.connect('rehab.db')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM sessions ORDER BY id DESC")
    raw_rows = c.fetchall()
    conn.close()
    rows = []
    for r in raw_rows:
        rows.append({
            "id":               r["id"],
            "person_name":      r["person_name"]      or "Unknown",
            "avg_dorsi":        r["avg_dorsi"]         or 0.0,
            "avg_plantar":      r["avg_plantar"]       or 0.0,
            "avg_inv":          r["avg_inv"]           or 0.0,
            "avg_eve":          r["avg_eve"]           or 0.0,
            "max_dorsi":        r["max_dorsi"]         or 0.0,
            "max_plantar":      r["max_plantar"]       or 0.0,
            "max_inv":          r["max_inv"]           or 0.0,
            "max_eve":          r["max_eve"]           or 0.0,
            "thr_dorsi_low":    r["thr_dorsi_low"]     or 0.0,
            "thr_dorsi_high":   r["thr_dorsi_high"]    or 0.0,
            "thr_plantar_low":  r["thr_plantar_low"]   or 0.0,
            "thr_plantar_high": r["thr_plantar_high"]  or 0.0,
            "thr_inv_low":      r["thr_inv_low"]       or 0.0,
            "thr_inv_high":     r["thr_inv_high"]      or 0.0,
            "thr_eve_low":      r["thr_eve_low"]       or 0.0,
            "thr_eve_high":     r["thr_eve_high"]      or 0.0,
            "rest_dp":          r["rest_dp"]            or 0.0,
            "rest_ie":          r["rest_ie"]            or 0.0,
            "timestamp":        r["timestamp"]          or "",
        })
    return render_template("data.html", rows=rows)

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

    # ── Calibration peak tracking ────────────────────────────
    if calibration["phase"]:
        mv    = calibration["phase"]

        if mv == "rest":
            # During rest capture, average incoming readings
            if calibration["capturing"]:
                calibration["rest_dp_sum"] = calibration.get("rest_dp_sum", 0) + angleDP
                calibration["rest_ie_sum"] = calibration.get("rest_ie_sum", 0) + angleIE
                calibration["rest_count"]  = calibration.get("rest_count",  0) + 1
            socketio.emit('cal_peak', {
                "movement":  "rest",
                "peak":      round(angleDP, 1),
                "current":   round(angleDP, 1),
                "capturing": calibration["capturing"]
            })
        else:
            angle = angleDP if mv in ["dorsiflexion","plantarflexion"] else angleIE
            if calibration["capturing"]:
                if mv in ["dorsiflexion", "eversion"]:
                    if angle > calibration["peak"]:
                        calibration["peak"] = angle
                else:
                    if calibration["peak"] == 180.0 or angle < calibration["peak"]:
                        calibration["peak"] = angle
            socketio.emit('cal_peak', {
                "movement":  mv,
                "peak":      round(calibration["peak"], 1),
                "current":   round(angle, 1),
                "capturing": calibration["capturing"]
            })

    # ── Session measurement ──────────────────────────────────
    if not current_session["session_active"] or not current_session["measuring"]:
        # Only push live angle display — never check thresholds here
        if current_session["session_active"]:
            idx = current_session["movement_index"]
            if idx < 4:
                movement = current_session["movements"][idx]
                angle    = angleDP if movement in ["dorsiflexion","plantarflexion"] else angleIE
                peak     = current_session["thresholds"][movement][0]
                socketio.emit('live_angle', {"movement": movement, "angle": round(angle, 1), "target": peak})
        return jsonify({"feedback": "waiting"})

    idx      = current_session["movement_index"]
    movement = current_session["movements"][idx]
    angle    = angleDP if movement in ["dorsiflexion","plantarflexion"] else angleIE

    low, high = current_session["thresholds"][movement]
    peak = low  # low == high == peak since we store peak directly
    print(f"[SERVER] measuring={movement} angle={angle:.1f} threshold={peak:.1f}")

    # Push live angle while measuring
    socketio.emit('live_angle', {
        "movement": movement,
        "angle":    round(angle, 1),
        "target":   peak
    })

    # dorsiflexion/eversion: must reach UP to >= peak
    # plantarflexion/inversion: must reach DOWN to <= peak
    if movement in ["dorsiflexion", "eversion"]:
        if angle < peak:
            latest_feedback = "Move More"
            socketio.emit('feedback', {"text": latest_feedback, "type": "warn"})
            return jsonify({"feedback": latest_feedback})
    else:
        if angle > peak:
            latest_feedback = "Move More"
            socketio.emit('feedback', {"text": latest_feedback, "type": "warn"})
            return jsonify({"feedback": latest_feedback})

    # Angle is in range — record, stop measuring, wait for "Start Measuring" press
    current_session["data"][movement].append(angle)
    current_session["measuring"] = False
    current_session["movement_index"] += 1

    if current_session["movement_index"] >= 4:
        latest_feedback = "Cycle Complete"
        socketio.emit('feedback', {"text": "Cycle Complete", "type": "complete"})
        print(f"[SERVER] Cycle complete")
    else:
        next_move = current_session["movements"][current_session["movement_index"]]
        msg = f"Good Job! Next: {next_move}"
        socketio.emit('feedback', {"text": msg, "type": "good"})
        print(f"[SERVER] Movement done — {msg}")
        # Reset latest_feedback immediately so the fallback poll
        # never re-fires this event on the next poll tick
        latest_feedback = "waiting"

    return jsonify({"feedback": "waiting"})

# ── Status fallback ──────────────────────────────────────────
@app.route('/status')
def status():
    return jsonify({
        "feedback": latest_feedback,
        "movement_index": current_session["movement_index"],
        "measuring": current_session["measuring"]
    })

# ── Calibration ──────────────────────────────────────────────
@app.route('/calibrate/start', methods=['POST'])
def calibrate_start():
    data     = request.json
    movement = data.get("movement", "")
    name     = data.get("name", "")
    current_session["person_name"] = name
    calibration["phase"]           = movement
    calibration["capturing"]       = True
    if movement == "rest":
        calibration["rest_dp_sum"] = 0
        calibration["rest_ie_sum"] = 0
        calibration["rest_count"]  = 0
    elif movement in ["plantarflexion", "inversion"]:
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
    print(f"[CALIBRATE] rest: dp={calibration['rest_dp']} ie={calibration['rest_ie']}")
    return jsonify({"rest_dp": calibration["rest_dp"], "rest_ie": calibration["rest_ie"]})

@app.route('/calibrate/capture', methods=['POST'])
def calibrate_capture():
    calibration["capturing"] = False
    movement = calibration["phase"]
    peak     = calibration["peak"]
    calibration["results"][movement] = peak

    # Store peak directly as the threshold target
    # low = high = peak, direction check handled in upload route
    current_session["thresholds"][movement] = (round(peak, 1), round(peak, 1))
    print(f"[CALIBRATE] {movement}: peak={peak:.1f}")
    return jsonify({"movement": movement, "peak": round(peak, 1)})

@app.route('/calibrate/done', methods=['POST'])
def calibrate_done():
    calibration["phase"]     = ""
    calibration["capturing"] = False
    return jsonify({"message": "Calibration complete"})

# ── Export CSV ───────────────────────────────────────────────
@app.route('/export_csv')
def export_csv():
    import csv, io
    conn = sqlite3.connect('rehab.db')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM sessions ORDER BY id ASC")
    rows = c.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Session", "Name",
        "Avg Dorsi (°)", "Avg Plantar (°)", "Avg Inv (°)", "Avg Eve (°)",
        "Max Dorsi (°)", "Max Plantar (°)", "Max Inv (°)", "Max Eve (°)",
        "Peak Dorsi (°)", "Peak Plantar (°)", "Peak Inv (°)", "Peak Eve (°)",
        "Resting Angle (°)", "Timestamp"
    ])
    for r in rows:
        writer.writerow([
            r["id"], r["person_name"] or "Unknown",
            round(r["avg_dorsi"]   or 0, 1), round(r["avg_plantar"] or 0, 1),
            round(r["avg_inv"]     or 0, 1), round(r["avg_eve"]     or 0, 1),
            round(r["max_dorsi"]   or 0, 1), round(r["max_plantar"] or 0, 1),
            round(r["max_inv"]     or 0, 1), round(r["max_eve"]     or 0, 1),
            round(r["thr_dorsi_low"]   or 0, 1), round(r["thr_plantar_low"] or 0, 1),
            round(r["thr_inv_low"]     or 0, 1), round(r["thr_eve_low"]     or 0, 1),
            round(r["rest_dp"]     or 0, 1),
            r["timestamp"] or ""
        ])

    from flask import Response
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=physiotrack_records.csv"}
    )

# ── Clear all records ────────────────────────────────────────
@app.route('/clear_records', methods=['POST'])
def clear_records():
    conn = sqlite3.connect('rehab.db')
    c = conn.cursor()
    c.execute("DELETE FROM sessions")
    c.execute("DELETE FROM sqlite_sequence WHERE name='sessions'")
    conn.commit()
    conn.close()
    return jsonify({"message": "All records cleared"})

# ── Session ──────────────────────────────────────────────────
@app.route('/start_session', methods=['POST'])
def start_session():
    global latest_feedback
    current_session["session_active"]  = True
    current_session["movement_index"]  = 0
    current_session["measuring"]       = False
    for key in current_session["data"]:
        current_session["data"][key] = []
    latest_feedback = "Ready"
    return jsonify({"message": latest_feedback})

@app.route('/start_measuring', methods=['POST'])
def start_measuring():
    """Called when user presses 'Start Measuring' for current movement."""
    global latest_feedback
    current_session["measuring"] = True
    idx  = current_session["movement_index"]
    move = current_session["movements"][idx]
    latest_feedback = f"Measuring {move}..."
    return jsonify({"feedback": latest_feedback, "movement": move})

@app.route('/continue', methods=['POST'])
def continue_cycle():
    global latest_feedback
    current_session["movement_index"] = 0
    current_session["measuring"]      = False
    latest_feedback = "Ready"
    return jsonify({"message": latest_feedback})

@app.route('/end_session', methods=['POST'])
def end_session():
    global latest_feedback

    def calc(values):
        if not values: return (0, 0)
        return (sum(values) / len(values), max(values))

    d = current_session["data"]
    avg_d, max_d = calc(d["dorsiflexion"])
    avg_p, max_p = calc(d["plantarflexion"])
    avg_i, max_i = calc(d["inversion"])
    avg_e, max_e = calc(d["eversion"])

    t = current_session["thresholds"]
    td_l, td_h = t["dorsiflexion"]
    tp_l, tp_h = t["plantarflexion"]
    ti_l, ti_h = t["inversion"]
    te_l, te_h = t["eversion"]

    conn = sqlite3.connect('rehab.db')
    c = conn.cursor()
    c.execute(
        """INSERT INTO sessions
           (person_name,
            avg_dorsi, avg_plantar, avg_inv, avg_eve,
            max_dorsi, max_plantar, max_inv, max_eve,
            thr_dorsi_low,   thr_dorsi_high,
            thr_plantar_low, thr_plantar_high,
            thr_inv_low,     thr_inv_high,
            thr_eve_low,     thr_eve_high,
            rest_dp, rest_ie,
            timestamp)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (current_session["person_name"],
         avg_d, avg_p, avg_i, avg_e,
         max_d, max_p, max_i, max_e,
         td_l, td_h,
         tp_l, tp_h,
         ti_l, ti_h,
         te_l, te_h,
         calibration.get("rest_dp", 0),
         calibration.get("rest_ie", 0),
         datetime.now())
    )
    conn.commit()
    conn.close()

    current_session["session_active"] = False
    current_session["measuring"]      = False
    current_session["thresholds"]     = dict(default_thresholds)
    calibration["results"]            = {}
    calibration["phase"]              = ""
    calibration["capturing"]          = False
    latest_feedback = "Thank you for using PhysioTrack"
    return jsonify({"message": latest_feedback})

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)
