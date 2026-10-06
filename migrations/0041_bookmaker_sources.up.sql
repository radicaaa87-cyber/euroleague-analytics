-- Extend bookmaker archive to Meridian and Millennium sources.

alter table bookmaker_source_document
    drop constraint if exists bookmaker_source_document_bookmaker_check;

alter table bookmaker_source_document
    add constraint bookmaker_source_document_bookmaker_check
    check (bookmaker in ('mozzart', 'starbet', 'meridian', 'millennium', 'sportlife', 'other'));

alter table bookmaker_player_points_offer
    drop constraint if exists bookmaker_player_points_offer_bookmaker_check;

alter table bookmaker_player_points_offer
    add constraint bookmaker_player_points_offer_bookmaker_check
    check (bookmaker in ('mozzart', 'starbet', 'meridian', 'millennium', 'sportlife', 'other'));

alter table bookmaker_source_document
    drop constraint if exists bookmaker_source_document_discovery_method_check;

alter table bookmaker_source_document
    add constraint bookmaker_source_document_discovery_method_check
    check (discovery_method in ('seed', 'wayback_cdx', 'brave_search', 'listing_page', 'manual'));
