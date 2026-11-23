"""Operator CLI to provision a student account in Supabase Auth and `student_profiles`.

Examples (ADR-051 D2, ADR-056, ADR-057):
    cd backend
    .venv/bin/python scripts/create_student.py --email demo.lunar@gmail.com --name "Akun Demo" --grade 5 --account-type demo
    .venv/bin/python scripts/create_student.py --email juri.lunar@gmail.com --name "Akun Juri" --grade 5 --account-type judge

Password is read securely via getpass prompt (never echoed) or `STUDENT_PASSWORD` env var.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from app.errors import Conflict  # noqa: E402
from app.provisioning.supabase_admin import SupabaseAdminClient, WeakPassword  # noqa: E402
from scripts.apply_migrations import _load_env_file  # noqa: E402

EMAIL_DOMAIN = "siswa.isyara.local"


def parse() -> argparse.Namespace:
    """Parse CLI arguments for student account provisioning."""
    p = argparse.ArgumentParser(description="Daftarkan akun siswa (Email + password).")
    p.add_argument("--email", help="Email login siswa (mis. demo.lunar@gmail.com). Wajib jika --nis tidak diisi.")
    p.add_argument("--nis", help="Opsional: NIS siswa (legacy / jalur sekolah).")
    p.add_argument("--name", required=True, help="Nama panggilan / lengkap siswa.")
    p.add_argument("--class", dest="class_name")
    p.add_argument("--grade", type=int, choices=range(1, 13), metavar="1-12")
    p.add_argument("--class-code", dest="class_code", help="Opsional: kode kelas dari sekolah (mis. LNR-5A21).")
    p.add_argument("--school", help="Opsional: nama sekolah (institutions.name).")
    p.add_argument(
        "--account-type",
        choices=["demo", "judge"],
        default=None,
        help="Opsional: tandai akun khusus untuk login activity tracking ('demo' atau 'judge', ADR-055).",
    )
    return p.parse_args()


async def main() -> int:
    """Create the Supabase Auth user and matching `student_profiles` row (with rollback on DB error)."""
    _load_env_file()
    a = parse()
    nis = a.nis.strip() if a.nis else None
    email = a.email.strip().lower() if a.email else (f"{nis.lower()}@{EMAIL_DOMAIN}" if nis else None)
    if not email or "@" not in email:
        print("Wajib mengisi --email yang valid (atau --nis).", file=sys.stderr)
        return 2
    if nis is not None and (not nis or len(nis) > 30):
        print("NIS maksimal 30 karakter.", file=sys.stderr)
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
        if nis is not None and conn.execute("select 1 from student_profiles where nis = %s", (nis,)).fetchone():
            print(f"NIS {nis} sudah terdaftar.", file=sys.stderr)
            return 1
        school_id = None
        if a.school:
            row = conn.execute("select id from institutions where name = %s limit 1", (a.school,)).fetchone()
            school_id = row[0] if row else conn.execute(
                "insert into institutions (name) values (%s) returning id", (a.school,)
            ).fetchone()[0]

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
                    "insert into student_profiles"
                    " (id, institution_id, nis, full_name, class_name, grade, class_code, account_type)"
                    " values (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (user_id, school_id, nis, a.name.strip(), a.class_name, a.grade, a.class_code, a.account_type),
                )
            except Exception:
                await admin.delete_user(user_id)  # rollback: jangan tinggalkan akun tanpa profil
                raise
        finally:
            await admin.aclose()

    tag = f" [account_type={a.account_type}]" if a.account_type else ""
    print(f"OK: siswa {a.name} ({email}){tag} terdaftar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
