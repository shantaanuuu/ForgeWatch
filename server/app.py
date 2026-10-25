import os, sqlite3, time, threading, csv, io
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
import numpy as np
from sklearn.linear_model import Ridge

BASE = Path(__file__).resolve().parent
DB = BASE / 'forgewatch.db'
API_KEY = os.environ.get('FORGEWATCH_KEY', 'demo-change-me')
lock = threading.RLock()

# Registered worker directory (RFID UID -> Personnel Profile)
WORKERS = {
    'F35B6EE2': {
        'name': 'Dhruv Jagare',
        'role': 'Lead Metallurgy Tech',
        'badge': 'FW-7041',
        'initials': 'RS',
        'assigned_zone': 1
    },
    '7D69E13F': {
        'name': 'Shantanu Marathe',
        'role': 'Precision Machinist',
        'badge': 'FW-8922',
        'initials': 'PD',
        'assigned_zone': 2
    }
}

state = {
    'counts': [0, 0],
    'sensors': [{'temp': None, 'hum': None, 'ldr': None}, {'temp': None, 'hum': None, 'ldr': None}],
    'seen': [0, 0],
    'base_capacity': 10,       # Base design capacity (scaled between 5 - 10)
    'capacity': 10,           # Backwards-compatible field
    'emergency': False,
    'last_exit2_id': 'init',
    'last_events': []
}

def dbconn():
    c = sqlite3.connect(DB, check_same_thread=False)
    c.execute('PRAGMA journal_mode=WAL')
    return c

def init_db():
    with dbconn() as c:
        c.execute('''
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL,
                zone INTEGER,
                kind TEXT,
                count INTEGER,
                event_id TEXT UNIQUE,
                worker_id TEXT,
                worker_name TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL,
                zone INTEGER,
                temp REAL,
                hum REAL,
                ldr INTEGER,
                count INTEGER,
                heat_index REAL,
                heat_risk TEXT
            )
        ''')
        c.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)')
        
        # Schema migration
        for col, col_type in (('worker_id', 'TEXT'), ('worker_name', 'TEXT')):
            try: c.execute(f'ALTER TABLE events ADD COLUMN {col} {col_type}')
            except Exception: pass
        for col, col_type in (('heat_index', 'REAL'), ('heat_risk', 'TEXT')):
            try: c.execute(f'ALTER TABLE readings ADD COLUMN {col} {col_type}')
            except Exception: pass

        # Restore zone counts
        for z in (1, 2):
            row = c.execute('SELECT value FROM meta WHERE key=?', (f'count{z}',)).fetchone()
            if row:
                state['counts'][z-1] = max(0, int(row[0]))
        
        # Restore base capacity
        cap_row = c.execute('SELECT value FROM meta WHERE key="base_capacity"').fetchone()
        if cap_row:
            state['base_capacity'] = max(5, min(10, int(cap_row[0])))
            state['capacity'] = state['base_capacity']

        # Restore last events safely
        try:
            rows = c.execute('SELECT ts, zone, kind, count, worker_name, worker_id FROM events ORDER BY id DESC LIMIT 40').fetchall()
            state['last_events'] = [
                {'ts': r[0], 'zone': r[1], 'kind': r[2], 'count': r[3], 'worker_name': r[4] or 'Factory Personnel', 'worker_id': r[5] or '—'}
                for r in rows
            ]
        except Exception:
            rows = c.execute('SELECT ts, zone, kind, count FROM events ORDER BY id DESC LIMIT 40').fetchall()
            state['last_events'] = [
                {'ts': r[0], 'zone': r[1], 'kind': r[2], 'count': r[3], 'worker_name': 'Factory Personnel', 'worker_id': '—'}
                for r in rows
            ]

init_db()

# --- AI & ENVIRONMENTAL ERGONOMICS ALGORITHMS ---

def calculate_heat_index(temp_c, hum):
    """Calculates OSHA / NOAA Rothfusz regression Heat Index in Celsius."""
    if temp_c is None or hum is None:
        return None, 'NO DATA', 'NORMAL', 'Awaiting telemetry'
    
    t_f = temp_c * 9.0 / 5.0 + 32.0
    r = hum
    
    hi_simple = 0.5 * (t_f + 61.0 + ((t_f - 68.0) * 1.2) + (r * 0.094))
    if (hi_simple + t_f) / 2.0 >= 80.0:
        hi = (-42.379 + 2.04901523 * t_f + 10.14333127 * r
              - 0.22475541 * t_f * r - 0.00683783 * (t_f ** 2)
              - 0.05481717 * (r ** 2) + 0.00122874 * (t_f ** 2) * r
              + 0.00085282 * t_f * (r ** 2) - 0.00000199 * (t_f ** 2) * (r ** 2))
    else:
        hi = hi_simple
    
    hi_c = round((hi - 32.0) * 5.0 / 9.0, 1)

    if hi_c < 27.0:
        level = 'OPTIMAL'
        status = 'SAFE'
        desc = 'Nominal thermal load. Continuous standard work rotation.'
    elif hi_c < 32.0:
        level = 'CAUTION'
        status = 'CAUTION'
        desc = 'Fatigue possible with prolonged exertion. Standard hydration.'
    elif hi_c < 38.0:
        level = 'EXTREME CAUTION'
        status = 'WARNING'
        desc = 'Heat cramps and exhaustion likely. 15-minute hydration breaks recommended.'
    else:
        level = 'DANGER'
        status = 'CRITICAL'
        desc = 'High danger of heat stroke. Immediate cooling and limited personnel mandated!'
    
    return hi_c, level, status, desc

def analyze_lighting(ldr_raw, count):
    """Analyzes raw ADC illumination for occupational safety and carbon/energy waste."""
    if ldr_raw is None:
        return 'UNKNOWN', 'NO DATA', 'NORMAL', 'Sensor pending.'
    
    if count > 0 and ldr_raw < 350:
        return 'HAZARD', 'POOR ILLUMINATION', 'HAZARD', 'Safety violation: Personnel active under substandard lighting.'
    elif count == 0 and ldr_raw > 2200:
        return 'ENERGY WASTE', 'LIGHTS ON (EMPTY ZONE)', 'WARNING', 'Energy inefficiency: High illumination in unoccupied zone.'
    else:
        return 'OPTIMAL', 'COMPLIANT', 'SAFE', 'Ambient illumination matches current operational status.'

def compute_dynamic_capacity(temp_c, hum, ldr_raw, base_cap=10):
    """
    DYNAMIC ENVIRONMENTAL OCCUPANCY ENGINE:
    Calculates permissible workforce capacity scaled strictly between 5 and 10 workers.
    Zone 1 and Zone 2 vary independently based on real-time temperature, humidity, and light.
    """
    penalties = []
    total_penalty = 0

    hi_c, hi_level, _, _ = calculate_heat_index(temp_c, hum)

    # 1. Thermal Ergonomics Penalties (Smooth scaling from 10 down to 5 minimum)
    if hi_c is not None:
        if hi_c >= 42.0:
            total_penalty += 4
            penalties.append({'factor': 'Critical Heat Index', 'delta': -4, 'desc': f'Heat Index {hi_c}°C exceeds 42°C threshold'})
        elif hi_c >= 38.0:
            total_penalty += 3
            penalties.append({'factor': 'Severe Thermal Load', 'delta': -3, 'desc': f'Heat Index {hi_c}°C in elevated caution zone'})
        elif hi_c >= 33.0:
            total_penalty += 2
            penalties.append({'factor': 'Elevated Thermal Load', 'delta': -2, 'desc': f'Heat Index {hi_c}°C requires personnel dispersion'})
        elif hi_c >= 28.0:
            total_penalty += 1
            penalties.append({'factor': 'Mild Thermal Risk', 'delta': -1, 'desc': f'Heat Index {hi_c}°C above nominal room baseline'})

    # 2. Illumination Penalties (Poor visibility near machinery)
    if ldr_raw is not None and ldr_raw < 350:
        total_penalty += 1
        penalties.append({'factor': 'Sub-Standard Illumination', 'delta': -1, 'desc': f'LDR ADC {ldr_raw} below standard factory floor illumination'})

    # Clamp strictly between 5 and 10 workers
    dynamic_cap = max(5, min(10, base_cap - total_penalty))
    status_label = 'OPTIMAL (10/10)' if total_penalty == 0 else (f'RESTRICTED ({dynamic_cap}/10)' if dynamic_cap > 5 else 'MINIMUM THRESHOLD (5/10)')

    return dynamic_cap, total_penalty, penalties, hi_c, status_label

def get_current_dynamic_state():
    """Computes dynamic capacities for both zones independently."""
    dyn_caps = []
    dyn_details = []
    for idx in range(2):
        s = state['sensors'][idx]
        cap, pen, reasons, hi, st = compute_dynamic_capacity(
            s['temp'], s['hum'], s['ldr'], state['base_capacity']
        )
        dyn_caps.append(cap)
        dyn_details.append({
            'zone': idx + 1,
            'base_capacity': state['base_capacity'],
            'dynamic_capacity': cap,
            'total_penalty': pen,
            'penalties': reasons,
            'heat_index': hi,
            'status': st
        })
    return dyn_caps, dyn_details

def compute_safety_score(counts, dynamic_caps, sensor_analysis, online_status, emergency=False):
    """Calculates comprehensive real-time Factory Safety Score (0-100%) against dynamic thresholds."""
    if emergency:
        return 0, 'EMERGENCY EVACUATION', {'occupancy': 0, 'thermal': 0, 'lighting': 0, 'connectivity': 0}

    score = 100
    
    # 1. Dynamic Occupancy compliance penalty
    occ_pen = 0
    for i, c in enumerate(counts):
        allowed = dynamic_caps[i]
        if c > allowed:
            occ_pen += 25 + 15 * (c - allowed)
    occ_pen = min(occ_pen, 45)
    score -= occ_pen
    occ_score = max(0, 100 - int(occ_pen * 2.2))

    # 2. Thermal stress penalty
    therm_pen = 0
    for sa in sensor_analysis:
        lvl = sa.get('heat_level')
        if lvl == 'CAUTION':
            therm_pen += 6
        elif lvl == 'EXTREME CAUTION':
            therm_pen += 16
        elif lvl == 'DANGER':
            therm_pen += 30
    therm_pen = min(therm_pen, 30)
    score -= therm_pen
    therm_score = max(0, 100 - int(therm_pen * 3.3))

    # 3. Lighting & ergonomics penalty
    light_pen = 0
    for sa in sensor_analysis:
        st = sa.get('light_status')
        if st == 'HAZARD':
            light_pen += 15
        elif st == 'ENERGY WASTE':
            light_pen += 5
    light_pen = min(light_pen, 15)
    score -= light_pen
    light_score = max(0, 100 - int(light_pen * 6.6))

    # 4. Telemetry connectivity penalty
    conn_pen = sum(15 for on in online_status if not on)
    score -= conn_pen
    conn_score = max(0, 100 - int(conn_pen * 3.3))

    final_score = max(5, min(100, score))
    
    if final_score >= 90:
        tier = 'EXCELLENT'
    elif final_score >= 75:
        tier = 'GOOD / NOMINAL'
    elif final_score >= 55:
        tier = 'ELEVATED RISK'
    else:
        tier = 'CRITICAL HAZARD'

    return final_score, tier, {
        'occupancy': occ_score,
        'thermal': therm_score,
        'lighting': light_score,
        'connectivity': conn_score
    }

def predict_occupancy_ml(zone, history_rows, current_count, dynamic_cap):
    """Machine learning occupancy forecasting using weighted Ridge regression evaluated against dynamic threshold."""
    zone_rows = [r for r in history_rows if r['zone'] == zone]
    horizons = [5, 10, 15, 30]

    if len(zone_rows) < 6:
        return {
            'horizons': horizons,
            'forecast': [current_count for _ in horizons],
            'confidence': 45,
            'model': 'Baseline Heuristic (Awaiting dataset)',
            'trend': 'STABLE',
            'slope_per_min': 0.0,
            'anomaly_score': 10 if current_count <= dynamic_cap else 65,
            'surge_risk': current_count > dynamic_cap
        }

    ts_arr = np.array([r['ts'] for r in zone_rows], dtype=float)
    t_latest = ts_arr[-1]
    x_mins = (ts_arr - t_latest) / 60.0
    y_counts = np.array([r['count'] for r in zone_rows], dtype=float)

    weights = np.exp(np.clip(x_mins / 15.0, -10, 0))

    X = x_mins.reshape(-1, 1)
    model = Ridge(alpha=1.5)
    model.fit(X, y_counts, sample_weight=weights)

    slope = float(model.coef_[0])
    r2 = float(model.score(X, y_counts))
    confidence = int(max(45, min(96, (r2 * 45 + 55) if not np.isnan(r2) else 50)))

    forecasts = []
    for h in horizons:
        damped_rate = slope * (0.82 ** (h / 5.0))
        pred = max(0, round(current_count + damped_rate * h, 1))
        forecasts.append(pred)

    anomaly = min(100, max(5, int(abs(slope) * 75 + (35 if current_count > dynamic_cap else 0))))
    surge_risk = slope > 0.35 or current_count > dynamic_cap
    trend = 'RISING' if slope > 0.04 else ('FALLING' if slope < -0.04 else 'STABLE')

    return {
        'horizons': horizons,
        'forecast': forecasts,
        'confidence': confidence,
        'model': 'Weighted Ridge Regression (scikit-learn)',
        'slope_per_min': round(slope, 3),
        'trend': trend,
        'anomaly_score': anomaly,
        'surge_risk': surge_risk
    }

# --- FASTAPI ENDPOINTS ---

class Packet(BaseModel):
    node: int = Field(ge=1, le=2)
    boot: str = Field(min_length=1, max_length=60)
    seq: int = Field(ge=0)
    action: str | None = None
    temp: float | None = None
    hum: float | None = None
    ldr: int | None = None

class CapacityUpdate(BaseModel):
    capacity: int = Field(ge=5, le=20)

app = FastAPI(title='ForgeWatch AI - Industrial Intelligence Platform')

@app.get('/')
def home():
    return FileResponse(BASE / 'static' / 'index.html')

@app.post('/api/ingest')
def ingest(p: Packet, request: Request):
    """Device telemetry ingestion endpoint. Returns dynamic capacities and alerts."""
    if request.headers.get('x-device-key') != API_KEY:
        raise HTTPException(401, 'Invalid device key')
    
    with lock:
        i = p.node - 1
        state['seen'][i] = time.time()
        state['sensors'][i] = {'temp': p.temp, 'hum': p.hum, 'ldr': p.ldr}
        accepted = None
        
        if p.action is not None:
            allowed = {1: {'entry1', 'entry2', 'exit1'}, 2: {'exit2'}}
            if p.action not in allowed.get(p.node, set()):
                raise HTTPException(400, 'Invalid action for node')
            
            event_id = f'{p.node}:{p.boot}:{p.seq}'
            with dbconn() as c:
                existing = c.execute('SELECT id FROM events WHERE event_id=?', (event_id,)).fetchone()
                if existing:
                    accepted = True
                else:
                    zone = 1 if p.action.endswith('1') else 2
                    idx = zone - 1
                    is_entry = p.action.startswith('entry')
                    
                    if not is_entry and state['counts'][idx] == 0:
                        accepted = False
                    else:
                        state['counts'][idx] += 1 if is_entry else -1
                        accepted = True
                        ts = time.time()
                        
                        # Match worker profile
                        tag = 'F35B6EE2' if zone == 1 else '7D69E13F'
                        worker_info = WORKERS.get(tag, {})
                        worker_name = worker_info.get('name', f'Worker #{tag[-4:]}')
                        worker_id = worker_info.get('badge', tag)
                        
                        kind = 'ENTRY' if is_entry else 'EXIT'
                        c.execute(
                            'INSERT INTO events(ts, zone, kind, count, event_id, worker_id, worker_name) VALUES(?,?,?,?,?,?,?)',
                            (ts, zone, kind, state['counts'][idx], event_id, worker_id, worker_name)
                        )
                        c.execute('INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)', (f'count{zone}', str(state['counts'][idx])))
                        
                        state['last_events'].insert(0, {
                            'ts': ts,
                            'zone': zone,
                            'kind': kind,
                            'count': state['counts'][idx],
                            'worker_name': worker_name,
                            'worker_id': worker_id
                        })
                        state['last_events'] = state['last_events'][:40]
        
        # Periodic environmental logging
        hi_val, hi_level, _, _ = calculate_heat_index(p.temp, p.hum)
        with dbconn() as c:
            last = c.execute('SELECT ts FROM readings WHERE zone=? ORDER BY id DESC LIMIT 1', (p.node,)).fetchone()
            if not last or time.time() - last[0] >= 5:
                c.execute(
                    'INSERT INTO readings(ts, zone, temp, hum, ldr, count, heat_index, heat_risk) VALUES(?,?,?,?,?,?,?,?)',
                    (time.time(), p.node, p.temp, p.hum, p.ldr, state['counts'][i], hi_val, hi_level)
                )
        
        # Compute real-time dynamic environmental capacity (5 - 10 range)
        dyn_caps, dyn_details = get_current_dynamic_state()

        # Hardware alert: True if count exceeds dynamic capacity OR emergency active
        alerts = [state['emergency'] or (state['counts'][idx] > dyn_caps[idx]) for idx in range(2)]

        if p.action == 'exit2':
            state['last_exit2_id'] = f"{p.node}:{p.boot}:{p.seq}"

        return {
            'ok': True,
            'accepted': accepted,
            'counts': state['counts'][:],
            'capacities': dyn_caps,
            'capacity': dyn_caps[p.node - 1],   # dynamic capacity for this specific node
            'last_exit2_id': state.get('last_exit2_id'),
            'emergency': state['emergency'],
            'alerts': alerts
        }

@app.get('/api/state')
def current():
    """Returns comprehensive real-time factory state, dynamic environmental capacities, and sensor analytics."""
    with lock:
        now = time.time()
        online = [now - t < 8 for t in state['seen']]
        
        dyn_caps, dyn_details = get_current_dynamic_state()
        alerts = [state['emergency'] or (state['counts'][i] > dyn_caps[i]) for i in range(2)]
        
        sensor_analytics = []
        for i in range(2):
            s = state['sensors'][i]
            hi_val, hi_level, hi_status, hi_desc = calculate_heat_index(s['temp'], s['hum'])
            light_eval, light_title, light_status, light_desc = analyze_lighting(s['ldr'], state['counts'][i])
            sensor_analytics.append({
                'zone': i + 1,
                'temp': s['temp'],
                'hum': s['hum'],
                'ldr': s['ldr'],
                'heat_index': hi_val,
                'heat_level': hi_level,
                'heat_status': hi_status,
                'heat_desc': hi_desc,
                'light_eval': light_eval,
                'light_title': light_title,
                'light_status': light_status,
                'light_desc': light_desc,
                'dynamic_capacity': dyn_caps[i],
                'penalty_info': dyn_details[i]
            })

        safety_score, safety_tier, breakdown = compute_safety_score(
            state['counts'], dyn_caps, sensor_analytics, online, state['emergency']
        )

        return {
            'counts': state['counts'][:],
            'total_workers': sum(state['counts']),
            'base_capacity': state['base_capacity'],
            'dynamic_capacities': dyn_caps,
            'dynamic_details': dyn_details,
            'capacity': dyn_caps[0],
            'emergency': state['emergency'],
            'online': online,
            'alerts': alerts,
            'safety_score': safety_score,
            'safety_tier': safety_tier,
            'safety_breakdown': breakdown,
            'sensors': sensor_analytics,
            'events': list(state['last_events']),
            'workers': WORKERS,
            'server_time': now
        }

@app.get('/api/history')
def history():
    """Historical sensor and occupancy telemetry for charts."""
    with dbconn() as c:
        rows = c.execute('''
            SELECT ts, zone, temp, hum, ldr, count, heat_index, heat_risk 
            FROM readings 
            ORDER BY id DESC LIMIT 600
        ''').fetchall()
    return [
        {'ts': a, 'zone': b, 'temp': c, 'hum': d, 'ldr': e, 'count': f, 'heat_index': g, 'heat_risk': h}
        for a, b, c, d, e, f, g, h in reversed(rows)
    ]

@app.get('/api/analytics')
def analytics():
    """Advanced AI and Scikit-Learn occupancy analytics evaluated against dynamic environmental capacities."""
    now = time.time()
    with dbconn() as c:
        events = c.execute('SELECT ts, zone, kind FROM events WHERE ts>=? ORDER BY ts', (now - 86400,)).fetchall()
        readings = c.execute('SELECT ts, zone, count FROM readings WHERE ts>=? ORDER BY ts', (now - 3600,)).fetchall()
    
    dyn_caps, _ = get_current_dynamic_state()
    history_dicts = [{'ts': r[0], 'zone': r[1], 'count': r[2]} for r in readings]
    zones_out = []
    
    for z in (1, 2):
        ze = [r for r in events if r[1] == z]
        entries = sum(r[2] == 'ENTRY' for r in ze)
        exits = sum(r[2] == 'EXIT' for r in ze)
        count = state['counts'][z - 1]
        
        ml_res = predict_occupancy_ml(z, history_dicts, count, dyn_caps[z - 1])
        
        zones_out.append({
            'zone': z,
            'entries24h': entries,
            'exits24h': exits,
            'current': count,
            'base_capacity': state['base_capacity'],
            'dynamic_capacity': dyn_caps[z - 1],
            'ml': ml_res
        })
    
    return {
        'zones': zones_out,
        'engine': 'Adaptive Dynamic Environmental Capacity (5-10 Scale) + Scikit-Learn Ridge Regression',
        'server_time': now
    }

@app.post('/api/emergency/toggle')
def toggle_emergency():
    """Toggles Master Emergency Evacuation Drill / Plant Alarm."""
    with lock:
        state['emergency'] = not state['emergency']
        ts = time.time()
        status_txt = 'EMERGENCY EVACUATION ACTIVATED' if state['emergency'] else 'EMERGENCY CLEARED - ALL CLEAR'
        with dbconn() as c:
            c.execute(
                'INSERT INTO events(ts, zone, kind, count, event_id, worker_id, worker_name) VALUES(?,?,?,?,?,?,?)',
                (ts, 0, 'ALARM', sum(state['counts']), f'alarm:{int(ts)}', 'SYS-SAFETY', status_txt)
            )
        state['last_events'].insert(0, {
            'ts': ts,
            'zone': 0,
            'kind': 'ALARM',
            'count': sum(state['counts']),
            'worker_name': 'Plant Safety System',
            'worker_id': status_txt
        })
        state['last_events'] = state['last_events'][:40]
        return {'emergency': state['emergency']}

@app.post('/api/capacity')
def set_capacity(body: CapacityUpdate):
    """Dynamically updates base zone threshold capacity (between 5 and 10)."""
    with lock:
        state['base_capacity'] = max(5, min(10, body.capacity))
        state['capacity'] = state['base_capacity']
        with dbconn() as c:
            c.execute('INSERT OR REPLACE INTO meta(key, value) VALUES("base_capacity", ?)', (str(state['base_capacity']),))
        return {'ok': True, 'base_capacity': state['base_capacity']}

@app.get('/api/export/audit.csv')
def export_csv():
    """Generates and streams downloadable OSHA/Factory Safety Audit CSV Report with Dynamic Capacities."""
    output = io.StringIO()
    writer = csv.writer(output)
    
    writer.writerow(['ForgeWatch AI - Industrial Safety & Dynamic Environmental Capacity Audit'])
    writer.writerow(['Export Timestamp', time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())])
    writer.writerow([])
    writer.writerow(['--- PERSONNEL ACCESS & OCCUPANCY EVENTS ---'])
    writer.writerow(['Event ID', 'Date & Time', 'Zone', 'Action', 'Zone Count', 'Worker Badge', 'Worker Name'])
    
    with dbconn() as c:
        events = c.execute('SELECT event_id, ts, zone, kind, count, worker_id, worker_name FROM events ORDER BY id DESC LIMIT 500').fetchall()
        for e in events:
            timestr = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(e[1]))
            writer.writerow([e[0], timestr, f'Zone {e[2]}' if e[2] > 0 else 'All Zones', e[3], e[4], e[5], e[6]])
            
        writer.writerow([])
        writer.writerow(['--- ENVIRONMENTAL & HEAT INDEX TELEMETRY ---'])
        writer.writerow(['Timestamp', 'Zone', 'Temperature (°C)', 'Humidity (%)', 'LDR Light ADC', 'Heat Index (°C)', 'Risk Level', 'Occupancy'])
        readings = c.execute('SELECT ts, zone, temp, hum, ldr, heat_index, heat_risk, count FROM readings ORDER BY id DESC LIMIT 500').fetchall()
        for r in readings:
            timestr = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r[0]))
            writer.writerow([timestr, f'Zone {r[1]}', r[2], r[3], r[4], r[5], r[6], r[7]])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type='text/csv',
        headers={'Content-Disposition': f'attachment; filename=forgewatch_audit_{int(time.time())}.csv'}
    )
