-- ADR-061 — Defense-in-Depth Database Hardening: RLS on Remaining Tables & Security-Invoker Views.
--
-- Non-destructive & idempotent:
--   1. Enables Row Level Security (without permissive policies) on the 5 remaining `public`
--      tables (`devices`, `institutions`, `matching_logs`, `phrases`, `sign_references`) so
--      all 11 application tables share the same defense-in-depth posture.
--      The Backend connects via `DATABASE_URL` (table owner / bypassrls) and is unaffected.
--   2. Switches the 3 reporting views (`user_login_account_summary`, `user_login_sessions_status`,
--      `user_login_visitor_summary`) to `security_invoker = true` so they never execute with
--      elevated owner privileges for lower-privileged roles.

alter table public.devices enable row level security;
alter table public.institutions enable row level security;
alter table public.matching_logs enable row level security;
alter table public.phrases enable row level security;
alter table public.sign_references enable row level security;

alter view public.user_login_account_summary set (security_invoker = true);
alter view public.user_login_sessions_status set (security_invoker = true);
alter view public.user_login_visitor_summary set (security_invoker = true);
