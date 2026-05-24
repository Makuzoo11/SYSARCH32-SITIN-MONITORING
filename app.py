from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, Response
import csv, json, io, sqlite3, os, hashlib, zipfile
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from datetime import datetime
from functools import wraps
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'ccs_secret_2024'

UPLOAD_FOLDER = os.path.join('static', 'images')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
MAX_SOFTWARE_IMPORT_BYTES = 5 * 1024 * 1024
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
DB = 'monitoring.db'
LAB_ROOMS = ['542', '526', '528', '530', '544']
PC_COUNT = 40
MAX_SESSIONS_REMAINING = 30
LEADERBOARD_BONUS_SESSION_CAP = 10.0
LEADERBOARD_HOURS_CAP = 30.0
COURSE_GROUPS = {
    'College of Engineering': [
        'BSCE',
        'BSME',
        'BSEE',
        'BSCpE',
        'BSIE',
    ],
    'College of Computer Studies': [
        'BSIT',
        'BSCS',
        'BSIS',
    ],
    'College of Business and Accountancy': [
        'BSA',
        'BSAIS',
        'BSBA',
        'BSCA',
    ],
    'Arts, Sciences, and Education': [
        'BEEd',
        'BSEd',
        'AB English Language',
        'AB Pol Sci',
    ],
    'College of Criminal Justice': [
        'BSCRIM',
    ],
    'College of Nursing': [
        'BSN',
    ],
}

# ─── HELPERS ──────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA busy_timeout = 30000')
    return conn

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def allowed_file(f):
    return '.' in f and f.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def parse_software_import_payload(payload_bytes):
    if zipfile.is_zipfile(io.BytesIO(payload_bytes)):
        records = []
        with zipfile.ZipFile(io.BytesIO(payload_bytes)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                with archive.open(info) as member:
                    member_bytes = member.read()
                try:
                    records.extend(parse_software_import_payload(member_bytes))
                except Exception:
                    continue
        if records:
            return records
        raise ValueError('No importable CSV or JSON files were found inside the ZIP archive.')

    raw = payload_bytes.decode('utf-8-sig', errors='replace').strip()
    if not raw:
        raise ValueError('The uploaded file is empty.')

    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            records = payload.get('software') or payload.get('items') or payload.get('data') or []
        else:
            records = payload
        if isinstance(records, list):
            return records
    except Exception:
        pass

    rows = list(csv.DictReader(io.StringIO(raw)))
    if rows:
        return rows
    raise ValueError('Could not read the file as CSV, JSON, or ZIP.')

def normalize_software_labs(value):
    if value is None:
        return ''
    if isinstance(value, (list, tuple, set)):
        return ','.join(str(item).strip() for item in value if str(item).strip())
    text = str(value).strip()
    if not text:
        return ''
    parts = text.replace(';', ',').split(',')
    return ','.join(part.strip() for part in parts if part.strip())

def parse_boolish(value, default=1):
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    text = str(value).strip().lower()
    if text in {'1', 'true', 'yes', 'on', 'enabled'}:
        return 1
    if text in {'0', 'false', 'no', 'off', 'disabled'}:
        return 0
    return default

def extract_software_fields(raw, default_labs=None):
    if not isinstance(raw, dict):
        return None
    name = (raw.get('name') or raw.get('software_name') or raw.get('app_name') or raw.get('title') or '').strip()
    if not name:
        return None
    description = (raw.get('description') or raw.get('details') or raw.get('info') or '').strip()
    category = (raw.get('category') or raw.get('type') or 'General').strip() or 'General'
    labs_value = raw.get('available_labs') or raw.get('labs') or raw.get('lab_rooms') or raw.get('lab')
    if labs_value:
        available_labs = normalize_software_labs(labs_value)
    else:
        available_labs = normalize_software_labs(default_labs or LAB_ROOMS)
    is_enabled = parse_boolish(raw.get('is_enabled', raw.get('enabled', 1)), default=1)
    return {
        'name': name,
        'description': description,
        'category': category,
        'available_labs': available_labs,
        'is_enabled': is_enabled,
    }

def software_exists(conn, name, category):
    row = conn.execute(
        '''SELECT 1 FROM software
           WHERE LOWER(name)=LOWER(?)
             AND LOWER(COALESCE(category, 'General'))=LOWER(COALESCE(?, 'General'))
           LIMIT 1''',
        (name, category)
    ).fetchone()
    return bool(row)

def fallback_software_record_from_file(filename, default_labs=None):
    base = secure_filename(os.path.splitext(filename or '')[0]).strip('_- .')
    if not base:
        base = 'Uploaded Software'
    return {
        'name': base,
        'description': f'Uploaded file: {filename}',
        'category': 'General',
        'available_labs': normalize_software_labs(default_labs or LAB_ROOMS),
        'is_enabled': 1,
    }

def import_software_records(records, conn, default_labs=None):
    imported = 0
    skipped = 0
    for raw in records:
        item = extract_software_fields(raw, default_labs=default_labs)
        if not item:
            skipped += 1
            continue
        if software_exists(conn, item['name'], item['category']):
            skipped += 1
            continue
        conn.execute('''
            INSERT INTO software (name, description, category, available_labs, is_enabled)
            VALUES (?, ?, ?, ?, ?)
        ''', (
            item['name'],
            item['description'],
            item['category'],
            item['available_labs'],
            item['is_enabled'],
        ))
        imported += 1
    return imported, skipped

def reservations_enabled(conn):
    row = conn.execute(
        "SELECT setting_value FROM reservation_settings WHERE setting_key='reservations_enabled'"
    ).fetchone()
    return (row['setting_value'] if row else '1') == '1'

def fetch_user(conn, user_id):
    return conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()

def get_logo():
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key='logo_filename'").fetchone()
    conn.close()
    return row['value'] if row else ''

def create_notification(user_id, title, message, category='info', conn=None):
    owns_connection = conn is None
    conn = conn or get_db()
    conn.execute(
        '''INSERT INTO notifications (user_id, title, message, category)
           VALUES (?,?,?,?)''',
        (user_id, title, message, category)
    )
    if owns_connection:
        conn.commit()
        conn.close()

def log_reasoning(admin_id, student_id, sit_in_log_id, action, reasoning, conn):
    conn.execute(
        '''INSERT INTO reasoning_logs (admin_id, student_id, sit_in_log_id, action, reasoning)
           VALUES (?,?,?,?,?)''',
        (admin_id, student_id, sit_in_log_id, action, reasoning.strip())
    )

def leaderboard_entries(conn, limit=None):
    rows = [dict(r) for r in conn.execute('''
        SELECT u.id, u.id_number, u.first_name, u.last_name, u.course, u.course_level,
               u.reward_points, u.sessions_remaining, u.admin_remarks,
               COALESCE(SUM(
                   CASE
                       WHEN s.time_out IS NOT NULL
                            AND julianday(s.time_out) >= julianday(s.time_in)
                       THEN (julianday(s.time_out) - julianday(s.time_in)) * 24.0
                       ELSE 0
                   END
               ), 0) AS total_hours
        FROM users u
        LEFT JOIN sit_in_logs s
          ON s.user_id = u.id AND s.status IN ('Done', 'Approved') AND COALESCE(s.source, 'admin') != 'login'
        WHERE u.is_admin = 0
        GROUP BY u.id
        ORDER BY u.last_name, u.first_name
    ''').fetchall()]
    if not rows:
        return []

    for row in rows:
        bonus_sessions = (row['reward_points'] or 0) / 3.0
        hours = row['total_hours'] or 0
        score = (
            (min(bonus_sessions, LEADERBOARD_BONUS_SESSION_CAP) / LEADERBOARD_BONUS_SESSION_CAP) * 50.0 +
            (min(hours, LEADERBOARD_HOURS_CAP) / LEADERBOARD_HOURS_CAP) * 30.0
        )
        row['total_hours'] = round(hours, 2)
        row['bonus_sessions'] = round(bonus_sessions, 2)
        row['leaderboard_score'] = round(score, 2)

    rows.sort(key=lambda item: (-item['leaderboard_score'], -(item['bonus_sessions'] or 0), -(item['total_hours'] or 0), item['last_name'], item['first_name']))
    for idx, row in enumerate(rows, start=1):
        row['rank'] = idx

    return rows[:limit] if limit else rows

def dashboard_notifications(conn, user_id, limit=6):
    return conn.execute(
        '''SELECT * FROM notifications
           WHERE user_id=?
           ORDER BY is_read ASC, created_at DESC
           LIMIT ?''',
        (user_id, limit)
    ).fetchall()

def lab_options():
    return LAB_ROOMS

def enabled_lab_options(conn):
    labs = [dict(r) for r in conn.execute('''
        SELECT lab_room,
               MAX(is_enabled) AS is_enabled,
               MAX(capacity) AS capacity,
               MAX(description) AS description
        FROM lab_availability
        WHERE is_enabled=1
        GROUP BY lab_room
        ORDER BY CAST(lab_room AS INTEGER)
    ''').fetchall()]
    return labs

def lab_capacity_map(conn):
    capacities = {lab: PC_COUNT for lab in LAB_ROOMS}
    for row in conn.execute('SELECT lab_room, capacity FROM lab_availability').fetchall():
        lab = normalize_lab_room(row['lab_room'])
        if not lab:
            continue
        try:
            capacities[lab] = max(1, int(row['capacity'] or PC_COUNT))
        except (TypeError, ValueError):
            capacities[lab] = PC_COUNT
    return capacities

@app.context_processor
def inject_lab_capacities():
    conn = get_db()
    try:
        return {'lab_capacities': lab_capacity_map(conn)}
    finally:
        conn.close()

def course_groups():
    return COURSE_GROUPS

def course_options():
    return [course for courses in COURSE_GROUPS.values() for course in courses]

def course_groups_with_existing(existing_courses):
    groups = {department: list(courses) for department, courses in COURSE_GROUPS.items()}
    return groups

def pc_options(limit=PC_COUNT):
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = PC_COUNT
    return [str(i) for i in range(1, limit + 1)]

def normalize_lab_room(value):
    value = (value or '').strip()
    return value if value in LAB_ROOMS else ''

def normalize_pc_number(value, limit=None):
    value = (value or '').strip()
    return value if value in pc_options(limit) else ''

def build_lab_pc_map(conn, capacities=None):
    capacities = capacities or lab_capacity_map(conn)
    status_map = {
        lab: {
            pc: {
                'state': 'available',
                'label': 'Available',
                'log_id': None,
                'student_name': '',
                'id_number': '',
                'status': '',
            }
            for pc in pc_options(capacities.get(lab, PC_COUNT))
        }
        for lab in LAB_ROOMS
    }
    priority = {'available': 0, 'reserved': 1, 'in_use': 2}

    rows = conn.execute('''
        SELECT s.id, s.lab_room, s.pc_number, s.status, s.source,
               u.first_name, u.last_name, u.id_number
        FROM sit_in_logs s
        JOIN users u ON u.id = s.user_id
        WHERE COALESCE(s.source, 'admin') != 'login'
          AND TRIM(COALESCE(s.lab_room, '')) != ''
          AND TRIM(COALESCE(s.pc_number, '')) != ''
          AND s.status IN ('Pending', 'Approved', 'Active')
    ''').fetchall()

    for row in rows:
        lab = normalize_lab_room(row['lab_room'])
        pc = normalize_pc_number(row['pc_number'], capacities.get(lab, PC_COUNT))
        if not lab or not pc:
            continue
        state = 'in_use' if row['status'] == 'Active' else 'reserved'
        label = 'Currently in use' if state == 'in_use' else 'Reserved'
        current = status_map[lab][pc]
        if priority[state] >= priority[current['state']]:
            status_map[lab][pc] = {
                'state': state,
                'label': label,
                'log_id': row['id'],
                'student_name': f"{row['first_name']} {row['last_name']}",
                'id_number': row['id_number'],
                'status': row['status'],
            }

    # Override with maintenance status
    maint_rows = conn.execute("SELECT lab_room, pc_number FROM pc_status WHERE status='maintenance'").fetchall()
    for row in maint_rows:
        lab = normalize_lab_room(row['lab_room'])
        pc = normalize_pc_number(row['pc_number'], capacities.get(lab, PC_COUNT))
        if lab and pc and lab in status_map and pc in status_map[lab]:
            status_map[lab][pc]['state'] = 'maintenance'
            status_map[lab][pc]['label'] = 'Maintenance'

    return status_map

def get_maintenance_map(conn):
    """Return {lab_room: {pc_number: True}} for PCs in maintenance."""
    mmap = {lab: {} for lab in LAB_ROOMS}
    for row in conn.execute("SELECT lab_room, pc_number FROM pc_status WHERE status='maintenance'").fetchall():
        lab = row['lab_room']
        if lab in mmap:
            mmap[lab][row['pc_number']] = True
    return mmap

def pc_is_available(conn, lab_room, pc_number, ignore_log_id=None, capacities=None):
    capacities = capacities or lab_capacity_map(conn)
    lab_room = normalize_lab_room(lab_room)
    pc_number = normalize_pc_number(pc_number, capacities.get(lab_room, PC_COUNT))
    if not lab_room or not pc_number:
        return False

    # Check maintenance
    maint = conn.execute(
        "SELECT 1 FROM pc_status WHERE lab_room=? AND pc_number=? AND status='maintenance' LIMIT 1",
        (lab_room, pc_number)
    ).fetchone()
    if maint:
        return False

    query = '''
        SELECT id
        FROM sit_in_logs
        WHERE lab_room=?
          AND pc_number=?
          AND status IN ('Pending', 'Approved', 'Active')
          AND COALESCE(source, 'admin') != 'login'
    '''
    params = [lab_room, pc_number]
    if ignore_log_id is not None:
        query += ' AND id != ?'
        params.append(ignore_log_id)

    row = conn.execute(query, params).fetchone()
    return row is None

def delete_sitin_log_dependents(conn, log_id):
    conn.execute("DELETE FROM feedback WHERE sit_in_log_id=?", (log_id,))
    conn.execute("DELETE FROM reasoning_logs WHERE sit_in_log_id=?", (log_id,))

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('is_admin'):
            return redirect(url_for('login'))
        if request.endpoint != 'force_change_password':
            conn = get_db()
            try:
                user = fetch_user(conn, session.get('user_id'))
            finally:
                conn.close()
            if user and user['must_change_password']:
                return redirect(url_for('force_change_password'))
        return f(*args, **kwargs)
    return decorated

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if request.endpoint != 'force_change_password':
            conn = get_db()
            try:
                user = fetch_user(conn, session.get('user_id'))
            finally:
                conn.close()
            if user and not user['is_admin'] and user['must_change_password']:
                return redirect(url_for('force_change_password'))
        return f(*args, **kwargs)
    return decorated

# ─── DB INIT ──────────────────────────────────────────────
def convert_reservation_points(user_id, conn):
    conn.execute("UPDATE users SET reservation_points=reservation_points+1 WHERE id=?", (user_id,))
    row = conn.execute("SELECT reservation_points, sessions_remaining FROM users WHERE id=?", (user_id,)).fetchone()
    points = row['reservation_points'] if row else 0
    current_sessions = row['sessions_remaining'] if row else 0
    if current_sessions > MAX_SESSIONS_REMAINING:
        conn.execute("UPDATE users SET sessions_remaining=? WHERE id=?", (MAX_SESSIONS_REMAINING, user_id))
        current_sessions = MAX_SESSIONS_REMAINING
    if points >= 3 and current_sessions < MAX_SESSIONS_REMAINING:
        extra_sessions = points // 3
        space_available = MAX_SESSIONS_REMAINING - current_sessions
        sessions_to_add = min(extra_sessions, space_available)
        remaining_points = points - (sessions_to_add * 3)
        conn.execute(
            "UPDATE users SET sessions_remaining=sessions_remaining+?, reservation_points=? WHERE id=?",
            (sessions_to_add, remaining_points, user_id)
        )
        return sessions_to_add, remaining_points
    return 0, points


def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            id_number          TEXT UNIQUE NOT NULL,
            last_name          TEXT NOT NULL,
            first_name         TEXT NOT NULL,
            middle_name        TEXT DEFAULT '',
            course             TEXT DEFAULT '',
            course_level       INTEGER DEFAULT 1,
            email              TEXT DEFAULT '',
            address            TEXT DEFAULT '',
            password           TEXT NOT NULL,
            sessions_remaining INTEGER DEFAULT 30,
            reservation_points INTEGER DEFAULT 0,
            reward_points      INTEGER DEFAULT 0,
            completed_tasks    INTEGER DEFAULT 0,
            is_admin           INTEGER DEFAULT 0,
            admin_remarks      TEXT DEFAULT '',
            profile_pic        TEXT DEFAULT '',
            created_at         DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS sit_in_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            lab_room    TEXT DEFAULT '',
            pc_number   TEXT DEFAULT '',
            purpose     TEXT DEFAULT '',
            time_in     DATETIME DEFAULT CURRENT_TIMESTAMP,
            time_out    DATETIME,
            status      TEXT DEFAULT 'Active',
            admin_remarks TEXT DEFAULT '',
            request_reason TEXT DEFAULT '',
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS announcements (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            content    TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS feedback (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            sit_in_log_id INTEGER NOT NULL,
            rating       INTEGER DEFAULT 0,
            feedback_text TEXT DEFAULT '',
            created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, sit_in_log_id),
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(sit_in_log_id) REFERENCES sit_in_logs(id)
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            title      TEXT NOT NULL,
            message    TEXT NOT NULL,
            category   TEXT DEFAULT 'info',
            is_read    INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS reasoning_logs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id      INTEGER,
            student_id    INTEGER,
            sit_in_log_id INTEGER,
            action        TEXT NOT NULL,
            reasoning     TEXT DEFAULT '',
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(admin_id) REFERENCES users(id),
            FOREIGN KEY(student_id) REFERENCES users(id),
            FOREIGN KEY(sit_in_log_id) REFERENCES sit_in_logs(id)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS courses (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS software (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            description TEXT DEFAULT '',
            category    TEXT DEFAULT 'General',
            available_labs TEXT DEFAULT '',
            is_enabled  INTEGER DEFAULT 1,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS lab_availability (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            lab_room    TEXT NOT NULL,
            is_enabled  INTEGER DEFAULT 1,
            capacity    INTEGER DEFAULT 40,
            description TEXT DEFAULT '',
            updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS reservation_settings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            setting_key TEXT UNIQUE NOT NULL,
            setting_value TEXT DEFAULT '',
            updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS pc_status (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            lab_room    TEXT NOT NULL,
            pc_number   TEXT NOT NULL,
            status      TEXT DEFAULT 'available',
            updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(lab_room, pc_number)
        );
        INSERT OR IGNORE INTO settings (key, value) VALUES ('logo_filename', '');
        INSERT OR IGNORE INTO courses (code) VALUES ('BSIT'),('BSCS'),('BSIS'),('BSCE'),('BSCpE');
    ''')
    conn.executemany(
        'INSERT OR IGNORE INTO courses (code) VALUES (?)',
        [(course,) for course in course_options()]
    )
    # Add profile_pic column if it doesn't exist (for existing databases)
    try:
        conn.execute("ALTER TABLE users ADD COLUMN profile_pic TEXT DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Add source column to sit_in_logs to differentiate admin vs student vs login entries
    try:
        conn.execute("ALTER TABLE sit_in_logs ADD COLUMN source TEXT DEFAULT 'admin'")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Add must_change_password flag for admin-created students
    try:
        conn.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Add reservation_points counter for sit-in rewards
    try:
        conn.execute("ALTER TABLE users ADD COLUMN reservation_points INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    try:
        conn.execute("ALTER TABLE users ADD COLUMN reward_points INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    try:
        conn.execute("ALTER TABLE users ADD COLUMN completed_tasks INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    try:
        conn.execute("ALTER TABLE users ADD COLUMN admin_remarks TEXT DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    try:
        conn.execute("ALTER TABLE sit_in_logs ADD COLUMN admin_remarks TEXT DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    try:
        conn.execute("ALTER TABLE sit_in_logs ADD COLUMN pc_number TEXT DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    try:
        conn.execute("ALTER TABLE sit_in_logs ADD COLUMN request_reason TEXT DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    # Add software column to sit_in_logs to track requested software for a reservation
    try:
        conn.execute("ALTER TABLE sit_in_logs ADD COLUMN software TEXT DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists

    # Initialize default labs if not exists
    try:
        conn.execute("UPDATE lab_availability SET lab_room='528' WHERE lab_room='524'")
        conn.execute("UPDATE sit_in_logs SET lab_room='528' WHERE lab_room='524'")
        conn.execute('''
            UPDATE pc_status
            SET lab_room='528'
            WHERE lab_room='524'
              AND NOT EXISTS (
                  SELECT 1
                  FROM pc_status AS existing
                  WHERE existing.lab_room='528'
                    AND existing.pc_number=pc_status.pc_number
              )
        ''')
        conn.execute("DELETE FROM pc_status WHERE lab_room='524'")
        conn.commit()
    except sqlite3.Error:
        pass

    for lab in LAB_ROOMS:
        try:
            conn.execute("INSERT OR IGNORE INTO lab_availability (lab_room, is_enabled, capacity) VALUES (?, 1, ?)", (lab, PC_COUNT))
            conn.commit()
        except sqlite3.OperationalError:
            pass

    # Initialize reservation settings
    try:
        conn.execute("INSERT OR IGNORE INTO reservation_settings (setting_key, setting_value) VALUES (?, ?)", ('reservations_enabled', '1'))
        conn.commit()
    except sqlite3.OperationalError:
        pass

    # Seed default lab software
    all_labs = ','.join(LAB_ROOMS)
    default_software = [
        ('Visual Studio Code',  'Lightweight code editor with IntelliSense and Git support.',          'Development',    all_labs),
        ('Visual Studio',       'Full-featured IDE for .NET, C++, and Windows development.',           'Development',    all_labs),
        ('Python 3',            'General-purpose programming language for scripting and data.',         'Development',    all_labs),
        ('NetBeans IDE',        'IDE for Java SE, Java EE, and PHP development.',                       'Development',    all_labs),
        ('Eclipse IDE',         'Open-source IDE primarily for Java development.',                      'Development',    all_labs),
        ('Android Studio',      'Official IDE for Android app development.',                            'Development',    all_labs),
        ('XAMPP',               'Apache, MySQL, PHP, and Perl local development stack.',                'Web & Database', all_labs),
        ('MySQL Workbench',     'Visual tool for database design and MySQL administration.',             'Web & Database', all_labs),
        ('Git',                 'Distributed version control system.',                                  'Development',    all_labs),
        ('GitHub Desktop',      'GUI client for Git and GitHub repositories.',                          'Development',    all_labs),
        ('Cisco Packet Tracer', 'Network simulation tool for designing and testing network topologies.','Networking',     all_labs),
        ('Wireshark',           'Network protocol analyzer for traffic inspection.',                    'Networking',     all_labs),
        ('VMware Workstation',  'Virtualization platform for running multiple OS environments.',        'Virtualization', all_labs),
        ('VirtualBox',          'Free virtualization software for running virtual machines.',           'Virtualization', all_labs),
        ('Microsoft Office',    'Word, Excel, PowerPoint, and other productivity tools.',               'Productivity',   all_labs),
        ('LibreOffice',         'Free and open-source office suite.',                                   'Productivity',   all_labs),
        ('MATLAB',              'Numerical computing environment for engineering and science.',          'Engineering',    all_labs),
        ('Arduino IDE',         'IDE for programming Arduino microcontrollers.',                        'Engineering',    all_labs),
        ('Sublime Text',        'Fast and lightweight text editor with a powerful plugin API.',         'Development',    all_labs),
        ('Postman',             'API development and testing platform.',                                'Development',    all_labs),
        ('DBeaver',             'Universal database management and SQL client tool.',                   'Web & Database', all_labs),
    ]
    for sw_name, sw_desc, sw_cat, sw_labs in default_software:
        try:
            conn.execute('''
                INSERT OR IGNORE INTO software (name, description, category, available_labs, is_enabled)
                VALUES (?, ?, ?, ?, 1)
            ''', (sw_name, sw_desc, sw_cat, sw_labs))
        except Exception:
            pass
    conn.commit()

    conn.execute("UPDATE sit_in_logs SET status='Pending' WHERE source='student' AND status='Active'")
    conn.execute("DELETE FROM sit_in_logs WHERE source='login' OR purpose='Login'")
    conn.execute('''
        UPDATE sit_in_logs
        SET time_out = time_in
        WHERE time_out IS NOT NULL
          AND julianday(time_out) < julianday(time_in)
    ''')

    # Create admin separately with parameterized query
    conn.execute('''
        INSERT OR IGNORE INTO users
            (id_number, last_name, first_name, middle_name, course,
             course_level, email, address, password, sessions_remaining, is_admin)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    ''', ('admin','Admin','CCS','','N/A',0,'admin@ccs.edu','CCS',hash_pw('admin'),0,1))
    conn.commit()
    conn.close()

@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        id_num = request.form.get('id_number','').strip()
        pw     = request.form.get('password','').strip()
        conn   = get_db()
        user   = conn.execute(
            'SELECT * FROM users WHERE id_number=? AND password=?',
            (id_num, hash_pw(pw))
        ).fetchone()
        conn.close()
        if user:
            session['user_id']  = user['id']
            session['name']     = f"{user['first_name']} {user['last_name']}"
            session['is_admin'] = bool(user['is_admin'])
            if not user['is_admin'] and user['must_change_password']:
                return redirect(url_for('force_change_password'))
            return redirect(url_for('admin_dashboard') if user['is_admin'] else url_for('dashboard'))
        flash('Invalid ID number or password.', 'error')
    return render_template('login.html', logo=get_logo())

@app.route('/register', methods=['GET','POST'])
def register():
    conn    = get_db()
    courses = [r['code'] for r in conn.execute('SELECT code FROM courses ORDER BY code').fetchall()]
    grouped_courses = course_groups_with_existing(courses)
    conn.close()
    if request.method == 'POST':
        d = {k: request.form.get(k,'').strip() for k in
             ['id_number','last_name','first_name','middle_name','course_level',
              'password','repeat_password','email','course','address']}
        if d['password'] != d['repeat_password']:
            flash('Passwords do not match.', 'error')
            return render_template('register.html', logo=get_logo(), data=d, course_groups=grouped_courses)
        try:
            conn = get_db()
            conn.execute('''INSERT INTO users
                (id_number,last_name,first_name,middle_name,course_level,
                 password,email,course,address)
                VALUES (?,?,?,?,?,?,?,?,?)''',
                (d['id_number'],d['last_name'],d['first_name'],d['middle_name'],
                 d['course_level'],hash_pw(d['password']),d['email'],d['course'],d['address']))
            conn.commit()
            conn.close()
            flash('Registration successful! Please log in.', 'success')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('ID Number already registered.', 'error')
    return render_template('register.html', logo=get_logo(), data={}, course_groups=grouped_courses)

@app.route('/logout')
def logout():
    user_id = session.get('user_id')
    if user_id:
        conn = get_db()
        try:
            user = conn.execute("SELECT id, is_admin FROM users WHERE id=?", (user_id,)).fetchone()
            if user and not user['is_admin']:
                # find any active sit-in rows for this user (include all sources)
                active_rows = conn.execute(
                    "SELECT id FROM sit_in_logs WHERE user_id=? AND status='Active'",
                    (user_id,)
                ).fetchall()
                active_ids = [row['id'] for row in active_rows]
                if active_ids:
                    count = len(active_ids)
                    conn.execute(
                        "UPDATE users SET sessions_remaining = CASE WHEN sessions_remaining >= ? THEN sessions_remaining - ? ELSE 0 END WHERE id=?",
                        (count, count, user_id)
                    )
                    conn.execute(
                        f"UPDATE sit_in_logs SET status='Done', time_out=COALESCE(time_out, CURRENT_TIMESTAMP) WHERE id IN ({','.join(['?'] * len(active_ids))})",
                        (*active_ids,)
                    )
                    conn.commit()
        finally:
            conn.close()
    session.clear()
    return redirect(url_for('login'))

@app.route('/change-password', methods=['GET', 'POST'])
@login_required
def force_change_password():
    if request.method == 'POST':
        new_pw     = request.form.get('new_password', '').strip()
        confirm_pw = request.form.get('confirm_password', '').strip()
        if not new_pw:
            flash('Password cannot be empty.', 'error')
            return redirect(url_for('force_change_password'))
        if new_pw != confirm_pw:
            flash('Passwords do not match.', 'error')
            return redirect(url_for('force_change_password'))
        conn = get_db()
        conn.execute(
            'UPDATE users SET password=?, must_change_password=0 WHERE id=?',
            (hash_pw(new_pw), session['user_id'])
        )
        conn.commit()
        conn.close()
        flash('Password updated! Welcome.', 'success')
        return redirect(url_for('dashboard'))
    return render_template('force_change_password.html', logo=get_logo())

# ─── STUDENT DASHBOARD ────────────────────────────────────
@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_db()
    user = fetch_user(conn, session['user_id'])
    announcements = conn.execute('SELECT * FROM announcements ORDER BY created_at DESC').fetchall()
    notifications = dashboard_notifications(conn, session['user_id'])
    pending_requests = conn.execute(
        '''SELECT COUNT(*) FROM sit_in_logs
           WHERE user_id=? AND source='student' AND status='Pending' ''',
        (session['user_id'],)
    ).fetchone()[0]

    conn.close()
    return render_template(
        'dashboard.html',
        user=user,
        announcements=announcements,
        notifications=notifications,
        pending_requests=pending_requests,
        logo=get_logo()
    )

# ─── STUDENT PROFILE EDIT ─────────────────────────────────
@app.route('/profile/edit', methods=['POST'])
@login_required
def edit_profile():
    uid        = session['user_id']
    first_name = request.form.get('first_name', '').strip()
    last_name  = request.form.get('last_name', '').strip()
    email      = request.form.get('email', '').strip()
    address    = request.form.get('address', '').strip()
    tab        = request.form.get('tab', 'info')
    new_pw     = request.form.get('new_password', '').strip()
    confirm_pw = request.form.get('confirm_password', '').strip()
    current_pw = request.form.get('current_password', '').strip()

    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()

    # Verify current password only when changing password
    if tab == 'pw':
        if user['password'] != hash_pw(current_pw):
            conn.close()
            flash('Current password is incorrect.', 'error')
            return redirect(url_for('dashboard'))

    # Handle profile picture upload
    profile_pic = user['profile_pic'] or ''
    file = request.files.get('profile_pic')
    if file and file.filename and allowed_file(file.filename):
        filename = secure_filename(f"profile_{uid}_{file.filename}")
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        profile_pic = filename

    # Handle password change
    if new_pw:
        if new_pw != confirm_pw:
            conn.close()
            flash('New passwords do not match.', 'error')
            return redirect(url_for('dashboard'))
        password = hash_pw(new_pw)
    else:
        password = user['password']

    conn.execute('''UPDATE users SET first_name=?, last_name=?, email=?,
                    address=?, password=?, profile_pic=? WHERE id=?''',
                 (first_name, last_name, email, address, password, profile_pic, uid))
    conn.commit()
    conn.close()

    session['name'] = f"{first_name} {last_name}"
    flash('Profile updated successfully!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/profile/photo', methods=['POST'])
@login_required
def update_profile_photo():
    uid = session['user_id']
    file = request.files.get('profile_pic')

    if not file or not file.filename:
        flash('Please choose an image file.', 'error')
        return redirect(url_for('dashboard'))

    if not allowed_file(file.filename):
        flash('Only JPG, PNG, GIF, or WEBP images are allowed.', 'error')
        return redirect(url_for('dashboard'))

    filename = secure_filename(f"profile_{uid}_{file.filename}")
    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))

    conn = get_db()
    conn.execute("UPDATE users SET profile_pic=? WHERE id=?", (filename, uid))
    conn.commit()
    conn.close()

    flash('Profile photo updated successfully.', 'success')
    return redirect(url_for('dashboard'))

# ─── LOGO UPLOAD ──────────────────────────────────────────
@app.route('/upload_logo', methods=['POST'])
def upload_logo():
    f = request.files.get('logo')
    if f and allowed_file(f.filename):
        filename = secure_filename(f.filename)
        f.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        conn = get_db()
        conn.execute("UPDATE settings SET value=? WHERE key='logo_filename'", (filename,))
        conn.commit()
        conn.close()
    return redirect(request.referrer or url_for('login'))

# ─── ADMIN DASHBOARD ──────────────────────────────────────
@app.route('/admin')
@admin_required
def admin_dashboard():
    conn = get_db()
    leaderboard = leaderboard_entries(conn, limit=5)
    reservation_setting = conn.execute(
        "SELECT setting_value FROM reservation_settings WHERE setting_key='reservations_enabled'"
    ).fetchone()
    stats = {
        'total_students':  conn.execute("SELECT COUNT(*) FROM users WHERE is_admin=0").fetchone()[0],
        'currently_sitin': conn.execute("SELECT COUNT(*) FROM sit_in_logs WHERE status='Active' AND COALESCE(source, 'admin') != 'login'").fetchone()[0],
        'total_sitin':     conn.execute("SELECT COUNT(*) FROM sit_in_logs WHERE COALESCE(source, 'admin') != 'login'").fetchone()[0],
        'pending_requests': conn.execute("SELECT COUNT(*) FROM sit_in_logs WHERE status='Pending' AND COALESCE(source, 'admin') != 'login'").fetchone()[0],
        'feedback_entries': conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0],
    }
    announcements = conn.execute("SELECT * FROM announcements ORDER BY created_at DESC").fetchall()
    course_stats  = [{'course': r['course'], 'cnt': r['cnt']} for r in conn.execute(
        "SELECT course, COUNT(*) as cnt FROM users WHERE is_admin=0 GROUP BY course"
    ).fetchall()]
    analytics = {
        'avg_hours': round(conn.execute('''
            SELECT COALESCE(AVG(hours_spent), 0) FROM (
                SELECT (julianday(time_out) - julianday(time_in)) * 24.0 AS hours_spent
                FROM sit_in_logs
                WHERE time_out IS NOT NULL
                  AND julianday(time_out) >= julianday(time_in)
            )
        ''').fetchone()[0] or 0, 2),
        'avg_feedback': round(conn.execute("SELECT COALESCE(AVG(rating), 0) FROM feedback").fetchone()[0] or 0, 2),
        'top_course': conn.execute('''
            SELECT course FROM users
            WHERE is_admin=0
            GROUP BY course
            ORDER BY COUNT(*) DESC, course ASC
            LIMIT 1
        ''').fetchone(),
    }
    conn.close()
    return render_template(
        'admin_dashboard.html',
        stats=stats,
        announcements=announcements,
        course_stats=course_stats,
        leaderboard=leaderboard,
        analytics=analytics,
        reservation_enabled=(reservation_setting['setting_value'] if reservation_setting else '1') == '1',
        logo=get_logo()
    )

@app.route('/admin/announcement', methods=['POST'])
@admin_required
def post_announcement():
    content = request.form.get('content','').strip()
    if content:
        conn = get_db()
        conn.execute("INSERT INTO announcements (content) VALUES (?)", (content,))
        student_ids = [row['id'] for row in conn.execute("SELECT id FROM users WHERE is_admin=0").fetchall()]
        for student_id in student_ids:
            create_notification(student_id, 'New announcement', content[:140], 'info', conn)
        conn.commit()
        conn.close()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/announcement/delete/<int:aid>', methods=['POST'])
@admin_required
def delete_announcement(aid):
    conn = get_db()
    conn.execute("DELETE FROM announcements WHERE id=?", (aid,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_dashboard'))

# ─── ADMIN STUDENTS ───────────────────────────────────────
@app.route('/admin/students')
@admin_required
def admin_students():
    search = request.args.get('search','').strip()
    conn   = get_db()
    if search:
        students = [dict(r) for r in conn.execute('''SELECT * FROM users WHERE is_admin=0 AND
            (id_number LIKE ? OR first_name LIKE ? OR last_name LIKE ?) ORDER BY last_name''',
            (f'%{search}%',)*3).fetchall()]
    else:
        students = [dict(r) for r in conn.execute("SELECT * FROM users WHERE is_admin=0 ORDER BY last_name").fetchall()]
    courses = [r['code'] for r in conn.execute('SELECT code FROM courses ORDER BY code').fetchall()]
    grouped_courses = course_groups_with_existing(courses)
    conn.close()
    return render_template('admin_students.html', students=students,
                           courses=courses, course_groups=grouped_courses, search=search, logo=get_logo())

@app.route('/admin/students/add', methods=['POST'])
@admin_required
def admin_add_student():
    d = {k: request.form.get(k,'').strip() for k in
         ['id_number','last_name','first_name','middle_name','course_level',
          'email','course','address','password']}
    try:
        conn = get_db()
        conn.execute('''INSERT INTO users
            (id_number,last_name,first_name,middle_name,course_level,email,course,address,password,must_change_password)
            VALUES (?,?,?,?,?,?,?,?,?,1)''',
            (d['id_number'],d['last_name'],d['first_name'],d['middle_name'],
             d['course_level'],d['email'],d['course'],d['address'],hash_pw(d['password'])))
        conn.commit()
        conn.close()
        flash('Student added.', 'success')
    except sqlite3.IntegrityError:
        flash('ID Number already exists.', 'error')
    return redirect(url_for('admin_students'))

@app.route('/admin/students/edit/<int:uid>', methods=['POST'])
@admin_required
def admin_edit_student(uid):
    last_name   = request.form.get('last_name','').strip()
    first_name  = request.form.get('first_name','').strip()
    middle_name = request.form.get('middle_name','').strip()
    course_level= request.form.get('course_level','1').strip()
    email       = request.form.get('email','').strip()
    course      = request.form.get('course','').strip()
    address     = request.form.get('address','').strip()
    remarks     = request.form.get('admin_remarks','').strip()
    try:
        sessions = int(request.form.get('sessions_remaining', 0))
    except (ValueError, TypeError):
        sessions = 0
    try:
        reward_points = int(request.form.get('reward_points', 0))
    except (ValueError, TypeError):
        reward_points = 0
    conn = get_db()
    conn.execute(
        '''UPDATE users SET last_name=?, first_name=?, middle_name=?,
            course_level=?, email=?, course=?, address=?, sessions_remaining=?,
            reward_points=?, admin_remarks=?
            WHERE id=?''',
        (
            last_name, first_name, middle_name, course_level, email, course, address,
            sessions, reward_points, remarks, uid
        )
    )
    create_notification(
        uid,
        'Profile updated by admin',
        'Your records, rewards, or remarks were updated by the admin panel.',
        'info',
        conn
    )
    conn.commit()
    conn.close()
    flash('Student updated.', 'success')
    return redirect(url_for('admin_students'))

@app.route('/admin/students/delete/<int:uid>', methods=['POST'])
@admin_required
def admin_delete_student(uid):
    conn = get_db()
    try:
        log_ids = [row['id'] for row in conn.execute("SELECT id FROM sit_in_logs WHERE user_id=?", (uid,)).fetchall()]
        if log_ids:
            placeholders = ','.join('?' for _ in log_ids)
            conn.execute(f"DELETE FROM feedback WHERE sit_in_log_id IN ({placeholders})", log_ids)
            conn.execute(f"DELETE FROM reasoning_logs WHERE sit_in_log_id IN ({placeholders})", log_ids)
        conn.execute("DELETE FROM reasoning_logs WHERE student_id=?", (uid,))
        conn.execute("DELETE FROM notifications WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM feedback WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM sit_in_logs WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        flash('Could not delete the student because related records are still linked.', 'error')
        conn.close()
        return redirect(url_for('admin_students'))
    conn.close()
    flash('Student deleted.', 'success')
    return redirect(url_for('admin_students'))

@app.route('/admin/students/reset_sessions', methods=['POST'])
@admin_required
def reset_all_sessions():
    conn = get_db()
    conn.execute("UPDATE users SET sessions_remaining=30 WHERE is_admin=0")
    conn.commit()
    conn.close()
    flash('All sessions reset to 30.', 'success')
    return redirect(url_for('admin_students'))

# ─── ADMIN SEARCH ─────────────────────────────────────────
@app.route('/admin/search')
@admin_required
def admin_search():
    q = request.args.get('q','').strip()
    if not q:
        return jsonify([])
    conn    = get_db()
    results = conn.execute('''SELECT * FROM users WHERE is_admin=0 AND
        (id_number LIKE ? OR first_name LIKE ? OR last_name LIKE ?)''',
        (f'%{q}%',)*3).fetchall()
    conn.close()
    return jsonify([dict(r) for r in results])

# ─── ADMIN SIT-IN ─────────────────────────────────────────
@app.route('/admin/sitin')
@admin_required
def admin_sitin():
    conn = get_db()
    capacities = lab_capacity_map(conn)
    logs = [dict(r) for r in conn.execute('''SELECT s.*, u.id_number, u.first_name, u.last_name,
                            u.sessions_remaining, u.course
                           FROM sit_in_logs s JOIN users u ON s.user_id=u.id
                           WHERE s.source = 'admin'
                           ORDER BY s.time_in DESC''').fetchall()]
    booking_map = build_lab_pc_map(conn, capacities)
    conn.close()
    return render_template(
        'admin_sitin.html',
        logs=logs,
        labs=lab_options(),
        booking_map=booking_map,
        logo=get_logo()
    )

@app.route('/admin/sitin/add', methods=['POST'])
@admin_required
def admin_sitin_add():
    id_num  = request.form.get('id_number','').strip()
    purpose = request.form.get('purpose','').strip()
    lab     = normalize_lab_room(request.form.get('lab',''))
    next_url = request.form.get('next', '').strip()
    if not next_url.startswith('/'):
        next_url = ''
    conn    = get_db()
    capacities = lab_capacity_map(conn)
    pc      = normalize_pc_number(request.form.get('pc_number',''), capacities.get(lab, PC_COUNT))
    user    = conn.execute("SELECT * FROM users WHERE id_number=?", (id_num,)).fetchone()
    if not user:
        flash('Student not found.', 'error')
    elif user['sessions_remaining'] <= 0:
        flash('No sessions remaining.', 'error')
    elif not lab:
        flash('Please choose a valid laboratory.', 'error')
    elif not pc:
        flash('Please choose a valid PC.', 'error')
    elif not pc_is_available(conn, lab, pc, capacities=capacities):
        flash(f'Lab {lab} PC {pc} is already reserved or in use.', 'error')
    else:
        conn.execute(
            "INSERT INTO sit_in_logs (user_id,lab_room,pc_number,purpose,status,source) VALUES (?,?,?,?, 'Pending', 'admin')",
            (user['id'], lab, pc, purpose)
        )
        conn.commit()
        flash('Sit-in request submitted for admin approval.', 'success')
    conn.close()
    return redirect(next_url or request.referrer or url_for('admin_dashboard'))

@app.route('/admin/sitin/timeout/<int:lid>', methods=['POST'])
@admin_required
def admin_sitin_timeout(lid):
    conn = get_db()
    log = conn.execute("SELECT * FROM sit_in_logs WHERE id=?", (lid,)).fetchone()
    if log:
        conn.execute("UPDATE sit_in_logs SET time_out=CURRENT_TIMESTAMP,status='Done' WHERE id=?", (lid,))
        create_notification(
            log['user_id'],
            'Sit-in session completed',
            f"Your {log['purpose'] or 'laboratory'} session has been marked complete.",
            'success',
            conn
        )
    conn.commit()
    conn.close()
    flash('Timed out.', 'success')
    return redirect(url_for('admin_sitin'))

@app.route('/admin/sitin/delete/<int:lid>', methods=['POST'])
@admin_required
def admin_sitin_delete(lid):
    conn = get_db()
    delete_sitin_log_dependents(conn, lid)
    conn.execute("DELETE FROM sit_in_logs WHERE id=?", (lid,))
    conn.commit()
    conn.close()
    flash('Record deleted.', 'success')
    return redirect(url_for('admin_sitin'))

# ─── STUDENT HISTORY ──────────────────────────────────────
@app.route('/history')
@login_required
def student_history():
    search   = request.args.get('search','').strip()
    page     = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 10))
    offset   = (page - 1) * per_page
    conn     = get_db()
    current_session = conn.execute('''
        SELECT s.lab_room, s.pc_number, s.status, s.time_in, s.time_out
        FROM sit_in_logs s
        WHERE s.user_id=?
          AND s.status IN ('Pending', 'Approved', 'Active')
          AND COALESCE(s.source, 'admin') != 'login'
        ORDER BY s.time_in DESC
        LIMIT 1
    ''', (session['user_id'],)).fetchone()
    summary_data = conn.execute('''
        SELECT 
            COALESCE(SUM(CASE WHEN time_out IS NOT NULL AND julianday(time_out) >= julianday(time_in) THEN 1 ELSE 0 END), 0) as total_sessions,
            COALESCE(SUM(CASE WHEN status='Done' AND time_out IS NOT NULL AND julianday(time_out) >= julianday(time_in) THEN 1 ELSE 0 END), 0) as completed_sessions,
            COALESCE(SUM(
                CASE 
                    WHEN time_out IS NOT NULL AND julianday(time_out) >= julianday(time_in)
                    THEN (julianday(time_out) - julianday(time_in)) * 24.0
                    ELSE 0
                END
            ), 0) as total_hours,
            COALESCE(AVG(
                CASE 
                    WHEN time_out IS NOT NULL AND julianday(time_out) >= julianday(time_in) AND status='Done'
                    THEN (julianday(time_out) - julianday(time_in)) * 24.0
                    ELSE NULL
                END
            ), 0) as avg_duration
        FROM sit_in_logs
        WHERE user_id=? AND status='Done' AND COALESCE(source, 'admin') != 'login'
    ''', (session['user_id'],)).fetchone()
    longest_session = conn.execute('''
        SELECT MAX((julianday(time_out) - julianday(time_in)) * 24.0) as longest_hours
        FROM sit_in_logs
        WHERE user_id=? AND time_out IS NOT NULL AND julianday(time_out) >= julianday(time_in) AND status='Done' AND COALESCE(source, 'admin') != 'login'
    ''', (session['user_id'],)).fetchone()
    sit_in_summary = {
        'total_hours': round(summary_data['total_hours'] or 0, 2),
        'total_sessions': summary_data['total_sessions'] or 0,
        'completed_sessions': summary_data['completed_sessions'] or 0,
        'avg_duration': round(summary_data['avg_duration'] or 0, 2),
        'longest_session': round(longest_session['longest_hours'] or 0, 2) if longest_session['longest_hours'] else 0
    }
    base_q   = '''SELECT s.*, u.id_number, u.first_name, u.last_name,
                         COALESCE(f.rating, 0) AS rating, COALESCE(f.feedback_text, '') AS feedback_text
                  FROM sit_in_logs s JOIN users u ON s.user_id=u.id
                  LEFT JOIN feedback f ON f.sit_in_log_id = s.id AND f.user_id = s.user_id
                  WHERE s.user_id=? AND COALESCE(s.source, 'admin') != 'login' '''
    params   = [session['user_id']]
    if search:
        base_q += ' AND (u.id_number LIKE ? OR u.first_name LIKE ? OR u.last_name LIKE ? OR s.purpose LIKE ? OR s.lab_room LIKE ? OR s.pc_number LIKE ?)'
        params += [f'%{search}%']*6
    total    = conn.execute(f'SELECT COUNT(*) FROM ({base_q})', params).fetchone()[0]
    logs     = [dict(r) for r in conn.execute(base_q + ' ORDER BY s.time_in DESC LIMIT ? OFFSET ?', params + [per_page, offset]).fetchall()]
    notifications = dashboard_notifications(conn, session['user_id'])
    conn.close()
    total_pages = max(1, -(-total // per_page))
    return render_template('history.html', logs=logs, page=page, per_page=per_page,
                           total=total, total_pages=total_pages, search=search,
                           notifications=notifications, logo=get_logo(),
                           current_session=current_session, sit_in_summary=sit_in_summary)

@app.route('/history/delete/<int:lid>', methods=['POST'])
@login_required
def student_delete_history(lid):
    conn = get_db()
    delete_sitin_log_dependents(conn, lid)
    conn.execute('DELETE FROM sit_in_logs WHERE id=? AND user_id=?', (lid, session['user_id']))
    conn.commit()
    conn.close()
    flash('Record deleted.', 'success')
    return redirect(url_for('student_history'))

@app.route('/history/feedback/<int:lid>', methods=['POST'])
@login_required
def submit_history_feedback(lid):
    rating = request.form.get('rating', '0').strip()
    feedback_text = request.form.get('feedback_text', '').strip()
    try:
        rating = max(1, min(5, int(rating)))
    except (TypeError, ValueError):
        rating = 0

    conn = get_db()
    log = conn.execute(
        "SELECT * FROM sit_in_logs WHERE id=? AND user_id=? AND status='Done'",
        (lid, session['user_id'])
    ).fetchone()
    if not log:
        conn.close()
        flash('Only completed sit-in sessions can receive feedback.', 'error')
        return redirect(url_for('student_history'))

    conn.execute(
        '''INSERT INTO feedback (user_id, sit_in_log_id, rating, feedback_text)
           VALUES (?,?,?,?)
           ON CONFLICT(user_id, sit_in_log_id)
           DO UPDATE SET rating=excluded.rating, feedback_text=excluded.feedback_text,
                         created_at=CURRENT_TIMESTAMP''',
        (session['user_id'], lid, rating, feedback_text)
    )
    conn.commit()
    conn.close()
    flash('Feedback saved.', 'success')
    return redirect(url_for('student_history'))

@app.route('/reservation', methods=['GET','POST'])
@login_required
def student_reservation():
    conn = get_db()
    reservation_open = reservations_enabled(conn)
    user = conn.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
    labs_availability = enabled_lab_options(conn)
    lab_capacities = {lab['lab_room']: max(1, int(lab['capacity'] or PC_COUNT)) for lab in labs_availability}
    seen_labs = set()
    labs = []
    for lab in labs_availability:
        lab_room = lab['lab_room']
        if lab_room and lab_room not in seen_labs:
            seen_labs.add(lab_room)
            labs.append(lab_room)
    booking_map = build_lab_pc_map(conn, lab_capacities)
    raw_sw = [dict(r) for r in conn.execute('''
        SELECT name, category, available_labs
        FROM software
        WHERE is_enabled=1
        ORDER BY category, name
    ''').fetchall()]

    # Merge duplicate software records by name to avoid repeated chips on reservation page
    sw_merged = {}
    sw_order = []
    for sw in raw_sw:
        name = (sw.get('name') or '').strip()
        key = name.lower()
        raw_labs = (sw.get('available_labs') or '').replace(';', ',')
        labs = [l.strip() for l in raw_labs.split(',') if l.strip()]
        if key in sw_merged:
            existing = sw_merged[key]
            existing_labs = [l.strip() for l in (existing.get('available_labs') or '').split(',') if l.strip()]
            combined = list(dict.fromkeys(existing_labs + labs))
            existing['available_labs'] = ','.join(combined)
        else:
            labs = list(dict.fromkeys(labs))
            sw['available_labs'] = ','.join(labs)
            sw_merged[key] = sw
            sw_order.append(key)

    software_list = [sw_merged[k] for k in sw_order]
    current_active = conn.execute('''
        SELECT lab_room, pc_number, status
        FROM sit_in_logs
        WHERE user_id=?
          AND status='Active'
          AND COALESCE(source, 'admin') != 'login'
        ORDER BY time_in DESC
        LIMIT 1
    ''', (session['user_id'],)).fetchone()
    selected_lab = ''
    selected_pc = ''
    if request.method == 'POST':
        if not reservation_open:
            flash('Reservations are currently disabled by the administrator. You can still view the reservation page.', 'error')
        else:
            purpose  = request.form.get('purpose','').strip()
            lab      = normalize_lab_room(request.form.get('lab',''))
            pc       = normalize_pc_number(request.form.get('pc_number',''), lab_capacities.get(lab, PC_COUNT))
            time_in  = request.form.get('time_in','').strip()
            date     = request.form.get('date','').strip()
            selected_lab = lab
            selected_pc = pc
            if not purpose or not lab or not pc or not time_in or not date:
                flash('Please complete all reservation fields.', 'error')
            elif not pc_is_available(conn, lab, pc, capacities=lab_capacities):
                flash(f'Lab {lab} PC {pc} is already reserved or in use.', 'error')
            else:
                try:
                    requested_datetime = datetime.strptime(f"{date} {time_in}", '%Y-%m-%d %H:%M')
                    now = datetime.now().replace(second=0, microsecond=0)
                    if requested_datetime < now:
                        flash('Please select a present or future date/time for your reservation.', 'error')
                    else:
                        time_in_dt = requested_datetime.strftime('%Y-%m-%d %H:%M:%S')
                        # include selected software in the reservation record
                        selected_software = request.form.get('software','').strip()
                        conn.execute(
                            "INSERT INTO sit_in_logs (user_id,purpose,lab_room,pc_number,time_in,status,source,software) VALUES (?,?,?,?,?,'Pending','student',?)",
                            (user['id'], purpose, lab, pc, time_in_dt, selected_software)
                        )
                        conn.execute(
                            "UPDATE users SET sessions_remaining=sessions_remaining-1, reward_points=reward_points+5 WHERE id=? AND sessions_remaining > 0",
                            (user['id'],)
                        )
                        extra_sessions, remaining_points = convert_reservation_points(user['id'], conn)
                        create_notification(
                            user['id'],
                            'Reservation submitted',
                            f'Your reservation for Lab {lab}, PC {pc} is pending admin approval.',
                            'info',
                            conn
                        )
                        conn.commit()
                        if extra_sessions > 0:
                            flash(f'Reservation submitted successfully! You earned {extra_sessions} session(s) from points and now have {remaining_points} point(s) remaining.', 'success')
                        else:
                            flash('Reservation submitted successfully! You earned 1 point.', 'success')
                        conn.close()
                        return redirect(url_for('student_reservation'))
                except ValueError:
                    flash('Please enter a valid date and time for reservation.', 'error')
                conn.execute("UPDATE users SET reward_points=reward_points+5 WHERE id=?", (user['id'],))
                extra_sessions, remaining_points = convert_reservation_points(user['id'], conn)
                create_notification(
                    user['id'],
                    'Reservation submitted',
                    f'Your reservation for Lab {lab}, PC {pc} is pending admin approval.',
                    'info',
                    conn
                )
                conn.commit()
                if extra_sessions > 0:
                    flash(f'Reservation submitted successfully! You earned {extra_sessions} session(s) from points and now have {remaining_points} point(s) remaining.', 'success')
                else:
                    flash('Reservation submitted successfully! You earned 1 point.', 'success')
                conn.close()
                return redirect(url_for('student_reservation'))
    notifications = dashboard_notifications(conn, session['user_id'])
    conn.close()
    return render_template(
        'reservation.html',
        user=user,
        labs=labs,
        booking_map=booking_map,
        lab_capacities=lab_capacities,
        current_active_lab=current_active['lab_room'] if current_active else '',
        current_active_pc=current_active['pc_number'] if current_active else '',
        selected_lab=selected_lab,
        selected_pc=selected_pc,
        reservation_open=reservation_open,
        notifications=notifications,
        software_list=software_list,
        logo=get_logo()
    )

# ─── ADMIN RESERVATIONS ──────────────────────────────────
@app.route('/admin/reservations')
@admin_required
def admin_reservations():
    conn = get_db()
    reservations = [dict(r) for r in conn.execute('''
        SELECT s.*, u.id_number, u.first_name, u.last_name, u.course,
               u.sessions_remaining, u.reservation_points, u.reward_points
        FROM sit_in_logs s JOIN users u ON s.user_id=u.id
        WHERE s.status = 'Pending' AND COALESCE(s.source, 'admin') != 'login'
        ORDER BY s.time_in DESC
    ''').fetchall()]
    conn.close()
    return render_template('admin_reservations.html', reservations=reservations, logo=get_logo())

@app.route('/admin/reservations/approve/<int:rid>', methods=['POST'])
@admin_required
def admin_reservation_approve(rid):
    conn = get_db()
    reasoning = request.form.get('reasoning', '').strip() or 'Reservation approved and activated after admin review.'
    log = conn.execute("SELECT * FROM sit_in_logs WHERE id=? AND status='Pending'", (rid,)).fetchone()
    if log:
        conn.execute(
            "UPDATE sit_in_logs SET status='Active', request_reason=? WHERE id=?",
            (reasoning, rid)
        )
        conn.execute(
            "UPDATE users SET sessions_remaining=CASE WHEN sessions_remaining >= 1 THEN sessions_remaining-1 ELSE 0 END WHERE id=?",
            (log['user_id'],)
        )
        log_reasoning(session['user_id'], log['user_id'], rid, 'approve', reasoning, conn)
        create_notification(
            log['user_id'],
            'Reservation active',
            reasoning,
            'success',
            conn
        )
    conn.commit()
    conn.close()
    flash('Reservation activated.', 'success')
    return redirect(url_for('admin_reservations'))

@app.route('/admin/reservations/deny/<int:rid>', methods=['POST'])
@admin_required
def admin_reservation_deny(rid):
    conn = get_db()
    reasoning = request.form.get('reasoning', '').strip() or 'Reservation denied after admin review.'
    log = conn.execute("SELECT * FROM sit_in_logs WHERE id=? AND status='Pending'", (rid,)).fetchone()
    if log:
        conn.execute(
            "UPDATE sit_in_logs SET status='Denied', time_out=CURRENT_TIMESTAMP, request_reason=? WHERE id=?",
            (reasoning, rid)
        )
        log_reasoning(session['user_id'], log['user_id'], rid, 'deny', reasoning, conn)
        create_notification(
            log['user_id'],
            'Reservation denied',
            reasoning,
            'error',
            conn
        )
    conn.commit()
    conn.close()
    flash('Reservation denied.', 'success')
    return redirect(url_for('admin_reservations'))

@app.route('/admin/reservations/timeout/<int:rid>', methods=['POST'])
@admin_required
def admin_reservation_timeout(rid):
    conn = get_db()
    log = conn.execute("SELECT * FROM sit_in_logs WHERE id=?", (rid,)).fetchone()
    if log:
        conn.execute("UPDATE sit_in_logs SET time_out=CURRENT_TIMESTAMP, status='Done' WHERE id=?", (rid,))
        create_notification(
            log['user_id'],
            'Reservation completed',
            'Your approved reservation has been marked as completed.',
            'success',
            conn
        )
    conn.commit()
    conn.close()
    flash('Reservation timed out.', 'success')
    return redirect(url_for('admin_reservations'))

@app.route('/admin/reservations/delete/<int:rid>', methods=['POST'])
@admin_required
def admin_reservation_delete(rid):
    conn = get_db()
    delete_sitin_log_dependents(conn, rid)
    conn.execute('DELETE FROM sit_in_logs WHERE id=?', (rid,))
    conn.commit()
    conn.close()
    flash('Reservation deleted.', 'success')
    return redirect(url_for('admin_reservations'))

# ─── ADMIN SIT-IN RECORDS ─────────────────────────────────
@app.route('/admin/sitin/records')
@admin_required
def admin_sitin_records():
    search   = request.args.get('search', '').strip()
    page     = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 25))
    offset   = (page - 1) * per_page
    conn     = get_db()
    count_q = '''SELECT COUNT(*) FROM sit_in_logs s JOIN users u ON s.user_id=u.id WHERE 1=1'''
    params = []
    count_q += " AND COALESCE(s.source, 'admin') != 'login'"
    if search:
        count_q += ' AND (u.id_number LIKE ? OR u.first_name LIKE ? OR u.last_name LIKE ? OR s.purpose LIKE ? OR s.lab_room LIKE ? OR s.pc_number LIKE ?)'
        params = [f'%{search}%'] * 6
    total = conn.execute(count_q, params).fetchone()[0]
    logs_q = '''SELECT s.*, u.id_number, u.first_name, u.last_name, u.course
                FROM sit_in_logs s JOIN users u ON s.user_id=u.id WHERE 1=1'''
    logs_q += " AND COALESCE(s.source, 'admin') != 'login'"
    if search:
        logs_q += ' AND (u.id_number LIKE ? OR u.first_name LIKE ? OR u.last_name LIKE ? OR s.purpose LIKE ? OR s.lab_room LIKE ? OR s.pc_number LIKE ?)'
    logs_q += ' ORDER BY s.time_in DESC LIMIT ? OFFSET ?'
    logs = [dict(r) for r in conn.execute(logs_q, params + [per_page, offset]).fetchall()]
    conn.close()
    total_pages = max(1, -(-total // per_page))
    return render_template('admin_sitin_records.html', logs=logs, page=page, per_page=per_page,
                           total=total, total_pages=total_pages, search=search, logo=get_logo())

# ─── ADMIN SIT-IN REPORTS ─────────────────────────────────
@app.route('/admin/sitin/reports')
@admin_required
def admin_sitin_reports():
    conn = get_db()
    leaderboard = leaderboard_entries(conn, limit=10)
    stats = {
        'total':  conn.execute("SELECT COUNT(*) FROM sit_in_logs WHERE COALESCE(source, 'admin') != 'login'").fetchone()[0],
        'active': conn.execute("SELECT COUNT(*) FROM sit_in_logs WHERE status='Active' AND COALESCE(source, 'admin') != 'login'").fetchone()[0],
        'done':   conn.execute("SELECT COUNT(*) FROM sit_in_logs WHERE status='Done' AND COALESCE(source, 'admin') != 'login'").fetchone()[0],
        'today':  conn.execute("SELECT COUNT(*) FROM sit_in_logs WHERE DATE(time_in)=DATE('now') AND COALESCE(source, 'admin') != 'login'").fetchone()[0],
    }
    by_course = [dict(r) for r in conn.execute('''
        SELECT u.course, COUNT(*) as total,
               SUM(CASE WHEN s.status='Active' THEN 1 ELSE 0 END) as active,
               SUM(CASE WHEN s.status='Done'   THEN 1 ELSE 0 END) as done
        FROM sit_in_logs s JOIN users u ON s.user_id=u.id
        WHERE COALESCE(s.source, 'admin') != 'login'
        GROUP BY u.course ORDER BY total DESC
    ''').fetchall()]
    by_lab = [dict(r) for r in conn.execute('''
        SELECT lab_room, COUNT(*) as total,
               SUM(CASE WHEN status='Active' THEN 1 ELSE 0 END) as active,
               SUM(CASE WHEN status='Done'   THEN 1 ELSE 0 END) as done
        FROM sit_in_logs
        WHERE COALESCE(source, 'admin') != 'login'
        GROUP BY lab_room ORDER BY total DESC
    ''').fetchall()]
    by_purpose = [dict(r) for r in conn.execute('''
        SELECT purpose, COUNT(*) as total FROM sit_in_logs
        WHERE COALESCE(source, 'admin') != 'login'
        GROUP BY purpose ORDER BY total DESC LIMIT 10
    ''').fetchall()]
    trend = [dict(r) for r in conn.execute('''
        SELECT DATE(time_in) as day, COUNT(*) as cnt
        FROM sit_in_logs
        WHERE time_in >= DATE('now', '-14 days') AND COALESCE(source, 'admin') != 'login'
        GROUP BY DATE(time_in) ORDER BY day
    ''').fetchall()]
    recent = [dict(r) for r in conn.execute('''
        SELECT s.id, u.id_number, u.id_number as name, s.lab_room, s.pc_number, s.purpose, s.time_in, s.time_out, s.status
        FROM sit_in_logs s JOIN users u ON s.user_id=u.id
        WHERE COALESCE(s.source, 'admin') != 'login'
        ORDER BY s.time_in DESC LIMIT 20
    ''').fetchall()]
    conn.close()
    return render_template('admin_sitin_reports.html', stats=stats, by_course=by_course,
                           by_lab=by_lab, by_purpose=by_purpose, trend=trend,
                           recent=recent, leaderboard=leaderboard, logo=get_logo())

@app.route('/admin/feedback-reports')
@admin_required
def admin_feedback_reports():
    conn = get_db()
    feedback_rows = [dict(r) for r in conn.execute('''
        SELECT f.*, u.id_number, u.first_name, u.last_name, s.purpose, s.lab_room
        FROM feedback f
        JOIN users u ON f.user_id=u.id
        JOIN sit_in_logs s ON f.sit_in_log_id=s.id
        ORDER BY f.created_at DESC
    ''').fetchall()]
    stats = {
        'total_feedback': len(feedback_rows),
        'avg_rating': round(conn.execute("SELECT COALESCE(AVG(rating), 0) FROM feedback").fetchone()[0] or 0, 2),
        'with_comments': conn.execute("SELECT COUNT(*) FROM feedback WHERE TRIM(feedback_text) != ''").fetchone()[0],
    }
    conn.close()
    return render_template('admin_feedback_reports.html', feedback_rows=feedback_rows, stats=stats, logo=get_logo())


# Export sit-in reports (CSV / JSON)
@app.route('/admin/sitin/reports/export')
@admin_required
def admin_sitin_reports_export():
    # Always return PDF for exports
    conn = get_db()
    rows = [dict(r) for r in conn.execute('''
        SELECT s.id, u.id_number, u.first_name || ' ' || u.last_name AS name,
               u.course, s.lab_room, s.pc_number, s.purpose, s.time_in, s.time_out, s.status
        FROM sit_in_logs s JOIN users u ON s.user_id=u.id
        WHERE COALESCE(s.source, 'admin') != 'login'
        ORDER BY s.time_in DESC
    ''').fetchall()]
    conn.close()

    # build a PDF that matches the provided report structure
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(letter), leftMargin=28, rightMargin=28, topMargin=28, bottomMargin=28)
    styles = getSampleStyleSheet()
    elems = []

    # Top small timestamp
    now_str = datetime.now().strftime('%d/%m/%Y, %H:%M')
    ts_style = ParagraphStyle('ts', parent=styles['Normal'], fontSize=8, alignment=0, textColor=colors.HexColor('#555555'))
    elems.append(Paragraph(now_str, ts_style))
    elems.append(Spacer(1, 6))

    # Organization and title
    org_style = ParagraphStyle('org', parent=styles['Normal'], fontSize=10, alignment=1, textColor=colors.HexColor('#333333'))
    elems.append(Paragraph('University of Cebu - Main Campus System', org_style))
    elems.append(Spacer(1, 4))
    title_style = ParagraphStyle('title', parent=styles['Heading1'], fontSize=18, alignment=1, textColor=colors.HexColor('#333333'))
    elems.append(Paragraph('College Of Computer Studies Reports', title_style))
    elems.append(Spacer(1, 12))

    # Table header matching screenshot structure
    header_keys = ['id_number', 'name', 'purpose', 'lab_room', 'time_in', 'time_out']
    data = [['ID Number', 'Name', 'Purpose', 'Laboratory', 'Login', 'Logout', 'Date']]

    for r in rows:
        raw_login = r.get('time_in') or ''
        raw_logout = r.get('time_out') or ''
        login = ''
        logout = ''
        date = ''
        # normalize timestamps: try ISO then common formats
        def parse_ts(val):
            if not val:
                return None
            s = str(val)
            for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
                try:
                    return datetime.strptime(s, fmt)
                except Exception:
                    pass
            try:
                return datetime.fromisoformat(s)
            except Exception:
                return None

        dt_login = parse_ts(raw_login)
        dt_logout = parse_ts(raw_logout)
        if dt_login:
            login = dt_login.strftime('%H:%M:%S')
            date = dt_login.date().isoformat()
        else:
            # if raw_login exists but couldn't parse, use short string
            login = str(raw_login)
            if not date and raw_login:
                date = str(raw_login).split(' ')[0]
        if dt_logout:
            logout = dt_logout.strftime('%H:%M:%S')
        else:
            logout = str(raw_logout)

        # Name
        name = r.get('name') or ''
        # Purpose and lab
        purpose = r.get('purpose') or ''
        lab = r.get('lab_room') or ''
        # ID number
        idnum = r.get('id_number') or ''
        data.append([str(idnum), str(name), str(purpose), str(lab), str(login), str(logout), str(date)])

    # Total records and status breakdown (displayed below the table)
    total_records = len(rows)
    completed_count = sum(1 for r in rows if (r.get('status') or '').lower() in ('done','completed'))
    active_count = sum(1 for r in rows if (r.get('status') or '').lower() == 'active')

    # Column widths tuned for landscape letter
    colWidths = [80, 150, 160, 70, 95, 95, 70]
    table = Table(data, repeatRows=1, colWidths=colWidths)
    table.setStyle(TableStyle([
        # header: light background with dark text
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#f7f7f7')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.HexColor('#1e2535')),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,0), 10),
        ('ALIGN', (0,0), (0,-1), 'CENTER'),
        ('ALIGN', (4,0), (5,-1), 'CENTER'),
        ('ALIGN', (6,0), (6,-1), 'CENTER'),
        ('ALIGN', (1,0), (3,-1), 'LEFT'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        # black grid lines and outer box
        ('GRID', (0,0), (-1,-1), 0.8, colors.HexColor('#000000')),
        ('BOX', (0,0), (-1,-1), 0.8, colors.HexColor('#000000')),
        ('BOTTOMPADDING', (0,0), (-1,0), 10),
        ('TOPPADDING', (0,0), (-1,0), 10),
        ('FONTSIZE', (0,1), (-1,-1), 9),
        ('TEXTCOLOR', (0,1), (-1,-1), colors.HexColor('#1e2535')),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('RIGHTPADDING', (0,0), (-1,-1), 6),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.whitesmoke])
    ]))

    elems.append(table)
    # Add totals line immediately below the table (closer to table like the screenshot)
    summary_style = ParagraphStyle('summary', parent=styles['Normal'], fontSize=9, alignment=0, textColor=colors.HexColor('#333333'))
    summary_text = f'Total records: {total_records} | Completed: {completed_count} | Active: {active_count}'
    elems.append(Spacer(1, 8))
    elems.append(Paragraph(summary_text, summary_style))

    # Footer callback to only set PDF metadata (no on-page text)
    def _footer(canvas, docu):
        canvas.saveState()
        # Only set PDF metadata in footer; do not draw a page background
        try:
            canvas.setAuthor('University of Cebu - Main Campus System')
            canvas.setTitle('Sit-in Reports')
            canvas.setCreator('CCS Monitoring System')
        except Exception:
            pass
        canvas.restoreState()

    doc.build(elems, onFirstPage=_footer, onLaterPages=_footer)
    pdf = buf.getvalue()
    buf.close()
    return Response(pdf, mimetype='application/pdf', headers={
        'Content-Disposition': 'attachment; filename="sitin_reports.pdf"'
    })

    # default: CSV
    si = io.StringIO()
    writer = csv.writer(si)
    header = ['id','id_number','name','course','lab_room','pc_number','purpose','time_in','time_out','status']
    writer.writerow(header)
    for r in rows:
        writer.writerow([r.get(h, '') for h in header])
    output = si.getvalue().encode('utf-8')
    return Response(output, mimetype='text/csv', headers={
        'Content-Disposition': 'attachment; filename="sitin_reports.csv"'
    })


# Export feedback reports (CSV / JSON)
@app.route('/admin/feedback-reports/export')
@admin_required
def admin_feedback_reports_export():
    # Always return PDF for exports
    conn = get_db()
    rows = [dict(r) for r in conn.execute('''
        SELECT f.id, u.id_number, u.first_name || ' ' || u.last_name AS name,
               s.purpose, s.lab_room, f.rating, f.feedback_text, f.created_at
        FROM feedback f
        JOIN users u ON f.user_id=u.id
        JOIN sit_in_logs s ON f.sit_in_log_id=s.id
        ORDER BY f.created_at DESC
    ''').fetchall()]
    conn.close()

    # build a simple table PDF
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(letter), leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24)
    styles = getSampleStyleSheet()
    elems = []
    elems.append(Paragraph('Feedback Reports', styles['Heading2']))
    elems.append(Spacer(1, 8))
    header_keys = ['id','id_number','name','purpose','lab_room','rating','feedback_text','created_at']
    data = [['ID','ID Number','Student','Purpose','Lab','Rating','Feedback','Created']]
    for r in rows:
        data.append([str(r.get(k, '') or '') for k in header_keys])
    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#2b6cb0')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('GRID', (0,0), (-1,-1), 0.25, colors.grey),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    elems.append(table)
    doc.build(elems)
    pdf = buf.getvalue()
    buf.close()
    return Response(pdf, mimetype='application/pdf', headers={
        'Content-Disposition': 'attachment; filename="feedback_reports.pdf"'
    })

    si = io.StringIO()
    writer = csv.writer(si)
    header = ['id','id_number','name','purpose','lab_room','rating','feedback_text','created_at']
    writer.writerow(header)
    for r in rows:
        writer.writerow([r.get(h, '') for h in header])
    output = si.getvalue().encode('utf-8')
    return Response(output, mimetype='text/csv', headers={
        'Content-Disposition': 'attachment; filename="feedback_reports.csv"'
    })

# ─── ADMIN REPORT PREVIEW ─────────────────────────────────
@app.route('/admin/reports/pdf')
@admin_required
def admin_report_preview():
    conn = get_db()
    selected_course = request.args.get('course', '').strip()
    selected_lab    = request.args.get('lab', '').strip()
    selected_status = request.args.get('status', '').strip()
    date_from       = request.args.get('date_from', '').strip()
    date_to         = request.args.get('date_to', '').strip()

    query = '''
        SELECT s.id, u.id_number,
               u.first_name || ' ' || u.last_name AS name,
               u.course, s.lab_room, s.pc_number, s.purpose,
               s.time_in, s.time_out, s.status
        FROM sit_in_logs s
        JOIN users u ON s.user_id = u.id
        WHERE COALESCE(s.source, 'admin') != 'login'
    '''
    params = []
    if selected_course:
        query += ' AND u.course = ?'; params.append(selected_course)
    if selected_lab:
        query += ' AND s.lab_room = ?'; params.append(selected_lab)
    if selected_status:
        query += ' AND s.status = ?'; params.append(selected_status)
    if date_from:
        query += ' AND DATE(s.time_in) >= ?'; params.append(date_from)
    if date_to:
        query += ' AND DATE(s.time_in) <= ?'; params.append(date_to)
    query += ' ORDER BY s.time_in DESC'

    raw_rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    today_str = datetime.now().strftime('%Y-%m-%d')
    today_count = sum(1 for r in raw_rows if (r.get('time_in') or '').startswith(today_str))

    def fmt_ts(val, fmt='%Y-%m-%d %H:%M:%S'):
        if not val: return ''
        s = str(val)
        for f in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
            try: return datetime.strptime(s, f).strftime(fmt)
            except Exception: pass
        return s

    for r in raw_rows:
        r['login_display']  = fmt_ts(r.get('time_in'))
        r['logout_display'] = fmt_ts(r.get('time_out'))

    courses = [row['code'] for row in conn.execute('SELECT code FROM courses ORDER BY code').fetchall()]
    conn.close()

    now_str = datetime.now().strftime('%d/%m/%Y, %H:%M')
    return render_template(
        'admin_report_preview.html',
        rows=raw_rows,
        courses=courses,
        labs=LAB_ROOMS,
        selected_course=selected_course,
        selected_lab=selected_lab,
        selected_status=selected_status,
        date_from=date_from,
        date_to=date_to,
        today_count=today_count,
        now_str=now_str,
        logo=get_logo()
    )

@app.route('/admin/reports/csv')
@admin_required
def admin_report_csv():
    conn = get_db()
    selected_course = request.args.get('course', '').strip()
    selected_lab    = request.args.get('lab', '').strip()
    selected_status = request.args.get('status', '').strip()
    date_from       = request.args.get('date_from', '').strip()
    date_to         = request.args.get('date_to', '').strip()

    query = '''
        SELECT s.id, u.id_number,
               u.first_name || ' ' || u.last_name AS name,
               u.course, s.lab_room, s.pc_number, s.purpose,
               s.time_in, s.time_out, s.status
        FROM sit_in_logs s
        JOIN users u ON s.user_id = u.id
        WHERE COALESCE(s.source, 'admin') != 'login'
    '''
    params = []
    if selected_course:
        query += ' AND u.course = ?'; params.append(selected_course)
    if selected_lab:
        query += ' AND s.lab_room = ?'; params.append(selected_lab)
    if selected_status:
        query += ' AND s.status = ?'; params.append(selected_status)
    if date_from:
        query += ' AND DATE(s.time_in) >= ?'; params.append(date_from)
    if date_to:
        query += ' AND DATE(s.time_in) <= ?'; params.append(date_to)
    query += ' ORDER BY s.time_in DESC'

    rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    conn.close()

    si = io.StringIO()
    writer = csv.writer(si)
    writer.writerow(['#', 'ID Number', 'Name', 'Course', 'Lab', 'PC', 'Purpose', 'Login Time', 'Logout Time', 'Status'])
    for idx, r in enumerate(rows, 1):
        writer.writerow([
            idx,
            r.get('id_number', ''),
            r.get('name', ''),
            r.get('course', ''),
            r.get('lab_room', ''),
            r.get('pc_number', ''),
            r.get('purpose', ''),
            r.get('time_in', ''),
            r.get('time_out', ''),
            r.get('status', ''),
        ])
    output = si.getvalue().encode('utf-8-sig')  # utf-8-sig so Excel opens it correctly
    filename = f"sitin_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(output, mimetype='text/csv', headers={
        'Content-Disposition': f'attachment; filename="{filename}"'
    })

@app.route('/leaderboard')
@login_required
def leaderboard():
    conn = get_db()
    rankings = leaderboard_entries(conn)
    user = fetch_user(conn, session['user_id'])
    notifications = dashboard_notifications(conn, session['user_id'], limit=4) if not session.get('is_admin') else []
    conn.close()
    return render_template('leaderboard.html', rankings=rankings, user=user, notifications=notifications, logo=get_logo())

# ─── STUDENT SOFTWARE PAGE ────────────────────────────────
@app.route('/lab-software')
@login_required
def student_software():
    conn = get_db()
    raw_software = [dict(r) for r in conn.execute('''
        SELECT id, name, description, category, available_labs
        FROM software
        WHERE is_enabled=1
        ORDER BY category, name
    ''').fetchall()]

    # Merge duplicate software records by (name, category) to avoid repeated cards
    merged = {}
    order = []
    for sw in raw_software:
        name = (sw.get('name') or '').strip()
        # Deduplicate by name only for a cleaner UI (merge different categories of same name)
        key = name.lower()
        # normalize lab list for this row
        raw_labs = (sw.get('available_labs') or '').replace(';', ',')
        labs = [l.strip() for l in raw_labs.split(',') if l.strip()]
        if key in merged:
            existing = merged[key]
            # merge labs (preserve order)
            existing_labs = [l.strip() for l in (existing.get('available_labs') or '').split(',') if l.strip()]
            combined = list(dict.fromkeys(existing_labs + labs))
            existing['available_labs'] = ','.join(combined)
            # prefer an existing description if present, otherwise take this one
            if not existing.get('description') and sw.get('description'):
                existing['description'] = sw.get('description')
        else:
            # normalize labs and store a shallow copy
            labs = list(dict.fromkeys(labs))
            sw['available_labs'] = ','.join(labs)
            merged[key] = sw
            order.append(key)

    all_software = [merged[k] for k in order]

    # Precompute lab arrays and visible lab chips for template simplicity
    for sw in all_software:
        raw = (sw.get('available_labs') or '')
        lab_list = [l.strip() for l in raw.replace(';', ',').split(',') if l.strip()]
        # remove duplicates but preserve order
        lab_list = list(dict.fromkeys(lab_list))
        sw['_labs_list'] = lab_list
        sw['_show_labs'] = lab_list[:3]
        sw['_extra_labs'] = max(0, len(lab_list) - len(sw['_show_labs']))
    labs = LAB_ROOMS

    # Group software by lab from merged list
    software_by_lab = {lab: [] for lab in labs}
    for sw in all_software:
        raw = sw.get('available_labs') or ''
        lab_list = [l.strip() for l in raw.replace(';', ',').split(',') if l.strip()]
        lab_list = list(dict.fromkeys(lab_list))
        for lab in lab_list:
            if lab in software_by_lab:
                software_by_lab[lab].append(sw)

    # Unique categories from merged software
    categories = sorted({(sw.get('category') or 'General') for sw in all_software if sw.get('category')})

    notifications = dashboard_notifications(conn, session['user_id'])
    conn.close()
    return render_template(
        'student_software.html',
        labs=labs,
        all_software=all_software,
        software_by_lab=software_by_lab,
        categories=categories,
        notifications=notifications,
        logo=get_logo()
    )

# ─── SOFTWARE & LAB AVAILABILITY ───────────────────────────
@app.route('/api/software')
@login_required
def api_get_software():
    conn = get_db()
    software = [dict(r) for r in conn.execute('''
        SELECT id, name, description, category, available_labs FROM software WHERE is_enabled=1
    ''').fetchall()]
    conn.close()
    return jsonify(software)

@app.route('/api/labs/availability')
@login_required
def api_get_labs_availability():
    conn = get_db()
    labs = [dict(r) for r in conn.execute('''
        SELECT lab_room, is_enabled, capacity, description FROM lab_availability WHERE is_enabled=1
    ''').fetchall()]
    conn.close()
    return jsonify(labs)

@app.route('/admin/software')
@admin_required
def admin_software():
    conn = get_db()
    software = [dict(r) for r in conn.execute('''
        SELECT * FROM software ORDER BY created_at DESC
    ''').fetchall()]
    conn.close()
    # Compute unique software counts per lab (count unique software names)
    lab_counts = {str(l): 0 for l in LAB_ROOMS}
    lab_sets = {str(l): set() for l in LAB_ROOMS}
    for sw in software:
        labs_csv = sw.get('available_labs') or ''
        name = (sw.get('name') or '').strip().lower()
        if not name:
            continue
        for lab in [s.strip() for s in labs_csv.split(',') if s.strip()]:
            if lab in lab_sets:
                lab_sets[lab].add(name)
    for lab in lab_sets:
        lab_counts[lab] = len(lab_sets[lab])

    return render_template('admin_software.html', software=software, labs=LAB_ROOMS, lab_counts=lab_counts, logo=get_logo())

@app.route('/admin/software/import', methods=['POST'])
@admin_required
def admin_import_software():
    # Support two modes:
    # 1) File import via `software_file` (CSV/JSON/ZIP)
    # 2) Manual single-item registration via form fields
    upload = request.files.get('software_file')

    # If a file was uploaded and has content, treat as import
    if upload and upload.filename:
        selected_labs = request.form.getlist('available_labs[]')
        if not selected_labs:
            selected_labs = LAB_ROOMS

        payload = upload.read()
        if not payload:
            flash('The uploaded file is empty.', 'error')
            return redirect(request.referrer or url_for('admin_software'))
        if len(payload) > MAX_SOFTWARE_IMPORT_BYTES:
            flash('The uploaded file is too large. Please use a file under 5 MB.', 'error')
            return redirect(request.referrer or url_for('admin_software'))

        try:
            records = parse_software_import_payload(payload)
        except Exception:
            records = []
        if not records:
            records = [fallback_software_record_from_file(upload.filename, default_labs=selected_labs)]

        conn = get_db()
        imported, skipped = import_software_records(records, conn, default_labs=selected_labs)
        conn.commit()
        conn.close()

        if imported:
            flash(f'Imported {imported} software item(s). Skipped {skipped} duplicate or invalid item(s).', 'success')
        else:
            flash(f'No new software was imported. Skipped {skipped} duplicate or invalid item(s).', 'info')
        return redirect(request.referrer or url_for('admin_software'))

    # Otherwise, allow manual registration from form fields
    name = request.form.get('software_name', '').strip()
    if not name:
        flash('Choose a file to import or provide a software name.', 'error')
        return redirect(request.referrer or url_for('admin_software'))

    category = request.form.get('software_category', 'General').strip() or 'General'
    description = request.form.get('software_description', '').strip()
    selected_labs = request.form.getlist('available_labs[]')
    if not selected_labs:
        selected_labs = LAB_ROOMS

    available_labs = normalize_software_labs(selected_labs)

    conn = get_db()
    if software_exists(conn, name, category):
        flash('A software with the same name and category already exists.', 'info')
    else:
        conn.execute('''
            INSERT INTO software (name, description, category, available_labs, is_enabled)
            VALUES (?, ?, ?, ?, 1)
        ''', (name, description, category, available_labs))
        conn.commit()
        flash('Software registered successfully.', 'success')
    conn.close()
    return redirect(request.referrer or url_for('admin_software'))

@app.route('/admin/software/edit/<int:sid>', methods=['POST'])
@admin_required
def admin_edit_software(sid):
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    category = request.form.get('category', 'General').strip()
    available_labs = ','.join(request.form.getlist('available_labs[]'))
    is_enabled = parse_boolish(request.form.get('is_enabled'), default=0)
    
    if not name:
        flash('Software name is required.', 'error')
    else:
        conn = get_db()
        conn.execute('''
            UPDATE software 
            SET name=?, description=?, category=?, available_labs=?, is_enabled=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        ''', (name, description, category, available_labs, is_enabled, sid))
        conn.commit()
        conn.close()
        flash('Software updated successfully.', 'success')
    
    return redirect(url_for('admin_software'))

@app.route('/admin/software/delete/<int:sid>', methods=['POST'])
@admin_required
def admin_delete_software(sid):
    conn = get_db()
    conn.execute('DELETE FROM software WHERE id=?', (sid,))
    conn.commit()
    conn.close()
    flash('Software deleted.', 'success')
    return redirect(url_for('admin_software'))

@app.route('/admin/labs')
@admin_required
def admin_labs():
    conn = get_db()
    
    # Existing lab data
    labs_data = [dict(r) for r in conn.execute('''
        SELECT lab_room,
               MAX(is_enabled) AS is_enabled,
               MAX(capacity) AS capacity,
               MAX(description) AS description
        FROM lab_availability
        GROUP BY lab_room
        ORDER BY CAST(lab_room AS INTEGER)
    ''').fetchall()]
    labs_by_room = {str(lab['lab_room']): lab for lab in labs_data}
    
    # New PC control data
    capacities = lab_capacity_map(conn)
    booking_map = build_lab_pc_map(conn, capacities)
    maintenance_map = get_maintenance_map(conn)
    conn.close()
    
    return render_template(
        'admin_labs.html', 
        labs=labs_data,
        labs_by_room=labs_by_room,
        lab_rooms=LAB_ROOMS,
        capacities=capacities,
        booking_map=booking_map,
        maintenance_map=maintenance_map,
        logo=get_logo()
    )

@app.route('/admin/labs/edit/<lab_room>', methods=['POST'])
@admin_required
def admin_edit_lab(lab_room):
    lab_room = normalize_lab_room(lab_room)
    if not lab_room:
        flash('Invalid laboratory.', 'error')
        return redirect(url_for('admin_labs'))

    is_enabled = 1 if request.form.get('is_enabled') == '1' else 0
    try:
        capacity = max(1, min(100, int(request.form.get('capacity', PC_COUNT))))
    except (TypeError, ValueError):
        capacity = PC_COUNT
    conn = get_db()
    cursor = conn.execute('''
        UPDATE lab_availability
        SET is_enabled=?, capacity=?, updated_at=CURRENT_TIMESTAMP
        WHERE lab_room=?
    ''', (is_enabled, capacity, lab_room))
    if cursor.rowcount == 0:
        conn.execute('''
            INSERT INTO lab_availability (lab_room, is_enabled, capacity)
            VALUES (?, ?, ?)
        ''', (lab_room, is_enabled, capacity))
    conn.commit()
    conn.close()
    flash('Laboratory updated successfully.', 'success')
    return redirect(url_for('admin_labs'))

# ─── ADMIN PC CONTROL API ─────────────────────────────────────
@app.route('/admin/pc-control/update', methods=['POST'])
@admin_required
def admin_pc_control_update():
    data = request.get_json(force=True)
    lab = data.get('lab', '').strip()
    pcs = data.get('pcs', [])
    status = data.get('status', 'available').strip()
    if lab not in LAB_ROOMS:
        return jsonify({'ok': False, 'error': 'Invalid lab room.'})
    if status not in ('available', 'maintenance'):
        return jsonify({'ok': False, 'error': 'Invalid status.'})
    if not pcs:
        return jsonify({'ok': False, 'error': 'No PCs selected.'})
    conn = get_db()
    for pc in pcs:
        pc = str(pc).strip()
        if not pc:
            continue
        if status == 'maintenance':
            conn.execute('''
                INSERT INTO pc_status (lab_room, pc_number, status, updated_at)
                VALUES (?, ?, 'maintenance', CURRENT_TIMESTAMP)
                ON CONFLICT(lab_room, pc_number)
                DO UPDATE SET status='maintenance', updated_at=CURRENT_TIMESTAMP
            ''', (lab, pc))
        else:
            conn.execute('''
                INSERT INTO pc_status (lab_room, pc_number, status, updated_at)
                VALUES (?, ?, 'available', CURRENT_TIMESTAMP)
                ON CONFLICT(lab_room, pc_number)
                DO UPDATE SET status='available', updated_at=CURRENT_TIMESTAMP
            ''', (lab, pc))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/admin/reservation-settings')
@admin_required
def admin_reservation_settings():
    conn = get_db()
    settings = {}
    for row in conn.execute('SELECT setting_key, setting_value FROM reservation_settings').fetchall():
        settings[row['setting_key']] = row['setting_value']
    conn.close()
    return render_template('admin_reservation_settings.html', settings=settings, logo=get_logo())

@app.route('/admin/reservation-settings/update', methods=['POST'])
@admin_required
def admin_update_reservation_settings():
    reservations_enabled = 1 if '1' in request.form.getlist('reservations_enabled') else 0
    
    conn = get_db()
    conn.execute('''
        INSERT OR REPLACE INTO reservation_settings (setting_key, setting_value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    ''', ('reservations_enabled', str(reservations_enabled)))
    conn.commit()
    conn.close()
    
    flash('Reservation settings updated.', 'success')
    return redirect(url_for('admin_reservation_settings'))

@app.route('/admin/leaderboard')
@admin_required
def admin_leaderboard():
    conn = get_db()
    rankings = leaderboard_entries(conn)
    conn.close()
    return render_template('admin_leaderboard.html', rankings=rankings, logo=get_logo())

if __name__ == '__main__':
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)
