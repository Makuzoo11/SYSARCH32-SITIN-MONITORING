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

# ─── HELPERS ──────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def allowed_file(f):
    return '.' in f and f.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_logo():
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key='logo_filename'").fetchone()
    conn.close()
    return row['value'] if row else ''

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('is_admin'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# ─── DB INIT ──────────────────────────────────────────────
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
            is_admin           INTEGER DEFAULT 0,
            profile_pic        TEXT DEFAULT '',
            created_at         DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS sit_in_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            lab_room    TEXT DEFAULT '',
            purpose     TEXT DEFAULT '',
            time_in     DATETIME DEFAULT CURRENT_TIMESTAMP,
            time_out    DATETIME,
            status      TEXT DEFAULT 'Active',
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS announcements (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            content    TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
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

    # Create admin separately with parameterized query
    conn.execute('''
        INSERT OR IGNORE INTO users
            (id_number, last_name, first_name, middle_name, course,
             course_level, email, address, password, sessions_remaining, is_admin)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    ''', ('admin','Admin','CCS','','N/A',0,'admin@ccs.edu','CCS',hash_pw('admin'),0,1))
    conn.commit()
    conn.close()

# ─── AUTH ─────────────────────────────────────────────────
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
            # Force password change for admin-created students on first login
            if not user['is_admin'] and user['must_change_password']:
                return redirect(url_for('force_change_password'))
            # Auto-create sit-in record when student logs in
            if not user['is_admin']:
                conn2 = get_db()
                # Only create if no active sit-in already exists
                existing = conn2.execute(
                    "SELECT id FROM sit_in_logs WHERE user_id=? AND status='Active' LIMIT 1",
                    (user['id'],)
                ).fetchone()
                if not existing:
                    conn2.execute(
                        "INSERT INTO sit_in_logs (user_id, purpose, lab_room, status, source) VALUES (?, 'Login', '—', 'Active', 'login')",
                        (user['id'],)
                    )
                    conn2.commit()
                conn2.close()
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
    uid = session.get('user_id')
    if uid and not session.get('is_admin'):
        conn = get_db()
        # Close any active sit-in
        active = conn.execute(
            "SELECT id FROM sit_in_logs WHERE user_id=? AND status='Active' LIMIT 1", (uid,)
        ).fetchone()
        if active:
            conn.execute(
                "UPDATE sit_in_logs SET time_out=CURRENT_TIMESTAMP, status='Done' WHERE id=?",
                (active['id'],)
            )
        # Always deduct a session on logout if sessions remain
        user = conn.execute('SELECT sessions_remaining FROM users WHERE id=?', (uid,)).fetchone()
        if user and user['sessions_remaining'] > 0:
            conn.execute(
                "UPDATE users SET sessions_remaining=sessions_remaining-1 WHERE id=?",
                (uid,)
            )
        conn.commit()
        conn.close()
    session.clear()
    return redirect(url_for('login'))

# ─── FORCE PASSWORD CHANGE ───────────────────────────────
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
    user = conn.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
    logs = conn.execute('''SELECT * FROM sit_in_logs WHERE user_id=? AND source != 'login'
                           ORDER BY time_in DESC LIMIT 10''', (session['user_id'],)).fetchall()
    announcements = conn.execute('SELECT * FROM announcements ORDER BY created_at DESC').fetchall()
    conn.close()
    return render_template('dashboard.html', user=user, logs=logs, announcements=announcements, logo=get_logo())

# ─── STUDENT PROFILE EDIT ─────────────────────────────────
@app.route('/profile/edit', methods=['POST'])
@login_required
def edit_profile():
    uid        = session['user_id']
    first_name = request.form.get('first_name', '').strip()
    last_name  = request.form.get('last_name', '').strip()
    email      = request.form.get('email', '').strip()
    address    = request.form.get('address', '').strip()
    new_pw     = request.form.get('new_password', '').strip()
    confirm_pw = request.form.get('confirm_password', '').strip()
    current_pw = request.form.get('current_password', '').strip()

    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()

    # Verify current password
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
    stats = {
        'total_students':  conn.execute("SELECT COUNT(*) FROM users WHERE is_admin=0").fetchone()[0],
        'currently_sitin': conn.execute("SELECT COUNT(*) FROM sit_in_logs WHERE status='Active'").fetchone()[0],
        'total_sitin':     conn.execute("SELECT COUNT(*) FROM sit_in_logs").fetchone()[0],
    }
    announcements = conn.execute("SELECT * FROM announcements ORDER BY created_at DESC").fetchall()
    course_stats  = [{'course': r['course'], 'cnt': r['cnt']} for r in conn.execute(
        "SELECT course, COUNT(*) as cnt FROM users WHERE is_admin=0 GROUP BY course"
    ).fetchall()]
    conn.close()
    return render_template('admin_dashboard.html', stats=stats,
                           announcements=announcements, course_stats=course_stats, logo=get_logo())

@app.route('/admin/announcement', methods=['POST'])
@admin_required
def post_announcement():
    content = request.form.get('content','').strip()
    if content:
        conn = get_db()
        conn.execute("INSERT INTO announcements (content) VALUES (?)", (content,))
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
    try:
        sessions = int(request.form.get('sessions_remaining', 0))
    except (ValueError, TypeError):
        sessions = 0
    conn = get_db()
    conn.execute(
        '''UPDATE users SET last_name=?, first_name=?, middle_name=?,
            course_level=?, email=?, course=?, address=?, sessions_remaining=?
            WHERE id=?''',
        (last_name, first_name, middle_name, course_level, email, course, address, sessions, uid)
    )
    conn.commit()
    conn.close()
    flash('Student updated.', 'success')
    return redirect(url_for('admin_students'))

@app.route('/admin/students/delete/<int:uid>', methods=['POST'])
@admin_required
def admin_delete_student(uid):
    conn = get_db()
    conn.execute("DELETE FROM sit_in_logs WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    conn.commit()
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
    conn.close()
    return render_template('admin_sitin.html', logs=logs, logo=get_logo())

@app.route('/admin/sitin/add', methods=['POST'])
@admin_required
def admin_sitin_add():
    id_num  = request.form.get('id_number','').strip()
    purpose = request.form.get('purpose','').strip()
    lab     = request.form.get('lab','').strip()
    conn    = get_db()
    user    = conn.execute("SELECT * FROM users WHERE id_number=?", (id_num,)).fetchone()
    if not user:
        flash('Student not found.', 'error')
    elif user['sessions_remaining'] <= 0:
        flash('No sessions remaining.', 'error')
    else:
        conn.execute("INSERT INTO sit_in_logs (user_id,purpose,lab_room,status,source) VALUES (?,?,?,'Active','admin')",
                     (user['id'],purpose,lab))
        conn.execute("UPDATE users SET sessions_remaining=sessions_remaining-1 WHERE id=?", (user['id'],))
        conn.commit()
        flash('Sit-in recorded.', 'success')
    conn.close()
    return redirect(url_for('admin_sitin'))

@app.route('/admin/sitin/timeout/<int:lid>', methods=['POST'])
@admin_required
def admin_sitin_timeout(lid):
    conn = get_db()
    conn.execute("UPDATE sit_in_logs SET time_out=CURRENT_TIMESTAMP,status='Done' WHERE id=?", (lid,))
    conn.commit()
    conn.close()
    flash('Timed out.', 'success')
    return redirect(url_for('admin_sitin'))

@app.route('/admin/sitin/signout/<int:lid>', methods=['POST'])
@admin_required
def admin_sitin_signout(lid):
    conn = get_db()
    # Get the sit-in log to find user_id
    log = conn.execute("SELECT user_id FROM sit_in_logs WHERE id=?", (lid,)).fetchone()
    if log:
        # Close the sit-in
        conn.execute("UPDATE sit_in_logs SET time_out=CURRENT_TIMESTAMP, status='Done' WHERE id=?", (lid,))
        # Restore the session
        conn.execute("UPDATE users SET sessions_remaining=sessions_remaining+1 WHERE id=?", (log['user_id'],))
    conn.commit()
    conn.close()
    flash('Student signed out and session restored.', 'success')
    return redirect(url_for('admin_sitin'))

@app.route('/admin/sitin/delete/<int:lid>', methods=['POST'])
@admin_required
def admin_sitin_delete(lid):
    conn = get_db()
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
    base_q   = '''SELECT s.*, u.id_number, u.first_name, u.last_name
                  FROM sit_in_logs s JOIN users u ON s.user_id=u.id
                  WHERE s.user_id=? AND s.source != 'login' '''
    params   = [session['user_id']]
    if search:
        base_q += ' AND (u.id_number LIKE ? OR u.first_name LIKE ? OR u.last_name LIKE ? OR s.purpose LIKE ? OR s.lab_room LIKE ?)'
        params += [f'%{search}%']*5
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
    conn.execute('DELETE FROM sit_in_logs WHERE id=? AND user_id=?', (lid, session['user_id']))
    conn.commit()
    conn.close()
    flash('Record deleted.', 'success')
    return redirect(url_for('student_history'))

# ─── STUDENT RESERVATION ──────────────────────────────────
@app.route('/reservation', methods=['GET','POST'])
@login_required
def student_reservation():
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
    if request.method == 'POST':
        purpose  = request.form.get('purpose','').strip()
        lab      = request.form.get('lab','').strip()
        time_in  = request.form.get('time_in','').strip()
        date     = request.form.get('date','').strip()
        if user['sessions_remaining'] <= 0:
            flash('No sessions remaining.', 'error')
        else:
            time_in_dt = f"{date} {time_in}:00" if date and time_in else None
            conn.execute("INSERT INTO sit_in_logs (user_id,purpose,lab_room,time_in,status,source) VALUES (?,?,?,?,'Active','student')",
                         (user['id'], purpose, lab, time_in_dt))
            conn.execute('UPDATE users SET sessions_remaining=sessions_remaining-1 WHERE id=?', (user['id'],))
            conn.commit()
            flash('Reservation submitted successfully!', 'success')
            conn.close()
            return redirect(url_for('student_reservation'))
    conn.close()
    return render_template('reservation.html', user=user, logo=get_logo())

# ─── ADMIN RESERVATIONS ──────────────────────────────────
@app.route('/admin/reservations')
@admin_required
def admin_reservations():
    conn = get_db()
    reservations = [dict(r) for r in conn.execute('''
        SELECT s.*, u.id_number, u.first_name, u.last_name, u.course, u.sessions_remaining
        FROM sit_in_logs s JOIN users u ON s.user_id=u.id
        WHERE s.source = 'student'
        ORDER BY s.time_in DESC
    ''').fetchall()]
    conn.close()
    return render_template('admin_reservations.html', reservations=reservations, logo=get_logo())

@app.route('/admin/reservations/approve/<int:rid>', methods=['POST'])
@admin_required
def admin_reservation_approve(rid):
    conn = get_db()
    conn.execute("UPDATE sit_in_logs SET status='Approved' WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    flash('Reservation approved.', 'success')
    return redirect(url_for('admin_reservations'))

@app.route('/admin/reservations/timeout/<int:rid>', methods=['POST'])
@admin_required
def admin_reservation_timeout(rid):
    conn = get_db()
    conn.execute("UPDATE sit_in_logs SET time_out=CURRENT_TIMESTAMP, status='Done' WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    flash('Reservation timed out.', 'success')
    return redirect(url_for('admin_reservations'))

@app.route('/admin/reservations/delete/<int:rid>', methods=['POST'])
@admin_required
def admin_reservation_delete(rid):
    conn = get_db()
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
    base_q   = '''SELECT s.*, u.id_number, u.first_name, u.last_name, u.course
                   FROM sit_in_logs s JOIN users u ON s.user_id=u.id
                   WHERE s.source != 'login' '''
    params   = []
    if search:
        base_q += ' AND (u.id_number LIKE ? OR u.first_name LIKE ? OR u.last_name LIKE ? OR s.purpose LIKE ? OR s.lab_room LIKE ?)'
        params  = [f'%{search}%'] * 5
    total       = conn.execute(f'SELECT COUNT(*) FROM ({base_q})', params).fetchone()[0]
    logs        = [dict(r) for r in conn.execute(base_q + ' ORDER BY s.time_in DESC LIMIT ? OFFSET ?', params + [per_page, offset]).fetchall()]
    conn.close()
    total_pages = max(1, -(-total // per_page))
    return render_template('admin_sitin_records.html', logs=logs, page=page, per_page=per_page,
                           total=total, total_pages=total_pages, search=search, logo=get_logo())

# ─── ADMIN SIT-IN REPORTS ─────────────────────────────────
@app.route('/admin/sitin/reports')
@admin_required
def admin_sitin_reports():
    conn = get_db()
    stats = {
        'total':  conn.execute("SELECT COUNT(*) FROM sit_in_logs").fetchone()[0],
        'active': conn.execute("SELECT COUNT(*) FROM sit_in_logs WHERE status='Active'").fetchone()[0],
        'done':   conn.execute("SELECT COUNT(*) FROM sit_in_logs WHERE status='Done'").fetchone()[0],
        'today':  conn.execute("SELECT COUNT(*) FROM sit_in_logs WHERE DATE(time_in)=DATE('now')").fetchone()[0],
    }
    by_course = [dict(r) for r in conn.execute('''
        SELECT u.course, COUNT(*) as total,
               SUM(CASE WHEN s.status='Active' THEN 1 ELSE 0 END) as active,
               SUM(CASE WHEN s.status='Done'   THEN 1 ELSE 0 END) as done
        FROM sit_in_logs s JOIN users u ON s.user_id=u.id
        GROUP BY u.course ORDER BY total DESC
    ''').fetchall()]
    by_lab = [dict(r) for r in conn.execute('''
        SELECT lab_room, COUNT(*) as total,
               SUM(CASE WHEN status='Active' THEN 1 ELSE 0 END) as active,
               SUM(CASE WHEN status='Done'   THEN 1 ELSE 0 END) as done
        FROM sit_in_logs GROUP BY lab_room ORDER BY total DESC
    ''').fetchall()]
    by_purpose = [dict(r) for r in conn.execute('''
        SELECT purpose, COUNT(*) as total FROM sit_in_logs
        GROUP BY purpose ORDER BY total DESC LIMIT 10
    ''').fetchall()]
    trend = [dict(r) for r in conn.execute('''
        SELECT DATE(time_in) as day, COUNT(*) as cnt
        FROM sit_in_logs
        WHERE time_in >= DATE('now', '-14 days')
        GROUP BY DATE(time_in) ORDER BY day
    ''').fetchall()]
    conn.close()
    return render_template('admin_sitin_reports.html', stats=stats, by_course=by_course,
                           by_lab=by_lab, by_purpose=by_purpose, trend=trend, logo=get_logo())

if __name__ == '__main__':
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)