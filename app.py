"""AFTERPARTY 2026-27 - registration, payment-proof review, tickets and gate scanner.

Beginner note: you do NOT need to edit this file. Settings live in the .env file.
"""
import csv, hashlib, io, json, os, re, secrets, shutil, sqlite3, tempfile, uuid, zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)

from flask import (Flask, abort, flash, jsonify, make_response, redirect, render_template,
                   request, send_file, session, url_for)
from flask.sessions import SecureCookieSessionInterface
from PIL import Image, ImageDraw, ImageOps
from werkzeug.security import check_password_hash, generate_password_hash

import ticketgen

IST = timezone(timedelta(hours=5, minutes=30))
MAX_UPLOAD = 8 * 1024 * 1024
ALLOWED_PROOF = {"jpg", "jpeg", "png", "webp"}
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'\-]{1,39}$")


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def env_path(name, default):
    p = Path(os.environ.get(name) or default)
    return p if p.is_absolute() else ROOT / p


SCHEMA = """
CREATE TABLE IF NOT EXISTS admins (
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS registrations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reference TEXT UNIQUE NOT NULL,
  status_token TEXT UNIQUE NOT NULL,
  full_name TEXT NOT NULL, phone TEXT NOT NULL, utr TEXT NOT NULL,
  proof_filename TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'Pending' CHECK(status IN ('Pending','Approved','Rejected')),
  reject_reason TEXT,
  created_at TEXT NOT NULL, ip TEXT, reviewed_at TEXT, reviewed_by TEXT,
  ticket_token_hash TEXT UNIQUE, ticket_reference TEXT UNIQUE,
  ticket_filename TEXT, qr_filename TEXT,
  checked_in INTEGER NOT NULL DEFAULT 0, checked_in_at TEXT, checked_in_by TEXT);
CREATE INDEX IF NOT EXISTS idx_reg_status ON registrations(status);
CREATE INDEX IF NOT EXISTS idx_reg_phone ON registrations(phone);
CREATE INDEX IF NOT EXISTS idx_reg_utr ON registrations(utr);
CREATE TABLE IF NOT EXISTS scan_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, method TEXT NOT NULL,
  ticket_reference TEXT, result TEXT NOT NULL, admin TEXT);
CREATE TABLE IF NOT EXISTS throttle (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, key TEXT NOT NULL, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_throttle ON throttle(kind, key, at);
"""


class _CookieIface(SecureCookieSessionInterface):
    pass


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        DATABASE=str(env_path("DATABASE_PATH", "data/afterparty.sqlite3")),
        UPLOAD_DIR=str(env_path("UPLOAD_DIR", "data/payment-proofs")),
        TICKET_DIR=str(env_path("TICKET_DIR", "data/tickets")),
        MAX_CONTENT_LENGTH=MAX_UPLOAD + 512 * 1024,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        EVENT_NAME="Afterparty 2026–27", EVENT_DATE="4 December 2026",
        EVENT_TIME="2:00 PM – 10:00 PM", VENUE="Sundaram, Tikoniya", PRICE="₹1,300",
        ORGANISER_PHONE="+91 9627723371", ORGANISER_WA="919627723371",
        PAYEE_NAME=os.environ.get("PAYEE_NAME", "").strip(),
        DEMO_MODE=os.environ.get("DEMO_MODE", "0") == "1",
        REGISTRATION_OPEN=os.environ.get("REGISTRATION_OPEN", "1") != "0",
        TESTING=False,
    )
    if app.config["DEMO_MODE"]:
        app.config["DATABASE"] = str(ROOT / "data-demo" / "afterparty-demo.sqlite3")
        app.config["UPLOAD_DIR"] = str(ROOT / "data-demo" / "payment-proofs")
        app.config["TICKET_DIR"] = str(ROOT / "data-demo" / "tickets")
    if test_config:
        app.config.update(test_config)
    for d in (Path(app.config["DATABASE"]).parent, app.config["UPLOAD_DIR"], app.config["TICKET_DIR"]):
        Path(d).mkdir(parents=True, exist_ok=True)

    # A secret key that survives restarts (needed so admin stays logged in across workers).
    key = os.environ.get("SECRET_KEY", "").strip()
    if app.config.get("SECRET_KEY"):
        key = app.config["SECRET_KEY"]
    if not key or key.startswith("replace-with"):
        kf = Path(app.config["DATABASE"]).parent / "secret_key.txt"
        if not kf.exists():
            kf.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        key = kf.read_text(encoding="utf-8").strip()
    app.config["SECRET_KEY"] = key

    if os.environ.get("TRUST_PROXY", "0") == "1":
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # ------------------------------------------------------------------ database
    @contextmanager
    def db():
        conn = sqlite3.connect(app.config["DATABASE"], timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    app.get_db = db  # used by tests

    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        user = os.environ.get("ADMIN_USERNAME", "").strip()
        pw = os.environ.get("ADMIN_PASSWORD", "")
        if app.config.get("ADMIN_USERNAME"):
            user, pw = app.config["ADMIN_USERNAME"], app.config["ADMIN_PASSWORD"]
        if app.config["DEMO_MODE"] and not app.config.get("ADMIN_USERNAME"):
            user, pw = "demo", "demo-password-123"   # test mode always uses this fixed login
        if user and pw and not pw.startswith("replace-with"):
            if len(pw) < 8 and not app.config["DEMO_MODE"]:
                raise SystemExit("ADMIN_PASSWORD must be at least 8 characters (10+ recommended). Edit the .env file.")
            row = c.execute("SELECT * FROM admins WHERE username=?", (user,)).fetchone()
            if not row:
                c.execute("INSERT INTO admins(username,password_hash,created_at) VALUES(?,?,?)",
                          (user, generate_password_hash(pw), utcnow()))
            elif not check_password_hash(row["password_hash"], pw):  # .env is the source of truth
                c.execute("UPDATE admins SET password_hash=? WHERE id=?", (generate_password_hash(pw), row["id"]))

    # ------------------------------------------------------------------ helpers
    def csrf_token():
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_urlsafe(24)
        return session["_csrf"]

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.template_filter("ist")
    def ist(value):
        if not value:
            return "—"
        try:
            dt = datetime.fromisoformat(value).astimezone(IST)
            return dt.strftime("%d %b %Y, %I:%M %p").lstrip("0")
        except ValueError:
            return value

    @app.context_processor
    def inject():
        return dict(cfg=app.config)

    @app.before_request
    def csrf_guard():
        if request.method == "POST":
            sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token") or ""
            if not sent or not secrets.compare_digest(sent, session.get("_csrf", "")):
                if request.path.startswith("/api/"):
                    return jsonify(ok=False, result="SESSION EXPIRED", message="Refresh the page and sign in again."), 400
                abort(400, "Security token expired. Please go back, refresh the page and try again.")

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        if request.endpoint != "static":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    def client_ip():
        return (request.remote_addr or "?")[:64]

    def throttled(kind, key, limit, minutes):
        since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
        with db() as c:
            n = c.execute("SELECT COUNT(*) FROM throttle WHERE kind=? AND key=? AND at>?", (kind, key, since)).fetchone()[0]
        return n >= limit

    def record(kind, key):
        with db() as c:
            c.execute("INSERT INTO throttle(kind,key,at) VALUES(?,?,?)", (kind, key, utcnow()))
            c.execute("DELETE FROM throttle WHERE at<?", ((datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds"),))

    def admin_required(fn):
        @wraps(fn)
        def wrapped(*a, **kw):
            if not session.get("admin_id"):
                if request.path.startswith("/api/"):
                    return jsonify(ok=False, result="LOGIN REQUIRED", message="Please sign in again."), 401
                return redirect(url_for("admin_login"))
            return fn(*a, **kw)
        return wrapped

    def clean_proof(raw):
        """Validate + re-encode the payment screenshot (removes metadata/odd payloads)."""
        Image.MAX_IMAGE_PIXELS = 40_000_000
        img = Image.open(io.BytesIO(raw))
        img.verify()
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((2200, 2200))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=86, optimize=True)
        return out.getvalue()

    def make_ticket_files(reg_ref, full_name, ticket_ref, raw_token):
        tdir = Path(app.config["TICKET_DIR"])
        ticket_file, qr_file = f"{reg_ref}.png", f"{reg_ref}-qr.png"
        ticketgen.make_ticket_png(full_name, ticket_ref, raw_token, tdir / ticket_file, tdir / qr_file)
        return ticket_file, qr_file

    # ------------------------------------------------------------- public pages
    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/healthz")
    def healthz():
        return "ok"

    @app.post("/register")
    def register():
        if not app.config["REGISTRATION_OPEN"]:
            flash("Registrations are closed. Please contact the organiser.", "error")
            return redirect(url_for("index"))
        ip = client_ip()
        if throttled("register", ip, 12, 60):
            flash("Too many submissions from this connection. Please wait a while or contact the organiser.", "error")
            return redirect(url_for("index"))
        name = re.sub(r"\s+", " ", request.form.get("full_name", "")).strip()
        phone = re.sub(r"[\s\-]", "", request.form.get("phone", ""))
        phone = re.sub(r"^(\+91|91|0)(?=\d{10}$)", "", phone)
        utr = re.sub(r"\s+", "", request.form.get("utr", "")).upper()
        proof = request.files.get("proof")
        back = lambda msg: (flash(msg, "error"), redirect(url_for("index") + "#register"))[1]
        if not NAME_RE.match(name):
            return back("Type your name using English letters only (A–Z), 2–40 characters, so it prints correctly on the ticket.")
        if not re.fullmatch(r"[6-9]\d{9}", phone):
            return back("Enter a valid 10-digit Indian mobile number.")
        if not re.fullmatch(r"[A-Z0-9]{8,30}", utr):
            return back("Enter the UTR / transaction ID from your UPI app (8–30 letters or numbers, no spaces).")
        if not proof or not proof.filename:
            return back("Please upload your payment screenshot.")
        ext = proof.filename.rsplit(".", 1)[-1].lower() if "." in proof.filename else ""
        if ext not in ALLOWED_PROOF:
            return back("The screenshot must be a JPG, PNG or WEBP image.")
        raw = proof.read(MAX_UPLOAD + 1)
        if len(raw) > MAX_UPLOAD:
            return back("The screenshot must be 8 MB or smaller.")
        try:
            clean = clean_proof(raw)
        except Exception:
            return back("That file is not a valid image. Please upload a screenshot (JPG/PNG).")
        with db() as c:
            dup = c.execute("SELECT reference FROM registrations WHERE utr=? AND status!='Rejected'", (utr,)).fetchone()
        if dup:
            return back("This UTR has already been submitted. If you made a mistake, contact the organiser on WhatsApp.")
        ref = "AP-" + secrets.token_hex(4).upper()
        status_token = secrets.token_urlsafe(32)
        filename = uuid.uuid4().hex + ".jpg"
        Path(app.config["UPLOAD_DIR"], filename).write_bytes(clean)
        try:
            with db() as c:
                c.execute("""INSERT INTO registrations(reference,status_token,full_name,phone,utr,proof_filename,created_at,ip)
                             VALUES(?,?,?,?,?,?,?,?)""", (ref, status_token, name, phone, utr, filename, utcnow(), ip))
        except sqlite3.IntegrityError:
            Path(app.config["UPLOAD_DIR"], filename).unlink(missing_ok=True)
            return back("Could not save your registration. Please try once more.")
        record("register", ip)
        return redirect(url_for("status_page", token=status_token, new=1))

    def reg_by_token(token):
        with db() as c:
            return c.execute("SELECT * FROM registrations WHERE status_token=?", (token,)).fetchone()

    @app.get("/status/<token>")
    def status_page(token):
        reg = reg_by_token(token)
        if not reg:
            abort(404)
        link = url_for("status_page", token=token, _external=True)
        return render_template("status.html", reg=reg, token=token, link=link, new=request.args.get("new"))

    @app.get("/ticket/<token>")
    def ticket_download(token):
        reg = reg_by_token(token)
        if not reg or reg["status"] != "Approved" or not reg["ticket_filename"]:
            abort(404)
        path = Path(app.config["TICKET_DIR"], reg["ticket_filename"])
        if not path.exists():
            abort(404)
        inline = request.args.get("view") == "1"
        return send_file(path, mimetype="image/png", as_attachment=not inline,
                         download_name=f"Afterparty-Ticket-{reg['ticket_reference']}.png")

    @app.post("/find")
    def find():
        ip = client_ip()
        if throttled("find", ip, 8, 30):
            flash("Too many attempts. Please wait 30 minutes or message the organiser.", "error")
            return redirect(url_for("index") + "#find")
        ref = request.form.get("reference", "").strip().upper()
        phone = re.sub(r"^(\+91|91|0)(?=\d{10}$)", "", re.sub(r"[\s\-]", "", request.form.get("phone", "")))
        with db() as c:
            reg = c.execute("SELECT status_token FROM registrations WHERE reference=? AND phone=?", (ref, phone)).fetchone()
        if not reg:
            record("find", ip)
            flash("No registration matches that reference and mobile number.", "error")
            return redirect(url_for("index") + "#find")
        return redirect(url_for("status_page", token=reg["status_token"]))

    # -------------------------------------------------------------------- admin
    @app.get("/admin/login")
    def admin_login():
        if session.get("admin_id"):
            return redirect(url_for("admin_dashboard"))
        return render_template("login.html")

    @app.post("/admin/login")
    def admin_login_post():
        ip = client_ip()
        username = request.form.get("username", "").strip()
        if throttled("login", ip, 8, 15):
            flash("Too many failed attempts. Wait 15 minutes and try again.", "error")
            return redirect(url_for("admin_login"))
        with db() as c:
            admin = c.execute("SELECT * FROM admins WHERE username=?", (username,)).fetchone()
        if not admin or not check_password_hash(admin["password_hash"], request.form.get("password", "")):
            record("login", ip)
            flash("Incorrect username or password.", "error")
            return redirect(url_for("admin_login"))
        session.clear()
        session.permanent = True
        session["admin_id"], session["admin_name"] = admin["id"], admin["username"]
        csrf_token()
        return redirect(url_for("admin_dashboard"))

    @app.post("/admin/logout")
    @admin_required
    def admin_logout():
        session.clear()
        return redirect(url_for("admin_login"))

    PAGE = 40

    @app.get("/admin")
    @admin_required
    def admin_dashboard():
        q = request.args.get("q", "").strip()
        status = request.args.get("status", "")
        entry = request.args.get("entry", "")
        page = max(1, request.args.get("page", 1, type=int))
        where, args = ["1=1"], []
        if q:
            where.append("(full_name LIKE ? OR phone LIKE ? OR reference LIKE ? OR ticket_reference LIKE ? OR utr LIKE ?)")
            args += [f"%{q}%"] * 5
        if status in ("Pending", "Approved", "Rejected"):
            where.append("status=?"); args.append(status)
        if entry == "used":
            where.append("checked_in=1")
        elif entry == "unused":
            where.append("status='Approved' AND checked_in=0")
        w = " AND ".join(where)
        with db() as c:
            total = c.execute(f"SELECT COUNT(*) FROM registrations WHERE {w}", args).fetchone()[0]
            regs = c.execute(f"SELECT * FROM registrations WHERE {w} ORDER BY id DESC LIMIT ? OFFSET ?", args + [PAGE, (page - 1) * PAGE]).fetchall()
            counts = {s: c.execute("SELECT COUNT(*) FROM registrations WHERE status=?", (s,)).fetchone()[0] for s in ("Pending", "Approved", "Rejected")}
            counts["Entered"] = c.execute("SELECT COUNT(*) FROM registrations WHERE checked_in=1").fetchone()[0]
            dups = {r[0] for r in c.execute("SELECT utr FROM registrations GROUP BY utr HAVING COUNT(*)>1")}
        return render_template("admin.html", regs=regs, counts=counts, q=q, status=status, entry=entry,
                               page=page, pages=max(1, -(-total // PAGE)), total=total, dups=dups)

    @app.get("/admin/proof/<int:reg_id>")
    @admin_required
    def proof(reg_id):
        with db() as c:
            reg = c.execute("SELECT proof_filename FROM registrations WHERE id=?", (reg_id,)).fetchone()
        if not reg:
            abort(404)
        path = Path(app.config["UPLOAD_DIR"], reg["proof_filename"])
        if not path.exists():
            abort(404)
        return send_file(path, mimetype="image/jpeg")

    @app.post("/admin/review/<int:reg_id>")
    @admin_required
    def review(reg_id):
        action = request.form.get("action")
        if action not in ("approve", "reject", "revoke", "reopen"):
            abort(400)
        reason = request.form.get("reason", "").strip()[:200] or None
        admin = session.get("admin_name")
        made = []
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                reg = c.execute("SELECT * FROM registrations WHERE id=?", (reg_id,)).fetchone()
                if not reg:
                    c.execute("ROLLBACK"); abort(404)
                st = reg["status"]
                if action == "approve" and st == "Pending":
                    raw = secrets.token_urlsafe(24)
                    tref = "APT-" + secrets.token_hex(4).upper()
                    tfile, qfile = make_ticket_files(reg["reference"], reg["full_name"], tref, raw)
                    made = [tfile, qfile]
                    c.execute("""UPDATE registrations SET status='Approved', reviewed_at=?, reviewed_by=?, reject_reason=NULL,
                                 ticket_token_hash=?, ticket_reference=?, ticket_filename=?, qr_filename=?
                                 WHERE id=? AND status='Pending'""", (utcnow(), admin, token_hash(raw), tref, tfile, qfile, reg_id))
                    msg = f"Approved. Ticket {tref} and its unique QR were created."
                elif action == "reject" and st == "Pending":
                    c.execute("UPDATE registrations SET status='Rejected', reviewed_at=?, reviewed_by=?, reject_reason=? WHERE id=? AND status='Pending'",
                              (utcnow(), admin, reason, reg_id))
                    msg = "Registration rejected."
                elif action == "revoke" and st == "Approved":
                    c.execute("UPDATE registrations SET status='Rejected', reviewed_at=?, reviewed_by=?, reject_reason=? WHERE id=?",
                              (utcnow(), admin, reason or "Ticket cancelled by organiser", reg_id))
                    msg = "Ticket cancelled. Its QR will now show NOT APPROVED at the gate."
                elif action == "reopen" and st == "Rejected":
                    if reg["ticket_token_hash"]:
                        c.execute("UPDATE registrations SET status='Approved', reviewed_at=?, reviewed_by=?, reject_reason=NULL WHERE id=?",
                                  (utcnow(), admin, reg_id))
                        msg = "Ticket restored (same QR works again)."
                    else:
                        c.execute("UPDATE registrations SET status='Pending', reject_reason=NULL WHERE id=?", (reg_id,))
                        msg = "Moved back to Pending."
                else:
                    c.execute("ROLLBACK")
                    flash(f"Nothing changed: this registration is already {st}.", "error")
                    return redirect(request.referrer or url_for("admin_dashboard"))
                c.execute("COMMIT")
            except Exception:
                try: c.execute("ROLLBACK")
                except sqlite3.Error: pass
                for f in made:
                    Path(app.config["TICKET_DIR"], f).unlink(missing_ok=True)
                raise
        flash(msg, "success")
        return redirect(request.referrer or url_for("admin_dashboard"))

    @app.get("/admin/ticket/<int:reg_id>")
    @admin_required
    def admin_ticket(reg_id):
        with db() as c:
            reg = c.execute("SELECT * FROM registrations WHERE id=?", (reg_id,)).fetchone()
        if not reg or not reg["ticket_filename"]:
            abort(404)
        path = Path(app.config["TICKET_DIR"], reg["ticket_filename"])
        if not path.exists():
            abort(404)
        return send_file(path, mimetype="image/png", as_attachment=request.args.get("view") != "1",
                         download_name=f"Afterparty-Ticket-{reg['ticket_reference']}.png")

    def csv_safe(v):
        s = "" if v is None else str(v)
        return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s

    @app.get("/admin/export.csv")
    @admin_required
    def export_csv():
        with db() as c:
            rows = c.execute("""SELECT reference,full_name,phone,utr,status,reject_reason,created_at,reviewed_at,
                ticket_reference,checked_in,checked_in_at FROM registrations ORDER BY id""").fetchall()
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["Registration Ref", "Name", "Phone", "UTR", "Status", "Reject reason", "Submitted (UTC)", "Reviewed (UTC)",
                    "Ticket Ref", "Entered (1=yes)", "Entry time (UTC)"])
        for r in rows:
            w.writerow([csv_safe(x) for x in tuple(r)])
        resp = make_response("\ufeff" + out.getvalue())
        resp.headers["Content-Type"] = "text/csv; charset=utf-8"
        resp.headers["Content-Disposition"] = "attachment; filename=afterparty-registrations.csv"
        return resp

    @app.get("/admin/backup.zip")
    @admin_required
    def backup():
        tmp = tempfile.mkdtemp()
        try:
            snap = Path(tmp, "afterparty.sqlite3")
            src = sqlite3.connect(app.config["DATABASE"])
            dst = sqlite3.connect(snap)
            src.backup(dst); dst.close(); src.close()
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(snap, "afterparty.sqlite3")
                for folder, arc in ((app.config["UPLOAD_DIR"], "payment-proofs"), (app.config["TICKET_DIR"], "tickets")):
                    for p in Path(folder).glob("*"):
                        if p.is_file():
                            z.write(p, f"{arc}/{p.name}")
            buf.seek(0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        stamp = datetime.now(IST).strftime("%Y%m%d-%H%M")
        return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=f"afterparty-backup-{stamp}.zip")

    @app.post("/admin/demo-seed")
    @admin_required
    def demo_seed():
        if not app.config["DEMO_MODE"]:
            abort(404)
        for nm, ph in (("Aarav Sharma", "9876500001"), ("Priya Verma", "9876500002"), ("Rohan Gupta", "9876500003")):
            img = Image.new("RGB", (600, 900), (245, 245, 250))
            d = ImageDraw.Draw(img)
            d.text((40, 60), "SAMPLE PAYMENT PROOF\n(demo - not real money)\n" + nm, fill=(40, 40, 60))
            buf = io.BytesIO(); img.save(buf, "JPEG")
            fn = uuid.uuid4().hex + ".jpg"
            Path(app.config["UPLOAD_DIR"], fn).write_bytes(buf.getvalue())
            with db() as c:
                c.execute("""INSERT INTO registrations(reference,status_token,full_name,phone,utr,proof_filename,created_at)
                             VALUES(?,?,?,?,?,?,?)""", ("AP-" + secrets.token_hex(4).upper(), secrets.token_urlsafe(32), nm, ph,
                                                        "DEMO" + secrets.token_hex(5).upper(), fn, utcnow()))
        flash("Added 3 sample registrations.", "success")
        return redirect(url_for("admin_dashboard"))

    # ------------------------------------------------------------- gate scanner
    @app.get("/admin/scanner")
    @admin_required
    def scanner():
        with db() as c:
            n_in = c.execute("SELECT COUNT(*) FROM registrations WHERE checked_in=1").fetchone()[0]
            n_ok = c.execute("SELECT COUNT(*) FROM registrations WHERE status='Approved'").fetchone()[0]
        return render_template("scanner.html", n_in=n_in, n_ok=n_ok)

    def log_scan(method, tref, result):
        with db() as c:
            c.execute("INSERT INTO scan_log(at,method,ticket_reference,result,admin) VALUES(?,?,?,?,?)",
                      (utcnow(), method, tref, result, session.get("admin_name")))

    def check_in(column, value, method):
        """ATOMIC check-in. One single UPDATE statement decides who gets in, so two
        phones scanning the same ticket at the same moment can never both succeed."""
        now = utcnow()
        with db() as c:
            cur = c.execute(f"""UPDATE registrations SET checked_in=1, checked_in_at=?, checked_in_by=?
                                WHERE {column}=? AND status='Approved' AND checked_in=0""",
                            (now, session.get("admin_name"), value))
            reg = c.execute(f"SELECT * FROM registrations WHERE {column}=?", (value,)).fetchone()
        if cur.rowcount == 1:
            log_scan(method, reg["ticket_reference"], "VALID")
            return jsonify(ok=True, result="VALID — ENTRY ALLOWED", name=reg["full_name"],
                           message=f'{reg["full_name"]} · {reg["ticket_reference"]}'), 200
        if not reg:
            log_scan(method, None, "INVALID")
            return jsonify(ok=False, result="INVALID TICKET — ENTRY DENIED", message="No such ticket."), 404
        if reg["status"] != "Approved":
            log_scan(method, reg["ticket_reference"], "NOT APPROVED")
            return jsonify(ok=False, result="NOT APPROVED — ENTRY DENIED", message=f'Status: {reg["status"]}.'), 403
        log_scan(method, reg["ticket_reference"], "ALREADY USED")
        return jsonify(ok=False, result="ALREADY USED — ENTRY DENIED", name=reg["full_name"],
                       message=f'{reg["full_name"]} · {reg["ticket_reference"]} · entered {ist(reg["checked_in_at"])}'), 409

    @app.post("/api/check-in")
    @admin_required
    def api_check_in():
        token = ((request.get_json(silent=True) or {}).get("token") or "").strip()
        if not 16 <= len(token) <= 200 or not re.fullmatch(r"[A-Za-z0-9_\-]+", token):
            log_scan("qr", None, "INVALID")
            return jsonify(ok=False, result="INVALID TICKET — ENTRY DENIED", message="This QR is not an Afterparty ticket."), 400
        return check_in("ticket_token_hash", token_hash(token), "qr")

    @app.post("/api/manual-lookup")
    @admin_required
    def api_manual_lookup():
        ref = ((request.get_json(silent=True) or {}).get("reference") or "").strip().upper()
        with db() as c:
            reg = c.execute("SELECT * FROM registrations WHERE ticket_reference=?", (ref,)).fetchone()
        if not reg:
            return jsonify(ok=False, result="INVALID TICKET — ENTRY DENIED", message="Ticket number not found."), 404
        if reg["status"] != "Approved":
            return jsonify(ok=False, result="NOT APPROVED — ENTRY DENIED", message=f'Status: {reg["status"]}.'), 403
        if reg["checked_in"]:
            return jsonify(ok=False, result="ALREADY USED — ENTRY DENIED", message=f'{reg["full_name"]} · entered {ist(reg["checked_in_at"])}'), 409
        return jsonify(ok=True, result="APPROVED — NOT YET USED", name=reg["full_name"], ticket_reference=reg["ticket_reference"],
                       message=f'{reg["full_name"]} · {reg["ticket_reference"]}')

    @app.post("/api/manual-checkin")
    @admin_required
    def api_manual_checkin():
        ref = ((request.get_json(silent=True) or {}).get("reference") or "").strip().upper()
        return check_in("ticket_reference", ref, "manual")

    @app.errorhandler(413)
    def too_large(e):
        flash("That upload is too large. The screenshot must be 8 MB or smaller.", "error")
        return redirect(url_for("index") + "#register")

    @app.errorhandler(404)
    def nf(e):
        return render_template("message.html", title="Not found", text="This page does not exist, or the link is wrong."), 404

    @app.errorhandler(400)
    def bad(e):
        return render_template("message.html", title="Please try again", text=getattr(e, "description", "Bad request.")), 400

    return app


app = create_app() if os.environ.get("AFTERPARTY_NO_AUTOCREATE") != "1" else None

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5000")), debug=False)
