-- ADR-060 — Drop Legacy Columns from `student_profiles` (Step 2, after Backend deploy).
--
-- Drops the 5 unused B2B/school legacy columns from `student_profiles`:
--   - institution_id
--   - nis
--   - class_name
--   - grade
--   - class_code
--
-- Precondition (verified before applying):
--   Backend runtime no longer queries or writes any of these 5 columns, and the 3
--   `user_login_*` reporting views no longer reference `nis` (ADR-059).

alter table student_profiles
  drop column if exists institution_id,
  drop column if exists nis,
  drop column if exists class_name,
  drop column if exists grade,
  drop column if exists class_code;
