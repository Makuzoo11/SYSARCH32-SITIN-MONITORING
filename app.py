from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import sqlite3, os, hashlib
from functools import wraps
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'ccs_secret_2024'

UPLOAD_FOLDER = os.path.join('static', 'images')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
DB = 'monitoring.db'
LAB_ROOMS = ['524', '526', '530', '544']
PC_COUNT = 40

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
               u.reward_points, u.completed_tasks, u.sessions_remaining, u.admin_remarks,
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

    max_points = max((row['reward_points'] or 0) for row in rows) or 1
    max_hours = max((row['total_hours'] or 0) for row in rows) or 1
    max_tasks = max((row['completed_tasks'] or 0) for row in rows) or 1

    for row in rows:
        points = row['reward_points'] or 0
        hours = row['total_hours'] or 0
        tasks = row['completed_tasks'] or 0
        score = ((points / max_points) * 50.0) + ((hours / max_hours) * 30.0) + ((tasks / max_tasks) * 20.0)
        row['total_hours'] = round(hours, 2)
        row['leaderboard_score'] = round(score, 2)

    rows.sort(key=lambda item: (-item['leaderboard_score'], -(item['reward_points'] or 0), -(item['total_hours'] or 0), -(item['completed_tasks'] or 0), item['last_name'], item['first_name']))
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

def pc_options():
    return [str(i) for i in range(1, PC_COUNT + 1)]

def normalize_lab_room(value):
    value = (value or '').strip()
    return value if value in LAB_ROOMS else ''

def normalize_pc_number(value):
    value = (value or '').strip()
    return value if value in pc_options() else ''

def build_lab_pc_map(conn):
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
            for pc in pc_options()
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
        pc = normalize_pc_number(row['pc_number'])
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

    return status_map

def pc_is_available(conn, lab_room, pc_number, ignore_log_id=None):
    lab_room = normalize_lab_room(lab_room)
    pc_number = normalize_pc_number(pc_number)
    if not lab_room or not pc_number:
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
    row = conn.execute("SELECT reservation_points FROM users WHERE id=?", (user_id,)).fetchone()
    points = row['reservation_points'] if row else 0
    if points >= 3:
        extra_sessions = points // 3
        remaining_points = points % 3
        conn.execute(
            "UPDATE users SET sessions_remaining=sessions_remaining+?, reservation_points=? WHERE id=?",
            (extra_sessions, remaining_points, user_id)
        )
        return extra_sessions, remaining_points
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
        INSERT OR IGNORE INTO settings (key, value) VALUES ('logo_filename', '');
        INSERT OR IGNORE INTO courses (code) VALUES ('BSIT'),('BSCS'),('BSIS'),('BSCE'),('BSCpE');
    ''')
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
    conn.close()
    if request.method == 'POST':
        d = {k: request.form.get(k,'').strip() for k in
             ['id_number','last_name','first_name','middle_name','course_level',
              'password','repeat_password','email','course','address']}
        if d['password'] != d['repeat_password']:
            flash('Passwords do not match.', 'error')
            return render_template('register.html', logo=get_logo(), data=d, courses=courses)
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
    return render_template('register.html', logo=get_logo(), data={}, courses=courses)

@app.route('/logout')
def logout():
    user_id = session.get('user_id')
    if user_id:
        conn = get_db()
        try:
            user = conn.execute("SELECT id, is_admin FROM users WHERE id=?", (user_id,)).fetchone()
            if user and not user['is_admin']:
                for _ in range(3):
                    try:
                        conn.execute('''
                            UPDATE sit_in_logs
                            SET status='Done',
                                time_out=COALESCE(time_out, CURRENT_TIMESTAMP)
                            WHERE user_id=?
                              AND status IN ('Pending', 'Approved', 'Active')
                        ''', (user_id,))
                        conn.commit()
                        break
                    except sqlite3.OperationalError as exc:
                        if 'locked' not in str(exc).lower():
                            raise
                        conn.rollback()
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
    current_session = conn.execute('''
        SELECT s.lab_room, s.pc_number, s.status, s.time_in, s.time_out
        FROM sit_in_logs s
        WHERE s.user_id=?
          AND s.status IN ('Pending', 'Approved', 'Active')
          AND COALESCE(s.source, 'admin') != 'login'
        ORDER BY s.time_in DESC
        LIMIT 1
    ''', (session['user_id'],)).fetchone()
    logs = conn.execute('''
        SELECT s.*, f.rating, f.feedback_text
        FROM sit_in_logs s
        LEFT JOIN feedback f ON f.sit_in_log_id = s.id AND f.user_id = s.user_id
        WHERE s.user_id=? AND s.source != 'login'
        ORDER BY s.time_in DESC LIMIT 8
    ''', (session['user_id'],)).fetchall()
    announcements = conn.execute('SELECT * FROM announcements ORDER BY created_at DESC').fetchall()
    notifications = dashboard_notifications(conn, session['user_id'])
    leaderboard = leaderboard_entries(conn, limit=5)
    pending_requests = conn.execute(
        '''SELECT COUNT(*) FROM sit_in_logs
           WHERE user_id=? AND source='student' AND status='Pending' ''',
        (session['user_id'],)
    ).fetchone()[0]
    conn.close()
    return render_template(
        'dashboard.html',
        user=user,
        logs=logs,
        announcements=announcements,
        notifications=notifications,
        leaderboard=leaderboard,
        pending_requests=pending_requests,
        current_session=current_session,
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
    return render_template('admin_dashboard.html', stats=stats,
                           announcements=announcements, course_stats=course_stats,
                           leaderboard=leaderboard, analytics=analytics, logo=get_logo())

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
    conn.close()
    return render_template('admin_students.html', students=students,
                           courses=courses, search=search, logo=get_logo())

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
    try:
        completed_tasks = int(request.form.get('completed_tasks', 0))
    except (ValueError, TypeError):
        completed_tasks = 0
    conn = get_db()
    conn.execute(
        '''UPDATE users SET last_name=?, first_name=?, middle_name=?,
            course_level=?, email=?, course=?, address=?, sessions_remaining=?,
            reward_points=?, completed_tasks=?, admin_remarks=?
            WHERE id=?''',
        (
            last_name, first_name, middle_name, course_level, email, course, address,
            sessions, reward_points, completed_tasks, remarks, uid
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
    logs = [dict(r) for r in conn.execute('''SELECT s.*, u.id_number, u.first_name, u.last_name,
                            u.sessions_remaining, u.course
                           FROM sit_in_logs s JOIN users u ON s.user_id=u.id
                           WHERE s.source = 'admin'
                           ORDER BY s.time_in DESC''').fetchall()]
    booking_map = build_lab_pc_map(conn)
    conn.close()
    return render_template(
        'admin_sitin.html',
        logs=logs,
        labs=lab_options(),
        pcs=pc_options(),
        booking_map=booking_map,
        logo=get_logo()
    )

@app.route('/admin/sitin/add', methods=['POST'])
@admin_required
def admin_sitin_add():
    id_num  = request.form.get('id_number','').strip()
    purpose = request.form.get('purpose','').strip()
    lab     = normalize_lab_room(request.form.get('lab',''))
    pc      = normalize_pc_number(request.form.get('pc_number',''))
    next_url = request.form.get('next', '').strip()
    if not next_url.startswith('/'):
        next_url = ''
    conn    = get_db()
    user    = conn.execute("SELECT * FROM users WHERE id_number=?", (id_num,)).fetchone()
    if not user:
        flash('Student not found.', 'error')
    elif user['sessions_remaining'] <= 0:
        flash('No sessions remaining.', 'error')
    elif not lab:
        flash('Please choose a valid laboratory.', 'error')
    elif not pc:
        flash('Please choose a valid PC.', 'error')
    elif not pc_is_available(conn, lab, pc):
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
    base_q   = '''SELECT s.*, u.id_number, u.first_name, u.last_name,
                         COALESCE(f.rating, 0) AS rating, COALESCE(f.feedback_text, '') AS feedback_text
                  FROM sit_in_logs s JOIN users u ON s.user_id=u.id
                  LEFT JOIN feedback f ON f.sit_in_log_id = s.id AND f.user_id = s.user_id
                  WHERE s.user_id=? AND s.source != 'login' '''
    params   = [session['user_id']]
    if search:
        base_q += ' AND (u.id_number LIKE ? OR u.first_name LIKE ? OR u.last_name LIKE ? OR s.purpose LIKE ? OR s.lab_room LIKE ? OR s.pc_number LIKE ?)'
        params += [f'%{search}%']*6
    total    = conn.execute(f'SELECT COUNT(*) FROM ({base_q})', params).fetchone()[0]
    logs     = [dict(r) for r in conn.execute(base_q + ' ORDER BY s.time_in DESC LIMIT ? OFFSET ?', params + [per_page, offset]).fetchall()]
    conn.close()
    total_pages = max(1, -(-total // per_page))
    return render_template('history.html', logs=logs, page=page, per_page=per_page,
                           total=total, total_pages=total_pages, search=search, logo=get_logo())

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
    user = conn.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
    labs = lab_options()
    pcs = pc_options()
    booking_map = build_lab_pc_map(conn)
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
        purpose  = request.form.get('purpose','').strip()
        lab      = normalize_lab_room(request.form.get('lab',''))
        pc       = normalize_pc_number(request.form.get('pc_number',''))
        time_in  = request.form.get('time_in','').strip()
        date     = request.form.get('date','').strip()
        selected_lab = lab
        selected_pc = pc
        if not purpose or not lab or not pc or not time_in or not date:
            flash('Please complete all reservation fields.', 'error')
        elif not pc_is_available(conn, lab, pc):
            flash(f'Lab {lab} PC {pc} is already reserved or in use.', 'error')
        else:
            time_in_dt = f"{date} {time_in}:00"
            conn.execute(
                "INSERT INTO sit_in_logs (user_id,purpose,lab_room,pc_number,time_in,status,source) VALUES (?,?,?,?,?,'Pending','student')",
                (user['id'], purpose, lab, pc, time_in_dt)
            )
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
    conn.close()
    return render_template(
        'reservation.html',
        user=user,
        labs=labs,
        pcs=pcs,
        booking_map=booking_map,
        current_active_lab=current_active['lab_room'] if current_active else '',
        current_active_pc=current_active['pc_number'] if current_active else '',
        selected_lab=selected_lab,
        selected_pc=selected_pc,
        logo=get_logo()
    )

# ─── ADMIN RESERVATIONS ──────────────────────────────────
@app.route('/admin/reservations')
@admin_required
def admin_reservations():
    conn = get_db()
    reservations = [dict(r) for r in conn.execute('''
        SELECT s.*, u.id_number, u.first_name, u.last_name, u.course,
               u.sessions_remaining, u.reservation_points, u.reward_points, u.completed_tasks
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
        if log['source'] == 'admin':
            conn.execute(
                "UPDATE users SET sessions_remaining=sessions_remaining-1 WHERE id=? AND sessions_remaining > 0",
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

@app.route('/leaderboard')
@login_required
def leaderboard():
    conn = get_db()
    rankings = leaderboard_entries(conn)
    user = fetch_user(conn, session['user_id'])
    notifications = dashboard_notifications(conn, session['user_id'], limit=4) if not session.get('is_admin') else []
    conn.close()
    return render_template('leaderboard.html', rankings=rankings, user=user, notifications=notifications, logo=get_logo())

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
