alter table athlete_source_identity
    drop constraint if exists athlete_source_identity_match_status_check;

alter table athlete_source_identity
    add constraint athlete_source_identity_match_status_check
    check (match_status in ('source_native', 'auto_link', 'manual_verified', 'review'));

comment on column athlete_source_identity.match_status is
    'source_native = identity proven only within one source; auto/manual statuses describe cross-source linking evidence.';
