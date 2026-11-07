"""Automated tests. Run with:  python -m unittest discover -s tests -v   (or double-click RUN_TESTS.bat)"""
import io, os, sys, tempfile, threading, unittest, zipfile, sqlite3
from pathlib import Path

os.environ["AFTERPARTY_NO_AUTOCREATE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2, numpy as np
from PIL import Image, ImageChops

import app as appmod
import ticketgen

ART = Image.open(ticketgen.ART).convert("RGB")


def png_bytes(color=(120, 80, 90), size=(160, 300)):
    b = io.BytesIO(); Image.new("RGB", size, color).save(b, "PNG"); b.seek(0); return b


def decode_qr(img_path):
    img = cv2.imread(str(img_path))
    h = img.shape[0]
    val, _, _ = cv2.QRCodeDetector().detectAndDecode(img[517:, :1280])
    return val


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        r = Path(self.tmp.name)
        self.cfg = {"SECRET_KEY": "test-secret", "DATABASE": str(r / "t.sqlite3"), "UPLOAD_DIR": str(r / "p"),
                    "TICKET_DIR": str(r / "t"), "ADMIN_USERNAME": "boss", "ADMIN_PASSWORD": "Str0ng-Pass-123"}
        self.app = appmod.create_app(self.cfg)
        self.c = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def csrf(self, client=None):
        client = client or self.c
        client.get("/")
        with client.session_transaction() as s:
            return s["_csrf"]

    def login(self, client=None, pw="Str0ng-Pass-123"):
        client = client or self.c
        t = self.csrf(client)
        return client.post("/admin/login", data={"_csrf": t, "username": "boss", "password": pw})

    def register(self, name="Aarav Sharma", phone="9876543210", utr="123456789012", img=None, fname="p.png"):
        t = self.csrf()
        return self.c.post("/register", data={"_csrf": t, "full_name": name, "phone": phone, "utr": utr,
                           "proof": (img or png_bytes(), fname)}, content_type="multipart/form-data")

    def row(self, **where):
        with self.app.get_db() as c:
            k, v = list(where.items())[0]
            return c.execute(f"SELECT * FROM registrations WHERE {k}=?", (v,)).fetchone()

    def review(self, reg_id, action, client=None, reason=""):
        client = client or self.c
        with client.session_transaction() as s:
            t = s["_csrf"]
        return client.post(f"/admin/review/{reg_id}", data={"_csrf": t, "action": action, "reason": reason}, follow_redirects=True)

    def approved_ticket(self, name="Aarav Sharma", utr="123456789012"):
        self.register(name=name, utr=utr)
        self.login()
        reg = self.row(utr=utr)
        self.review(reg["id"], "approve")
        reg = self.row(utr=utr)
        token = decode_qr(Path(self.app.config["TICKET_DIR"]) / reg["ticket_filename"])
        return reg, token

    def scan(self, token, client=None):
        client = client or self.c
        with client.session_transaction() as s:
            t = s["_csrf"]
        return client.post("/api/check-in", json={"token": token}, headers={"X-CSRF-Token": t})


class RegistrationTests(Base):
    def test_01_submit_and_proof_saved_pending(self):
        r = self.register()
        self.assertEqual(r.status_code, 302)
        reg = self.row(utr="123456789012")
        self.assertEqual(reg["status"], "Pending")
        self.assertTrue((Path(self.app.config["UPLOAD_DIR"]) / reg["proof_filename"]).exists())
        page = self.c.get(r.headers["Location"])
        self.assertEqual(page.status_code, 200); self.assertIn(b"PENDING", page.data)
        self.assertEqual(self.c.get("/status/wrong").status_code, 404)

    def test_02_validation(self):
        for kw in ({"name": "R@hul 123"}, {"name": "A"}, {"phone": "12345"}, {"utr": "12"}):
            self.register(**kw)
        self.register(img=io.BytesIO(b"not an image"), fname="x.png")
        self.register(fname="x.exe")
        with self.app.get_db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM registrations").fetchone()[0], 0)

    def test_03_duplicate_utr_blocked_and_phone_normalised(self):
        self.register(phone="+91 98765-43210")
        self.assertEqual(self.row(utr="123456789012")["phone"], "9876543210")
        self.register(name="Someone Else", utr="123456789012")
        with self.app.get_db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM registrations").fetchone()[0], 1)

    def test_04_oversize_upload_rejected(self):
        big = io.BytesIO(b"\xff" * (9 * 1024 * 1024))
        self.register(img=big, fname="big.jpg")
        with self.app.get_db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM registrations").fetchone()[0], 0)

    def test_05_proof_reencoded_as_jpeg(self):
        self.register()
        reg = self.row(utr="123456789012")
        data = (Path(self.app.config["UPLOAD_DIR"]) / reg["proof_filename"]).read_bytes()
        self.assertEqual(data[:3], b"\xff\xd8\xff")


class AdminTests(Base):
    def test_10_admin_pages_protected(self):
        self.assertEqual(self.c.get("/admin").status_code, 302)
        self.assertEqual(self.c.get("/admin/scanner").status_code, 302)
        self.assertEqual(self.c.get("/admin/export.csv").status_code, 302)
        self.assertEqual(self.c.get("/admin/backup.zip").status_code, 302)
        self.assertEqual(self.csrf() and self.c.post("/api/check-in", json={"token": "x" * 30}).status_code, 400)

    def test_11_login_wrong_and_right_and_lockout(self):
        self.login(pw="wrong")
        self.assertEqual(self.c.get("/admin").status_code, 302)
        self.login()
        self.assertEqual(self.c.get("/admin").status_code, 200)
        c2 = self.app.test_client()
        for _ in range(9):
            self.login(c2, pw="bad")
        self.login(c2)  # correct password but locked out
        self.assertEqual(c2.get("/admin").status_code, 302)

    def test_12_password_stored_hashed(self):
        with self.app.get_db() as c:
            h = c.execute("SELECT password_hash FROM admins").fetchone()[0]
        self.assertNotIn("Str0ng", h); self.assertTrue(h.startswith(("scrypt", "pbkdf2")))

    def test_13_search_and_export_and_backup(self):
        self.register(name="Priya Verma", utr="AAAA11112222", phone="9000000001")
        self.register(name="Rohan Gupta", utr="BBBB11112222", phone="9000000002")
        self.login()
        self.assertIn(b"Priya Verma", self.c.get("/admin?q=Priya").data)
        self.assertNotIn(b"Rohan Gupta", self.c.get("/admin?q=Priya").data)
        self.assertIn(b"Rohan Gupta", self.c.get("/admin?q=9000000002").data)
        csv = self.c.get("/admin/export.csv"); self.assertIn(b"Priya Verma", csv.data)
        z = zipfile.ZipFile(io.BytesIO(self.c.get("/admin/backup.zip").data))
        names = z.namelist(); self.assertIn("afterparty.sqlite3", names)
        self.assertTrue(any(n.startswith("payment-proofs/") for n in names))
        d = Path(self.tmp.name, "restored.sqlite3"); d.write_bytes(z.read("afterparty.sqlite3"))
        self.assertEqual(sqlite3.connect(d).execute("SELECT COUNT(*) FROM registrations").fetchone()[0], 2)

    def test_14_csv_injection_neutralised(self):
        self.register(name="Normal Name")
        with self.app.get_db() as c:
            c.execute("UPDATE registrations SET utr='=CMD1234567'")
        self.login()
        self.assertIn(b"'=CMD1234567", self.c.get("/admin/export.csv").data)


class TicketTests(Base):
    def test_20_ticket_generated_only_after_approval(self):
        self.register()
        reg = self.row(utr="123456789012")
        self.assertIsNone(reg["ticket_filename"])
        self.assertEqual(self.c.get(f"/ticket/{reg['status_token']}").status_code, 404)
        self.login()
        res = self.review(reg["id"], "approve")
        self.assertIn(b"Approved", res.data)
        reg = self.row(utr="123456789012")
        self.assertEqual(reg["status"], "Approved")
        self.assertTrue(reg["ticket_reference"].startswith("APT-"))
        dl = self.c.get(f"/ticket/{reg['status_token']}")
        self.assertEqual(dl.status_code, 200); self.assertEqual(dl.mimetype, "image/png")

    def test_21_name_sharp_positioned_and_artwork_untouched(self):
        for name in ("Aarav Sharma", "Devansh Tomar", "Al", "Priyanka Venkata Subramaniam Iyer",
                     "Dr. Mary-Anne O'Neil Fernandes de Souza Pereira"):
            out = Path(self.tmp.name, "x.png")
            ticketgen.make_ticket_png(name, "APT-TEST0001", "tok" * 10, out)
            t = Image.open(out).convert("RGB")
            top = t.crop((0, 0, 1600, 517))
            diff = ImageChops.difference(top, ART).convert("L").point(lambda v: 255 if v else 0)
            bbox = diff.getbbox()
            self.assertIsNotNone(bbox, name)
            x0, y0, x1, y1 = bbox
            # inside the upper-right area, right of "PARTY", clear of the title and the pink panel
            self.assertGreater(x0, 700, name); self.assertLess(x1, 1260, name)
            self.assertGreater(y0, 20, name); self.assertLess(y1, 100, name)
            # sharp: every changed pixel is EXACTLY the text colour or the shadow colour (no blur / anti-aliasing)
            a, b = np.array(top), np.array(ART)
            changed = np.any(a != b, axis=2)
            colours = {tuple(p) for p in a[changed]}
            self.assertLessEqual(colours, {ticketgen.NAME_COLOUR, ticketgen.SHADOW}, name)
            # nothing else on the artwork changed; ticket keeps the artwork's original width
            self.assertEqual(t.size[0], 1600)

    def test_22_ticket_dimensions_and_qr_decodes(self):
        reg, token = self.approved_ticket()
        self.assertTrue(len(token) >= 30)
        t = Image.open(Path(self.app.config["TICKET_DIR"]) / reg["ticket_filename"])
        self.assertEqual(t.size, (1600, 517 + ticketgen.STUB_H))

    def test_23_qr_tokens_unique_and_not_stored_plain(self):
        r1, t1 = self.approved_ticket("Aarav Sharma", "111111111111")
        r2, t2 = self.approved_ticket("Priya Verma", "222222222222")
        self.assertNotEqual(t1, t2); self.assertNotEqual(r1["ticket_token_hash"], r2["ticket_token_hash"])
        with self.app.get_db() as c:
            dump = " ".join(str(v) for row in c.execute("SELECT * FROM registrations") for v in tuple(row))
        self.assertNotIn(t1, dump); self.assertNotIn(t2, dump)


class GateTests(Base):
    def test_30_first_scan_allows_second_denies(self):
        reg, token = self.approved_ticket()
        a = self.scan(token); b = self.scan(token)
        self.assertEqual(a.status_code, 200); self.assertEqual(a.json["result"], "VALID — ENTRY ALLOWED")
        self.assertEqual(b.status_code, 409); self.assertEqual(b.json["result"], "ALREADY USED — ENTRY DENIED")
        row = self.row(id=reg["id"]); self.assertEqual(row["checked_in"], 1); self.assertTrue(row["checked_in_at"])
        with self.app.get_db() as c:
            log = [r["result"] for r in c.execute("SELECT result FROM scan_log ORDER BY id")]
        self.assertEqual(log, ["VALID", "ALREADY USED"])

    def test_31_invalid_qr_denied(self):
        self.approved_ticket()
        for bad in ("short", "A" * 40, "not a ticket!!!!!!!!!!!!!!!!!!!!!!!", "https://example.com/" + "a" * 30):
            r = self.scan(bad)
            self.assertIn(r.status_code, (400, 404)); self.assertIn("INVALID TICKET", r.json["result"])

    def test_32_rejected_and_cancelled_tickets_cannot_enter(self):
        self.register(utr="999999999999")
        self.login()
        rej = self.row(utr="999999999999")
        self.review(rej["id"], "reject", reason="Payment not received")
        self.assertEqual(self.row(utr="999999999999")["status"], "Rejected")
        self.assertIsNone(self.row(utr="999999999999")["ticket_filename"])  # no ticket exists to scan
        page = self.c.get(f"/status/{rej['status_token']}"); self.assertIn(b"Payment not received", page.data)
        self.assertEqual(self.c.get(f"/ticket/{rej['status_token']}").status_code, 404)
        reg, token = self.approved_ticket("Priya Verma", "888888888888")
        self.review(reg["id"], "revoke")
        r = self.scan(token); self.assertEqual(r.status_code, 403); self.assertIn("NOT APPROVED", r.json["result"])
        self.review(reg["id"], "reopen")
        self.assertEqual(self.scan(token).status_code, 200)

    def test_33_simultaneous_scans_admit_exactly_one(self):
        reg, token = self.approved_ticket()
        clients = []
        for _ in range(25):
            c = self.app.test_client(); self.login(c); clients.append(c)
        results, barrier = [], threading.Barrier(25)
        def go(c):
            barrier.wait(); results.append(self.scan(token, c).status_code)
        ts = [threading.Thread(target=go, args=(c,)) for c in clients]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(results.count(200), 1, results)
        self.assertEqual(results.count(409), 24, results)

    def test_34_manual_fallback_lookup_and_admit(self):
        reg, token = self.approved_ticket()
        with self.c.session_transaction() as s: t = s["_csrf"]
        h = {"X-CSRF-Token": t}
        self.assertEqual(self.c.post("/api/manual-lookup", json={"reference": "apt-nope"}, headers=h).status_code, 404)
        look = self.c.post("/api/manual-lookup", json={"reference": reg["ticket_reference"].lower()}, headers=h)
        self.assertEqual(look.status_code, 200)
        self.assertEqual(self.row(id=reg["id"])["checked_in"], 0)   # lookup alone must not use the ticket
        ok = self.c.post("/api/manual-checkin", json={"reference": reg["ticket_reference"]}, headers=h)
        self.assertEqual(ok.json["result"], "VALID — ENTRY ALLOWED")
        again = self.c.post("/api/manual-checkin", json={"reference": reg["ticket_reference"]}, headers=h)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.scan(token).status_code, 409)         # QR now also shows already used


class PersistenceTests(Base):
    def test_40_data_survives_restart(self):
        reg, token = self.approved_ticket()
        self.scan(token)
        app2 = appmod.create_app(self.cfg)           # simulates restarting the server
        c2 = app2.test_client(); self.login(c2)
        self.assertIn(b"Aarav Sharma", c2.get("/admin").data)
        self.assertEqual(self.scan(token, c2).status_code, 409)   # still marked USED after restart
        self.assertTrue((Path(self.cfg["TICKET_DIR"]) / reg["ticket_filename"]).exists())

    def test_41_find_my_ticket(self):
        self.register()
        reg = self.row(utr="123456789012")
        t = self.csrf()
        ok = self.c.post("/find", data={"_csrf": t, "reference": reg["reference"].lower(), "phone": "98765 43210"})
        self.assertEqual(ok.status_code, 302); self.assertIn("/status/", ok.headers["Location"])
        bad = self.c.post("/find", data={"_csrf": t, "reference": reg["reference"], "phone": "9000000000"})
        self.assertIn("/#find", bad.headers["Location"])

    def test_42_csrf_enforced(self):
        self.assertEqual(self.c.post("/register", data={"full_name": "x"}).status_code, 400)

    def test_43_pages_render(self):
        for u in ("/", "/admin/login", "/healthz"):
            self.assertEqual(self.c.get(u).status_code, 200)
        self.assertIn("₹1,300", self.c.get("/").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
