"""Operator CLI to provision a student account in Supabase Auth and `student_profiles`.

Examples (ADR-051 D2, ADR-056, ADR-057, ADR-058):
    cd backend
    .venv/bin/python scripts/create_student.py --email demo.lunar@gmail.com --name "Akun Demo" --phone 081234567890 --account-type demo
    .venv/bin/python scripts/create_student.py --email juri.lunar@gmail.com --name "Akun Juri" --phone 081234567891 --account-type judge

Password is read securely via getpass prompt (never echoed) or `STUDENT_PASSWORD` env var.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from app.errors import Conflict  # noqa: E402
from app.provisioning.supabase_admin import SupabaseAdminClient, WeakPassword  # noqa: E402
from scripts.apply_migrations import _load_env_file  # noqa: E402

_PHONE_RE = re.compile(r"^0[0-9]{7,14}$")


def parse() -> argparse.Namespace:
    """Parse CLI arguments for student account provisioning."""
    p = argparse.ArgumentParser(description="Daftarkan akun siswa (Email + password).")
    p.add_argument("--email", required=True, help="Email login siswa (mis. demo.lunar@gmail.com).")
    p.add_argument("--name", required=True, help="Nama lengkap siswa.")
    p.add_argument("--phone", required=True, help="Nomor telepon siswa (awalan 0, 8-15 digit, tanpa spasi/strip).")
    p.add_argument(
        "--account-type",
        choices=["demo", "judge", "student"],
        default="student",
        help="Tipe akun untuk tracking ('demo', 'judge', atau 'student'; default: 'student').",
    )
    return p.parse_args()


async def main() -> int:
    """Create the Supabase Auth user and matching `student_profiles` row (with rollback on DB error)."""
    _load_env_file()
    a = parse()
    email = a.email.strip().lower()
    if not email or "@" not in email:
        print("Wajib mengisi --email yang valid.", file=sys.stderr)
        return 2
    phone = a.phone.strip()
    if not _PHONE_RE.match(phone):
        print("Nomor telepon harus diawali 0 dan berisi 8-15 digit angka saja.", file=sys.stderr)
        return 2
    password = os.environ.get("STUDENT_PASSWORD") or getpass.getpass("Password siswa (min. 8 karakter): ")
    if len(password) < 8:
        print("Password minimal 8 karakter.", file=sys.stderr)
        return 2

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL belum di-set (muat backend/.env dulu).", file=sys.stderr)
        return 2

    with psycopg.connect(dsn, autocommit=True, prepare_threshold=None) as conn:
        if conn.execute("select to_regclass('student_profiles')").fetchone()[0] is None:
            print("Tabel student_profiles belum ada. Jalankan migration dulu.", file=sys.stderr)
            return 2

        admin = SupabaseAdminClient.from_env()
        try:
            try:
                user_id = await admin.create_user(email=email, password=password, role="siswa")
            except Conflict:
                print(f"Akun Auth untuk email {email} sudah ada di Supabase.", file=sys.stderr)
                return 1
            except WeakPassword:
                print("Password ditolak kebijakan password Supabase. Pakai password yang lebih kuat.", file=sys.stderr)
                return 1
            try:
                conn.execute(
                    "insert into student_profiles (id, full_name, phone, account_type) values (%s, %s, %s, %s)",
                    (user_id, a.name.strip(), phone, a.account_type),
                )
            except Exception:
                await admin.delete_user(user_id)  # rollback: jangan tinggalkan akun tanpa profil
                raise
        finally:
            await admin.aclose()

    print(f"OK: siswa {a.name} ({email}) [account_type={a.account_type}] terdaftar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
