"""One-time setup: asks for the admin username/password and writes the .env file."""
import getpass, secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".env"

existing = {}
if ENV.exists():
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            existing[k.strip()] = v.strip()

print("\n=== AFTERPARTY SETUP ===")
if existing.get("ADMIN_PASSWORD") and not existing["ADMIN_PASSWORD"].startswith("replace-with"):
    ans = input(f"Admin '{existing.get('ADMIN_USERNAME')}' is already set up. Change it? (y/N): ").strip().lower()
    if ans != "y":
        print("Keeping your existing login.")
        raise SystemExit(0)

user = input("Choose an admin username [organiser]: ").strip() or "organiser"
while True:
    pw = getpass.getpass("Choose an admin password (at least 10 characters; typing is hidden): ")
    if len(pw) < 10:
        print("Too short. Use at least 10 characters."); continue
    if pw != getpass.getpass("Type it again: "):
        print("They did not match. Try again."); continue
    break

example = {}
for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
    if line.strip() and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); example[k.strip()] = v.strip()
values = {**example, **existing, "ADMIN_USERNAME": user, "ADMIN_PASSWORD": pw}
values["SECRET_KEY"] = values.get("SECRET_KEY") or secrets.token_urlsafe(48)
ENV.write_text("# Afterparty settings - keep private\n" + "\n".join(f"{k}={v}" for k, v in values.items()) + "\n", encoding="utf-8")
print("\nDone. Your admin login is saved.  Username:", user)
