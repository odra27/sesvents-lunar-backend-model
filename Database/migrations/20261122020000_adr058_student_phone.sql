-- ADR-058 — Student Phone Number on Self-Registration & Profile.
--
-- Non-destructive & idempotent:
--   Adds nullable `student_profiles.phone` (varchar(25)) so existing accounts remain valid while
--   new self-registrations require a validated phone number on `POST /api/student/auth/register`.

alter table student_profiles
  add column if not exists phone varchar(25);
