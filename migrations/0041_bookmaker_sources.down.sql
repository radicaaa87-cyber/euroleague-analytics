-- Revert Meridian/Millennium archive enum-like checks conservatively.

update bookmaker_player_points_offer
set bookmaker = 'other'
where bookmaker in ('meridian', 'millennium');

update bookmaker_source_document
set bookmaker = 'other'
where bookmaker in ('meridian', 'millennium');

update bookmaker_source_document
set discovery_method = 'manual'
where discovery_method = 'listing_page';

alter table bookmaker_source_document
    drop constraint if exists bookmaker_source_document_bookmaker_check;

alter table bookmaker_source_document
    add constraint bookmaker_source_document_bookmaker_check
    check (bookmaker in ('mozzart', 'starbet', 'sportlife', 'other'));

alter table bookmaker_player_points_offer
    drop constraint if exists bookmaker_player_points_offer_bookmaker_check;

alter table bookmaker_player_points_offer
    add constraint bookmaker_player_points_offer_bookmaker_check
    check (bookmaker in ('mozzart', 'starbet', 'sportlife', 'other'));

alter table bookmaker_source_document
    drop constraint if exists bookmaker_source_document_discovery_method_check;

alter table bookmaker_source_document
    add constraint bookmaker_source_document_discovery_method_check
    check (discovery_method in ('seed', 'wayback_cdx', 'brave_search', 'manual'));
