-- ADR-059 — Safe Package A Cleanup: Tracking Views (email + phone), Backfill account_type, Drop Unused sign_references Columns.
--
-- Idempotent & zero runtime impact on live API:
--   1. Backfills `student_profiles.account_type = 'student'` where NULL.
--   2. Drops unused columns `sign_references.landmark_data` and `sign_references.signer_id`
--      (never read or written by Backend runtime; always NULL).
--   3. Updates the 3 reporting views (`user_login_sessions_status`, `user_login_account_summary`,
--      `user_login_visitor_summary`) to display `email` and `phone` instead of legacy `nis`.

update student_profiles
set account_type = 'student'
where account_type is null;

alter table sign_references
  drop column if exists landmark_data,
  drop column if exists signer_id;

drop view if exists user_login_visitor_summary;
drop view if exists user_login_account_summary;
drop view if exists user_login_sessions_status;

create view user_login_sessions_status as
select
  uls.session_id,
  uls.user_id,
  uls.visitor_id,
  u.email,
  sp.phone,
  sp.full_name,
  uls.account_type,
  (uls.login_at at time zone 'Asia/Jakarta') as login_at_wib,
  (uls.last_activity_at at time zone 'Asia/Jakarta') as last_activity_at_wib,
  (uls.logout_at at time zone 'Asia/Jakarta') as logout_at_wib,
  (
    case
      when uls.logout_at is not null then uls.logout_at
      when now() - uls.last_activity_at > interval '30 minutes' then uls.last_activity_at
      else null
    end at time zone 'Asia/Jakarta'
  ) as effective_ended_at_wib,
  greatest(
    0,
    extract(epoch from (coalesce(uls.logout_at, uls.last_activity_at) - uls.login_at))::integer
  ) as session_duration_seconds,
  case
    when uls.logout_at is not null then 'logged_out'
    when now() - uls.last_activity_at <= interval '30 minutes' then 'active'
    else 'expired'
  end as session_status,
  uls.login_at,
  uls.last_activity_at,
  uls.logout_at,
  case
    when uls.logout_at is not null then uls.logout_at
    when now() - uls.last_activity_at > interval '30 minutes' then uls.last_activity_at
    else null
  end as effective_ended_at
from user_login_sessions uls
join student_profiles sp on sp.id = uls.user_id
left join auth.users u on u.id = sp.id;

create view user_login_account_summary as
select
  sp.id as user_id,
  u.email,
  sp.phone,
  sp.full_name,
  coalesce(sp.account_type, max(uls.account_type), 'student') as account_type,
  count(uls.session_id) > 0 as ever_used,
  count(uls.session_id)::integer as total_logins,
  count(distinct uls.visitor_id)::integer as unique_visitors,
  (max(uls.login_at) at time zone 'Asia/Jakarta') as last_login_at_wib,
  max(uls.login_at) as last_login_at,
  count(uls.session_id) filter (
    where uls.logout_at is null
      and now() - uls.last_activity_at <= interval '30 minutes'
  )::integer as active_sessions,
  coalesce(
    sum(
      greatest(
        0,
        extract(epoch from (coalesce(uls.logout_at, uls.last_activity_at) - uls.login_at))::integer
      )
    ),
    0
  )::integer as total_duration_seconds
from student_profiles sp
left join auth.users u on u.id = sp.id
left join user_login_sessions uls on uls.user_id = sp.id
group by sp.id, u.email, sp.phone, sp.full_name, sp.account_type;

create view user_login_visitor_summary as
select
  uls.visitor_id,
  uls.account_type,
  uls.user_id,
  u.email,
  sp.phone,
  count(uls.session_id)::integer as total_logins,
  (min(uls.login_at) at time zone 'Asia/Jakarta') as first_login_at_wib,
  (max(uls.login_at) at time zone 'Asia/Jakarta') as last_login_at_wib,
  min(uls.login_at) as first_login_at,
  max(uls.login_at) as last_login_at,
  count(uls.session_id) filter (
    where uls.logout_at is null
      and now() - uls.last_activity_at <= interval '30 minutes'
  )::integer as active_sessions,
  coalesce(
    sum(
      greatest(
        0,
        extract(epoch from (coalesce(uls.logout_at, uls.last_activity_at) - uls.login_at))::integer
      )
    ),
    0
  )::integer as total_duration_seconds
from user_login_sessions uls
join student_profiles sp on sp.id = uls.user_id
left join auth.users u on u.id = sp.id
where uls.visitor_id is not null
group by uls.visitor_id, uls.account_type, uls.user_id, u.email, sp.phone;
