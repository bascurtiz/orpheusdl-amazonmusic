Continue the approved simplification pass of orpheusdl-amazonmusic (plan saved at `.zcode/plans/plan-sess_19cca6c5-e5b2-4528-97be-3a90c001539e.md`). Steps 1–3 and 6 are fully executed; both copy-paste bug fixes are in; current state is 5,166 LOC (azapi 1,361 / interface 3,631 / models 174) vs 6,426 baseline. Remaining work:

## Phase A — finish the 3 leftover merges from the approved plan (interface.py, zero functional change)

1. **Merge `_spatial_display_tokens`** (interface.py:1301) into `_merge_manifest_quality_labels` (interface.py:1328). Move the Atmos/RA360 badge-detection loop body inline at its single call site (interface.py:1349) and delete the staticmethod.
2. **Inline `_metadata_artist_name`** (interface.py:1105) into its sole caller in `get_album_info` (interface.py:1720). Replace the call with the helper body verbatim (keeps the `primaryArtistName`/`artistName`/`albumArtistName` fallbacks), delete the staticmethod.
3. **Inline `_playlist_payload_is_complete`** (interface.py:1663) into its sole use in `get_playlist_info` (interface.py:1837). Expand the predicate into the `if cached and ...` branch, delete the staticmethod.
4. **Deliberate skip (deviation):** PSSHEntitlements direct-field-iteration — `to_dict` is already removed; the remaining 5-line `iterate` generator has two internal consumers and replacing `dataclasses.fields` with explicit field tuples would regress maintainability for ~3 lines.

## Phase B — Step 7 verification (blocked in plan mode, must run after approval)

1. `python -m py_compile azapi.py interface.py models.py`.
2. Grep to confirm zero remaining references to every deleted symbol: `APIError`, `get`, `get_root`, `find_item_by_asin_in_search_results`, `get_documents_from_search_results`, `get_recent_tracks`, `_now_to_unix_ms`, `_get_app_metadata`, `retrieve_customer_home`, `_deauthorize_device`, `_internal_login`, `parse_for_app_config`, `_probe_quality_labels`, `_pick_spatial_display_label`, `AudioTrack.to_dict`, `quality_ranking`, `SEARCH_PROBE_SPATIAL_TIER`, `access_token_expires`, `AmazonRegion.to_dict`, plus the 3 newly inlined helpers.
3. Re-confirm preservation guarantees: all framework hook signatures (`module_information`, `validate_amazonmusic_setup`, and the 16 `ModuleInterface` methods), dataclass field set + `from_dict` legacy-key migration + `is_dict_of_instance` (already verified this session), `lru_cache` on `get_metadata`/`get_account_status`/`select_session`, `search` dual return shape (dict when `asins` given, list otherwise), `get_track_lyrics` region→tld map, `_wait_for_response` retry semantics, ThreadPoolExecutor manifest batching (4 workers / chunks of 10), `tracks_to_quality_map` None-key filtering, and that all 16 `mobile_session.*` calls resolve to azapi methods (already verified this session).
4. Full `git diff` review against the preservation list.
5. Final report: per-file and total LOC vs the 6,426 baseline. Expected landing: ~5,120 total (≈ -20%). The original ~28-32% projection was optimistic; per user direction this pass ends here — no new consolidation beyond the approved plan.

No commit will be made unless requested.