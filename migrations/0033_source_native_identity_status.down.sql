alter table athlete_source_identity
    drop constraint if exists athlete_source_identity_match_status_check;

alter table athlete_source_identity
    add constraint athlete_source_identity_match_status_check
    check (match_status in ('auto_link', 'manual_verified', 'review'));
