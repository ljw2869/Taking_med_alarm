from flask import Flask, render_template, request, redirect, url_for
from datetime import datetime, timedelta
import sqlite3
import os
import smtplib

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# --- DB 초기화 ---
def init_db():
    with sqlite3.connect('medication.db') as conn:
        c=conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS Customers(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT NOT NULL,
                  contact TEXT,
                  start_date DATE
                  )''')
        c.execute('''CREATE TABLE IF NOT EXISTS DoseLogs(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  customer_id INTEGER,
                  taken_date DATE NOT NULL,
                  taken_week INTEGER,
                  note TEXT,
                  FOREIGN KEY(customer_id) REFERENCES Customers(id)
                  )
                  ''')
        # 알림 전송 기록: 중복 전송 방지용
        c.execute('''CREATE TABLE IF NOT EXISTS NotificationLogs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            notify_type TEXT,
            notify_date DATE,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(customer_id) REFERENCES Customers(id)
        )''')
        conn.commit()

# --- 이메일 전송 함수 (SMTP) ---
def send_email_smtp(to_email: str, subject: str, body: str) -> bool:
    """
    환경변수 필요:
      EMAIL_USER : SMTP 로그인(예: Gmail 주소)
      EMAIL_PASS : SMTP 비밀번호 또는 앱 비밀번호
      EMAIL_FROM : 발신자 이메일(생략하면 EMAIL_USER 사용)
      EMAIL_HOST : smtp.gmail.com 등 (기본 smtp.gmail.com)
      EMAIL_PORT : 465 (SSL) 또는 587 (STARTTLS)
    """
    smtp_user = os.environ.get('EMAIL_USER')
    smtp_pass = os.environ.get('EMAIL_PASS')
    smtp_host = os.environ.get('EMAIL_HOST', 'smtp.gmail.com')
    smtp_port = int(os.environ.get('EMAIL_PORT', 465))
    from_addr = os.environ.get('EMAIL_FROM', smtp_user)

    if not smtp_user or not smtp_pass:
        app.logger.error("Email credentials not configured. Set EMAIL_USER and EMAIL_PASS.")
        return False
    if not to_email:
        app.logger.warning("Recipient email empty, skipping.")
        return False

    try:
        # MIME 메시지 구성 (간단한 텍스트)
        msg = MIMEMultipart()
        msg['From'] = from_addr
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        # SSL 연결 (Gmail 권장 포트 465)
        with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
            server.login(smtp_user, smtp_pass)
            server.sendmail(from_addr, [to_email], msg.as_string())

        app.logger.info(f"Email sent to {to_email}: {subject}")
        return True

    except Exception as e:
        app.logger.exception("Failed to send email:")
        return False
    

# --- 알림 체크 및 발송 작업 ---
def check_and_notify():
    """
    - 모든 고객 순회
    - 최근 DoseLogs 기준으로 next_date 계산 (없으면 start_date + 4주)
    - D-day 계산, 사전 정의된 notify_days와 비교 (예: 7, 0)
    - 중복 전송 확인 후 메일 발송, NotificationLogs 기록
    """
    notify_days = [7, 0]  # 보낼 시점: D-7, D-0
    today = datetime.today().date()

    with sqlite3.connect('medication.db') as conn:
        c = conn.cursor()
        c.execute("SELECT id, name, contact, start_date FROM Customers")
        customers = c.fetchall()

        for cust in customers:
            cid, name, contact, start_date = cust
            # 마지막 복약 기록
            c.execute("SELECT taken_date, taken_week FROM DoseLogs WHERE customer_id=? ORDER BY taken_date DESC LIMIT 1", (cid,))
            last = c.fetchone()
            if last:
                last_taken = datetime.strptime(last[0], "%Y-%m-%d").date()
                taken_week = int(last[1]) if last[1] else 4
            else:
                last_taken = datetime.strptime(start_date, "%Y-%m-%d").date()
                taken_week = 4

            next_date = last_taken + timedelta(weeks=taken_week)
            d_day = (next_date - today).days

            for nd in notify_days:
                if d_day == nd:
                    notify_type = f"D-{nd}" if nd != 0 else "D-0"
                    # 중복 확인: 같은 고객/notify_type/notify_date 이미 보냈는지
                    c.execute("SELECT 1 FROM NotificationLogs WHERE customer_id=? AND notify_type=? AND notify_date=?",
                              (cid, notify_type, next_date.isoformat()))
                    if c.fetchone():
                        app.logger.info(f"Already notified {cid} {notify_type} for {next_date}")
                        continue

                    # 이메일 발송 (고객 contact 대신 담당자 이메일로 고정)
                    subject = f"[복약 알람] {name}님: 복약 예정일 {next_date.isoformat()}"
                    body = f"""고객: {name}, 복약 예정일: {next_date.isoformat()}, 현재 상태: D-{d_day}, 확인 부탁드립니다."""
                    # 수신자를 contact 대신 환경변수 또는 직접 지정
                    EMAIL_TO = os.environ.get("NOTIFY_EMAIL", "ljwljw2869@gmail.com")

                    sent = send_email_smtp(EMAIL_TO, subject, body)
                    if sent:
                        c.execute("INSERT INTO NotificationLogs (customer_id, notify_type, notify_date) VALUES (?,?,?)",
                                  (cid, notify_type, next_date.isoformat()))
                        conn.commit()

# --- 스케줄러 설정 (BackgroundScheduler) ---
scheduler = BackgroundScheduler(timezone="Europe/London")
# 매일 오전 09:00 (Europe/London 기준)에 실행하고 싶다면:
scheduler.add_job(check_and_notify, 'cron', hour=9, minute=0)  
# 개발/테스트 용으로는 단순히 매분 테스트: scheduler.add_job(check_and_notify, 'interval', minutes=1)

from threading import Thread

# (테스트용) 수동 트리거 라우트 — 테스트 시 사용(운영에서는 제거 또는 보호 필요)
@app.route('/run_checks_now')
def run_checks_now():
    # 백그라운드에서 check_and_notify 실행
    Thread(target=check_and_notify).start()
    return "check_and_notify() executed"


# --- 홈 ---
@app.route('/')
def index():
    with sqlite3.connect('medication.db') as conn:
        c = conn.cursor()
        c.execute('SELECT * FROM Customers WHERE is_active=1 ORDER BY name COLLATE NOCASE ASC')
        customers = c.fetchall()

        customer_list = []
        for customer in customers:
            customer_id = customer[0]

            # 해당 고객의 마지막 복약 기록 가져오기
            c.execute("SELECT taken_date, taken_week FROM DoseLogs WHERE customer_id=? ORDER BY taken_date DESC LIMIT 1", (customer_id,))
            last_log = c.fetchone()

            if last_log:
                taken_date = datetime.strptime(last_log[0], "%Y-%m-%d").date()
                taken_week = int(last_log[1])
                next_date = taken_date + timedelta(weeks=taken_week)
                today = datetime.today().date()
                d_day = (next_date - today).days
            else:
                # 기록 없으면 시작일 기준으로 계산
                start_date = datetime.strptime(customer[3], "%Y-%m-%d").date()
                next_date = start_date + timedelta(weeks=4)
                today=datetime.today().date()
                d_day = (next_date - today).days

            # 고객 정보 + D-day 추가
            customer_list.append({
                "id": customer[0],
                "name": customer[1],
                "phone": customer[2],
                "start_date": customer[3],
                "d_day": d_day
            })
    return render_template('index.html', customers=customer_list)

# --- 고객 추가 ---
@app.route('/add_customer', methods=['POST'])
def add_customer():
    name = request.form['name']
    contact = request.form['contact']
    start_date = request.form['start_date']
    first_weeks=int(request.form.get('first_weeks',4))# 값이 안 넘어올 때를 대비히 기본값을 4로 처리

    with sqlite3.connect('medication.db') as conn:
        c = conn.cursor()

        # 같은 이름이 이미 존재하는지 확인
        c.execute("SELECT id FROM Customers WHERE name = ?", (name,))
        existing = c.fetchone()

        if existing:
            # 이미 존재 → 저장하지 않고 에러 페이지 대신 경고
            return render_template(
                'error.html',
                message=f"⚠️ '{name}' 고객은 이미 존재합니다!"
            )
        
        c.execute('INSERT INTO Customers (name, contact, start_date) VALUES (?, ?, ?)',
                  (name, contact, start_date))
        customer_id=c.lastrowid
        c.execute('INSERT INTO DoseLogs (customer_id,taken_date,taken_week,note) VALUES(?,?,?,?)',
                  (customer_id,start_date,first_weeks,'첫 복용'))
        conn.commit()
    return redirect(url_for('index'))

# --- 복약 기록 추가 ---
@app.route('/add_dose_log/<int:customer_id>', methods=['POST'])
def add_dose_log(customer_id):
    taken_date = request.form['taken_date']
    taken_week=int(request.form.get('taken_week',4)) # html 폼에서 전송된 데이터를 first_weeks라는 이름의 값이 있으면 가져오고 없으면 기본값 4를 대신 사용
    note = request.form['note']
    with sqlite3.connect('medication.db') as conn:
        c = conn.cursor()
        c.execute('INSERT INTO DoseLogs (customer_id, taken_date, taken_week,note) VALUES (?, ?, ?, ?)',
                  (customer_id, taken_date, taken_week, note))
        conn.commit()
    return redirect(url_for('view_customer', customer_id=customer_id))

# --- 고객별 상세보기 ---
@app.route('/customer/<int:customer_id>')
def view_customer(customer_id):
    with sqlite3.connect('medication.db') as conn:
        c = conn.cursor()
        c.execute('SELECT * FROM Customers WHERE id = ?', (customer_id,))
        customer = c.fetchone()

        c.execute('SELECT id, taken_date, taken_week, note, extra_weeks FROM DoseLogs WHERE customer_id = ? ORDER BY taken_date DESC', (customer_id,))
        logs = c.fetchall()

    # 다음 복약일 계산
    if logs:
        latest = datetime.strptime(logs[0][1], "%Y-%m-%d")
        taken_week=int(logs[0][2]) if logs[0][2] else 4 #위에서 select한 순서대로이니까 taken_week임
    else:
        latest = datetime.strptime(customer[3], "%Y-%m-%d")
        taken_week=4

    next_date = latest + timedelta(weeks=taken_week)
    alert_date = next_date - timedelta(days=7)

    # D-day 계산
    today=datetime.today().date()
    d_day=(next_date.date()-today).days

    return render_template('customer.html', customer=customer, logs=logs, next_date=next_date.date(), alert_date=alert_date.date(),d_day=d_day)

# --- 고객 정보 수정 --- (GET: 폼 표시, POST: 수정 반영)
@app.route('/customer/<int:customer_id>/edit', methods=['GET', 'POST'])
def edit_customer(customer_id):
    error_message = None  # 기본값

    with sqlite3.connect('medication.db') as conn:
        c = conn.cursor()

        if request.method == 'POST':
            new_name = request.form.get('name', '').strip()
            new_contact = request.form.get('contact', '').strip()

            # 이름이 공백이면 → 에러 메시지 설정 후 다시 폼 렌더링
            if not new_name:
                error_message = "⚠️ 이름은 반드시 입력해야 합니다."
                c.execute("SELECT id, name, contact FROM Customers WHERE id = ?", (customer_id,))
                customer = c.fetchone()
                return render_template('edit_customer.html', customer=customer, error_message=error_message)

            # DB에 업데이트
            c.execute("UPDATE Customers SET name = ?, contact = ? WHERE id = ?",
                      (new_name, new_contact, customer_id))
            conn.commit()

            return redirect(url_for('view_customer', customer_id=customer_id))

        # GET 요청 → 수정 폼 보여주기
        c.execute("SELECT id, name, contact FROM Customers WHERE id = ?", (customer_id,))
        customer = c.fetchone()

    return render_template('edit_customer.html', customer=customer, error_message=error_message)

def migrate_db():
    """
    DB 스키마 변경 시 기존 데이터를 보존하면서 마이그레이션 수행
    예: Customers 테이블에 is_active 컬럼 추가
    """
    with sqlite3.connect('medication.db') as conn:
        c = conn.cursor()
        # is_active 컬럼이 없으면 추가
        try:
            c.execute("ALTER TABLE Customers ADD COLUMN is_active INTEGER DEFAULT 1;")
            print("is_active 칼럼이 추가되었습니다.")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e):
                print("is active 칼럼이 이미 존재합니다. 건너 뜁니다.")
            else:
                raise

        # DoseLogs 테이블: extra_weeks 컬럼 추가
        try:
            c.execute("ALTER TABLE DoseLogs ADD COLUMN extra_weeks INTEGER DEFAULT 0;")
            print("extra_weeks 칼럼이 추가되었습니다.")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e):
                print("extra_weeks 칼럼이 이미 존재합니다. 건너뜁니다.")
            else:
                raise

        conn.commit()

@app.route('/manage_customers', methods=['GET', 'POST'])
def manage_customers():
    with sqlite3.connect('medication.db') as conn:
        c = conn.cursor()

        if request.method == 'POST':
            # 체크된 고객 id 리스트
            active_ids = set(request.form.getlist('active_customers'))

            # 현재 DB 상태 불러오기
            c.execute("SELECT id, is_active FROM Customers")
            db_customers = c.fetchall()

            updates = []
            for cid, current_active in db_customers:
                cid_str = str(cid)
                should_active = 1 if cid_str in active_ids else 0
                if current_active != should_active:
                    updates.append((should_active, cid))

            if updates:
                c.executemany("UPDATE Customers SET is_active = ? WHERE id = ?", updates)
                conn.commit()

            return redirect(url_for('manage_customers'))

        # GET 요청 → 모든 고객 목록 (활성화 여부 포함)
        c.execute("SELECT id, name, contact, is_active FROM Customers ORDER BY name COLLATE NOCASE ASC")
        customers = c.fetchall()

    return render_template("manage_customers.html", customers=customers)

# 여행으로 인한 추가 수령을 위해 복용기록 수정 창 열기 (GET) 및 수정 반영 (POST)
@app.route("/customer/<int:customer_id>/dose_log/<int:log_id>/edit", methods=["GET", "POST"])
def edit_dose_log(customer_id,log_id):
    with sqlite3.connect('medication.db') as conn:
        conn.row_factory = sqlite3.Row  
        cursor = conn.cursor()

        if request.method == "POST":
            taken_date = request.form["taken_date"]
            taken_week = int(request.form["taken_week"])
            extra_weeks = int(request.form["extra_weeks"])
            note = request.form["note"]

            cursor.execute("""
                UPDATE DoseLogs
                SET taken_date=?, taken_week=?, extra_weeks=?, note=?
                WHERE id=?
            """, (taken_date, taken_week, extra_weeks, note, log_id))
            conn.commit()
            return redirect(url_for("view_customer", customer_id=customer_id))

        # GET 요청일 때: 기존 데이터 불러오기
        cursor.execute("SELECT * FROM DoseLogs WHERE id=?", (log_id,))
        dose_log = cursor.fetchone()

    return render_template("edit_dose_log.html", dose_log=dose_log)

# 특정 DoseLog 삭제
@app.route("/customer/<int:customer_id>/dose_log/<int:log_id>/delete", methods=["POST"])
def delete_dose_log(customer_id,log_id):
    with sqlite3.connect('medication.db') as conn:
        cursor = conn.cursor()

        cursor.execute("DELETE FROM DoseLogs WHERE id=?", (log_id,))
        conn.commit()

        return redirect(url_for("view_customer",customer_id=customer_id))

# 앱 실행 전 DB 초기화
if __name__ == '__main__':
    init_db()
    migrate_db() #is_active 칼럼 마이그레이션
    # Flask 디버거의 reloader 때문에 작업이 두 번 실행되는 것을 방지
    # reloader의 경우 subprocess에서 WERKZEUG_RUN_MAIN가 'true'로 설정됨
    if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        scheduler.start()

    app.run(debug=True)
