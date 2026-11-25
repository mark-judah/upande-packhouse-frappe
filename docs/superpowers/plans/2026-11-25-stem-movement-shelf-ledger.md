# Stem Movement Shelf Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Shelving Log a complete, truthful shelf ledger and rebuild the Stem Movement v2 page around it (opening stock at midnight, every entry, every exit by named reason, closing, and an explicit unexplained line).

**Architecture:** Each Shelving Log row is a lot of stems on a shelf from `shelved_on` to `removed_on`. One helper module is the only way writers open, close or split lots. A read model computes any day's ledger from the lots in site time (Nairobi) and checks it against `core/stock.bucket_rows`. A one-off repair closes the stale open lots. The page reads the read model.

**Tech Stack:** Frappe v16 (Python, MariaDB), `www/*.html` pages with inline JS, Frappe `IntegrationTestCase` / `UnitTestCase`, bench site `kaitet.local`.

**Spec:** `docs/superpowers/specs/2026-11-25-stem-movement-shelf-ledger-design.md`

## Global Constraints

- No new ledger doctype and no snapshots; the Shelving Log is the ledger (decided 2026-11-25).
- Day boundary is midnight in **site time** (Nairobi). The database stores UTC (three hours behind), so never use `DATE(col)` on stored values for a day edge; convert the day's start and end to the stored timezone first.
- No event time exists. Every time is server time; offline actions land on the sync day and the page says so.
- Unexplained stems show as a **red banner** listing the buckets and likely writers; the figures still render.
- The 5,000-plus open lots with no Shelf Item are closed as reason `Unrecorded exit` with `removed_on` = last evidence of the bucket (latest Stock Entry or later shelving of the same bucket); past days carry a "repaired" label.
- Closing stock and its state split use `core/stock.bucket_rows` and `stock_truth.classify_shelf_row`. No second definition of free or available.
- Reasons are matched exactly, never by substring.
- A closed log row is never edited and never hard-deleted. Rows are only closed, split or opened.
- Writers live in three apps (`upande_packhouse`, `upande_quality`, `upande_agriculture`); each repo gets its own commit and PR. Nothing is pushed without the user's go-ahead.
- Stem Movement's pipeline funnel (harvested to dispatched) stays, below the ledger.
- Start from `upstream/develop` on a new branch `feat/shelf-ledger`. The unfinished Stock Visibility work on `feat/stock-visibility-withheld` is committed or stashed first, not mixed in.

## Review Focus

- A bucket shelved and cleared and reshelved within one day, same bucket id, same variety: must be three lots, not one, and the day's in/out must count each.
- A lot shelved at 23:30 UTC (02:30 Nairobi next day): must land on the Nairobi day, in the right day's opening and flows.
- A multi-variety bucket (two varieties on one bucket id): lots, splits and exits are per bucket + variety + length, never per bucket.
- A lot with `stem_qty` 0 or `shelved_on` NULL (741 and 251 rows exist): must not crash the page; it shows in a data-quality count.
- A partial draw of the whole remaining quantity (draw equals remainder): closes the lot with no zero-quantity "Carried" row.
- Running the repair twice, or on a day already repaired: idempotent, no second close.
- A date range of 90 days across all farms: opening/in/out must come from indexed range queries and return in seconds.
- Shelf-to-shelf between two farms: out of one farm, in to the other, nets to zero for the region, shows per farm.

## File Structure

Created:
- `upande_packhouse/shelf_ledger.py`: the writer helper (`open_lot`, `close_lot`, `draw`), reasons and sources constants.
- `upande_packhouse/api/v2/shelf_ledger.py`: the read model (`day_ledger`, `range_ledger`, `invariants`, `lot_rows`).
- `upande_packhouse/patches/repair_open_shelf_lots.py`: the one-off repair, also callable with `dry_run`.
- `upande_packhouse/upande_packhouse/doctype/shelf_control_total/`: one row per farm per night.
- Tests: `tests/test_shelf_ledger_helper.py`, `tests/test_v2_shelf_ledger.py`, `tests/test_shelf_ledger_repair.py`, `tests/test_shelf_ledger_writers.py`.

Modified:
- `upande_packhouse/upande_packhouse/doctype/shelving_log/shelving_log.json` (fields, reasons, indexes).
- `upande_packhouse/mobile/api.py`, `api/offline_issue.py`, `api/packing_reject.py`, `merge_duplicate_shelf_items.py`, `api/remote_transfer/transfer_scheduling.py`, `upande_packhouse/page/sales_allocation/sales_allocation.py` and `.../sales_allocation_v2/sales_allocation_v2.py` (`_clear_shelf_item`).
- `upande_quality/mobile/api.py` and `upande_agriculture/mobile/api.py` (separate repos).
- `api/v2/stem_movement.py` (use the read model), `www/stem-movement-v2.html` (redesign), `hooks.py` (daily control job), `patches.txt`.

---

### Task 1: Shelving Log fields, reasons, indexes

**Files:**
- Modify: `upande_packhouse/upande_packhouse/doctype/shelving_log/shelving_log.json`
- Create: `upande_packhouse/patches/add_shelving_log_ledger_fields.py`; add to `patches.txt`
- Test: `tests/test_shelf_ledger_helper.py` (meta test only)

**Interfaces:**
- Produces: field `source` (Select: `Online`, `Offline recorded`, `System`, `Maintenance`, default `Online`); reasons added to the `reason` Select: entries `Reshelved`, `Returned from trolley/truck`, `Arrived (shelf-to-shelf)`, `Carried`; exits `Unrecorded exit`, `Discarded`, `Rejected`, `Stem replacement`, `Bunch moved`, `Clean-up`. Existing reasons stay. a `detail` field (Small Text) for the typed reason of a farm-app removal; DB indexes on `shelved_on`, `removed_on`, and `(farm, removed_on)`.

- [ ] **Step 1: Write the failing test** `test_shelving_log_meta`: `frappe.get_meta("Shelving Log")` has `source`, its options are the four values, and every new reason appears in `reason` options.
- [ ] **Step 2: Run it** `bench --site kaitet.local run-tests --app upande_packhouse --module upande_packhouse.tests.test_shelf_ledger_helper --test test_shelving_log_meta`; expect FAIL.
- [ ] **Step 3: Edit the doctype JSON** (field after `reason`, options, index flags) and add the patch that runs `frappe.db.add_index("Shelving Log", ["farm", "removed_on"], "idx_farm_removed")` and the two single-column indexes if missing; the patch is idempotent.
- [ ] **Step 4: Run `bench --site kaitet.local migrate`, then the test**; expect PASS. Check `SHOW INDEX FROM \`tabShelving Log\`` lists the three.
- [ ] **Step 5: Commit** `feat: shelving log source field, ledger reasons and indexes`.

### Task 2: The writer helper

**Files:**
- Create: `upande_packhouse/shelf_ledger.py`
- Test: `tests/test_shelf_ledger_helper.py`

**Interfaces:**
- Produces:
  - `open_lot(*, bucket_id, variety, stem_length, stem_qty, shelf, farm, reason, source="Online", shelf_item=None, **extra) -> str` (inserts a Shelving Log row with `shelved_on = now`, returns its name; `reason` must be an entry reason).
  - `close_lot(log_name, *, reason, source="Online") -> None` (sets `reason`, `removed_on = now`, `source`; refuses an already closed row; reason must be an exit reason).
  - `draw(log_name, *, qty, reason, source="Online") -> str | None`: closes the lot with `stem_qty = qty` and the exit reason; if stems remain, opens a new lot (same bucket, variety, length, shelf, farm, `reason="Carried"`, `shelved_on = now`, `stem_qty = remaining`) and returns its name; returns `None` when `qty >= stem_qty`. `qty` must be positive and not exceed the lot.
  - `find_open_lot(bucket_id, variety, stem_length, shelf=None) -> str | None` (the single open row; if more than one is open, the newest wins and the rest are returned by `find_open_lots`).
  - Constants `ENTRY_REASONS`, `EXIT_REASONS`, `SOURCES`.

- [ ] **Step 1: Write failing tests**: `test_open_then_close_sets_times_and_reason`, `test_close_twice_raises`, `test_draw_partial_splits_and_quantities_add_up` (lot 100, draw 30: closed row 30 `Issued to Sales Order`, new open row 70 `Carried`; total in = out + open), `test_draw_equal_to_remainder_closes_without_carried_row`, `test_draw_more_than_lot_raises`, `test_multi_variety_bucket_lots_are_independent`, `test_entry_reason_rejected_on_close_and_exit_reason_rejected_on_open`.
- [ ] **Step 2: Run them**; expect FAIL (module missing).
- [ ] **Step 3: Implement the module** with `frappe.get_doc(...).insert(ignore_permissions=True)` and `frappe.db.set_value(..., update_modified=False)` for closes; no commit inside.
- [ ] **Step 4: Run the tests**; expect PASS.
- [ ] **Step 5: Commit** `feat: shelf_ledger helper (open, close, draw lots)`.

### Task 3: The read model

**Files:**
- Create: `upande_packhouse/api/v2/shelf_ledger.py`
- Test: `tests/test_v2_shelf_ledger.py`

**Interfaces:**
- Consumes: Task 2 reasons; `core/stock.bucket_rows`; `stock_truth.classify_shelf_row` for the closing state split; `region_core` for the farm filter.
- Produces (all `@frappe.whitelist(methods=["GET"])`):
  - `day_ledger(day=None, region=None, farm=None, variety=None, length=None, rose=None) -> dict` with `opening`, `in` (dict by entry reason with stems and buckets), `out` (dict by exit type with stems and buckets), `closing`, `live` (today only, from `bucket_rows`), `unexplained` (`closing - live`, today only), `closing_states` (stock_truth states, summing to `closing`), `quality` (counts of lots with null `shelved_on`, zero qty, reasons outside the known sets), `repaired` (stems closed as `Unrecorded exit` that fall on this day), `hourly` (open stems at each hour).
  - `range_ledger(from_date, to_date, ...) -> dict`: one `day_ledger`-shaped row per day with `opening_matches_previous_closing` flags.
  - `lot_rows(day, type, ..., limit, offset) -> dict`: the buckets behind a number (bucket, variety, length, shelf, stems, reason, source, times).
  - `invariants(day, ...) -> list[dict]` (`name`, `ok`, `detail`) for the six checks in spec section 7.
  - Out types map exactly from reason: `Issued to Sales Order` (split into online and offline-recorded by `source`), `Offline Issuing`, `Shelf Cleared`, `Replaced`, `Transferred (Trolley/Truck)`, `Transferred (Shelf-to-Shelf)`, `Discarded`, `Rejected`, `Stem replacement`, `Bunch moved`, `Clean-up`, `Unrecorded exit`.
- Day edges: helper `_day_bounds(day) -> (start_utc, end_utc)` converting site-time midnight to the stored timezone.

- [ ] **Step 1: Write failing tests** with planted lots (Marisa at Kapkolia, as in `test_v2_stock_truth.py`): `test_opening_is_lots_open_at_midnight_with_their_quantity_then`, `test_partial_draw_day_flows` (draw splits count out once and carry-over is not an entry), `test_lot_at_2330_utc_lands_on_next_nairobi_day`, `test_same_day_clear_and_reshelve_counts_both`, `test_shelf_to_shelf_nets_to_zero_across_region`, `test_null_shelved_at_and_zero_qty_do_not_crash_and_are_counted_in_quality`, `test_closing_states_sum_to_closing`, `test_opening_equals_previous_closing_over_a_range`.
- [ ] **Step 2: Run them**; expect FAIL.
- [ ] **Step 3: Implement** `_day_bounds`, then one range SQL per quantity (opening: `shelved_on < start AND (removed_on IS NULL OR removed_on >= start)`; in: `shelved_on` within the day with entry reasons; out: `removed_on` within the day grouped by reason and source), the live comparison, and `invariants`. Group by bucket + variety + length for bucket counts.
- [ ] **Step 4: Run the tests**; expect PASS. Then time `range_ledger` over 90 days all farms on the local data and record the time in the commit message (target under 5 s).
- [ ] **Step 5: Commit** `feat: shelf ledger read model from Shelving Log lots`.

### Task 4: Repair of the stale open lots

**Files:**
- Create: `upande_packhouse/patches/repair_open_shelf_lots.py` (function `run(dry_run=True) -> dict`; not in `patches.txt` until the user approves running it)
- Test: `tests/test_shelf_ledger_repair.py`

**Interfaces:**
- Produces: `run(dry_run=True)` returns `{"closed": n, "stems": s, "by_farm": {...}, "samples": [...]}`. It targets log rows with `removed_on IS NULL` whose bucket + variety + length has no Shelf Item, sets `reason = "Unrecorded exit"`, `source = "Maintenance"`, `removed_on` = the latest of: last Stock Entry (`custom_bucket_id`) posting time, a later shelving log row's `shelved_on` for the same bucket, and the row's own `shelved_on`. It also reports (does not change) Shelf Items whose log is closed and rows with null `shelved_on`.

- [ ] **Step 1: Write failing tests**: `test_dry_run_changes_nothing_and_reports`, `test_closes_orphan_at_last_evidence`, `test_leaves_lot_with_live_shelf_item`, `test_idempotent_second_run_closes_nothing`, `test_removed_on_never_before_shelved_on`.
- [ ] **Step 2: Run them**; expect FAIL.
- [ ] **Step 3: Implement** with set-based SQL for the orphan set and batched updates (1,000 rows per batch, no per-row loop of queries).
- [ ] **Step 4: Run the tests**; expect PASS. Run `run(dry_run=True)` on the local data and keep the report (expect about 5,553 rows, 748,891 stems).
- [ ] **Step 5: Commit** `feat: repair for open shelving lots with no shelf item (dry run by default)`.

### Task 5: Writers in upande_packhouse

**Files:**
- Modify: `upande_packhouse/mobile/api.py` (`_issue_bucket_once` partial draw ~6386 and exit ~6397-6425; `shelveBucket` log write and self-heal ~7096-7129), `api/offline_issue.py` (`issue_offline`, `mark_issued_offline`), `api/packing_reject.py:475-495`, `merge_duplicate_shelf_items.py:50-56`, `api/remote_transfer/transfer_scheduling.py` (`_restore_farm_shelf` ~2470-2566), `upande_packhouse/page/sales_allocation/sales_allocation.py:3298` and `.../sales_allocation_v2/sales_allocation_v2.py:4647` (`_clear_shelf_item`)
- Test: `tests/test_shelf_ledger_writers.py`

**Interfaces:**
- Consumes: `shelf_ledger.open_lot / close_lot / draw / find_open_lot`.
- Produces: behavior per writer:
  - Partial issue: `draw(..., reason="Issued to Sales Order")`; full issue closes the lot; `source = "Offline recorded"` when called from `issue_offline` / `mark_issued_offline`.
  - `_clear_shelf_item` (both copies call one shared implementation): closes the open lot with the given reason; when none exists, opens a lot with `shelved_on` = the Shelf Item's `date_added` (else now) and closes it, never a null `shelved_on`.
  - Reshelve self-heal: closes the stale lot as `Clean-up`.
  - `_restore_farm_shelf`: closes nothing and rewrites nothing; opens a new lot `Returned from trolley/truck`.
  - Merge duplicates: closes dropped lots as `Clean-up` and keeps one lot at the summed quantity opened as `Carried`; no `frappe.db.delete` on logs.
  - Packing-reject partial donor draw: `draw(..., reason="Stem replacement")`; empty donor closes as `Issued to Sales Order` as today.

- [ ] **Step 1: Write failing tests** (replay, one per writer, using the existing planting style): `test_partial_issue_splits_lot_and_exit_row_carries_drawn_stems`, `test_offline_issue_marks_source`, `test_clear_shelf_item_without_log_creates_closed_lot_with_real_shelved_on`, `test_clear_shelf_item_v1_and_v2_behave_the_same`, `test_restore_farm_shelf_adds_return_row_and_keeps_exit_row`, `test_merge_duplicates_closes_not_deletes_logs`, `test_self_heal_closes_stale_lot_as_cleanup`. Each also asserts the sum of open lots equals the Shelf Item stems for that bucket.
- [ ] **Step 2: Run them**; expect FAIL.
- [ ] **Step 3: Change each writer** to call the helper in the same transaction; keep behavior otherwise. Make the two `_clear_shelf_item` copies delegate to one function in `shelf_ledger.py` (`clear_shelf_item(shelf_item_name, shelf, reason)`).
- [ ] **Step 4: Run the tests and the existing `test_shelf_operations`, `test_v2_rt_journey`, `test_v2_stock_truth` modules**; expect PASS except the two known failures that fail on the original code (`test_search_finds_the_order_holding_a_bucket`, `test_expected_stems_become_real_when_the_stock_reaches_the_shelf`).
- [ ] **Step 5: Commit** per file group, message `fix: <writer> keeps the shelving log lots true`.

### Task 6: Writers in upande_quality, upande_agriculture, and the database script

**Files:**
- Modify: `apps/upande_quality/upande_quality/mobile/api.py` (`createDiscardEntry` remove-from-shelf ~905-925, `createShelvingEntry` self-heal ~2553-2567, `transferBucket` ~13863-13912, `replaceStems` ~9188, `replaceBunchInOpl` ~9411, `moveBunch` ~8094/8185, `createOfflineIssuingEntry` ~14041-14125, `saveTrolleyData` and `setOfflineTrolleyFlags` removals), `apps/upande_agriculture/upande_agriculture/mobile/api.py:230` (`cleanup_bucket_reuse_anomalies`)
- Create: `upande_packhouse/server_scripts/clear_shelf_item.py` (the `clearShelfItem` Server Script moved into the repo, role Shelf Clearer, calls `shelf_ledger.clear_shelf_item`)
- Test: `apps/upande_quality/upande_quality/tests/test_shelf_operations.py` (extend)

**Interfaces:**
- Consumes: `frappe.get_attr("upande_packhouse.shelf_ledger.<fn>")` (the apps share one site; if the packhouse app is missing the call falls back to today's behavior and logs once).
- Produces: discard closes the lot `Discarded`; self-heal closes `Clean-up`; `transferBucket` opens the destination lot with reason `Arrived (shelf-to-shelf)` and closes the source lot as `Transferred (Shelf-to-Shelf)`, so the pair nets to zero; donor draws and bunch moves call `draw(..., reason="Stem replacement" | "Bunch moved")`; `createOfflineIssuingEntry` keeps reason `Offline Issuing` and stores the typed text in `detail` (added in Task 1); trolley and truck exits close with `Transferred (Trolley/Truck)` as today.

- [ ] **Step 1: Write failing tests** in the quality app's existing shelf-operations test file: `test_discard_closes_lot_as_discarded`, `test_transfer_bucket_pair_nets_to_zero`, `test_donor_draw_splits_lot`, `test_offline_removal_keeps_typed_reason_on_the_row`, plus an agriculture test `test_reuse_cleanup_closes_lot`.
- [ ] **Step 2: Run them**; expect FAIL.
- [ ] **Step 3: Implement the changes.**
- [ ] **Step 4: Run the quality and agriculture tests**; expect PASS.
- [ ] **Step 5: Commit in each repo** (`upande_quality`, `upande_agriculture`) separately; do not push.
- [ ] **Step 6: After deploy, disable the database `Clear Shelf Item (emergency, role-gated)` Server Script** (an operations step; list it in the PR text).

### Task 7: Control total and daily check

**Files:**
- Create: `upande_packhouse/upande_packhouse/doctype/shelf_control_total/` (fields `day`, `farm`, `open_lot_stems`, `live_stems`, `difference`, `top_buckets` Small Text), `api/v2/shelf_ledger.py: nightly_control()`
- Modify: `hooks.py` (add to `scheduler_events["daily"]`)
- Test: `tests/test_v2_shelf_ledger.py` (extend)

**Interfaces:**
- Produces: `nightly_control() -> None` writes one row per farm for the site-time day just ended; when `abs(difference)` exceeds `Production Settings.shelf_control_threshold` (default 500 stems) it creates a ToDo for the Stock Controller role listing the top buckets by difference.

- [ ] **Step 1: Write failing tests**: `test_nightly_control_writes_row_per_farm`, `test_difference_above_threshold_creates_todo`, `test_rerun_same_day_updates_not_duplicates`.
- [ ] **Step 2: Run them**; expect FAIL.
- [ ] **Step 3: Implement** and register the daily hook.
- [ ] **Step 4: Run the tests**; expect PASS.
- [ ] **Step 5: Commit** `feat: nightly shelf control total and alert`.

### Task 8: Stem Movement API wiring

**Files:**
- Modify: `api/v2/stem_movement.py` (`get_flow` returns `shelf_ledger` from `shelf_ledger.day_ledger / range_ledger`; remove `_shelf_ledger`, `_unlogged_now`, `_NOT_MOVE`)
- Test: `tests/test_v2_shelf_ledger.py` (extend)

**Interfaces:**
- Consumes: Task 3 `day_ledger`, `range_ledger`.
- Produces: `get_flow(...)` keeps its current keys for the pipeline stages and adds `shelf_ledger` with the Task 3 shape plus `invariants`.

- [ ] **Step 1: Write the failing test** `test_get_flow_returns_shelf_ledger_from_the_read_model_and_pipeline_stages_unchanged`.
- [ ] **Step 2: Run it**; expect FAIL.
- [ ] **Step 3: Replace the old ledger code** with calls to the read model; keep the pipeline stage code untouched.
- [ ] **Step 4: Run the test and the existing stem-movement checks**; expect PASS.
- [ ] **Step 5: Commit** `refactor: stem movement ledger reads the shelf ledger read model`.

### Task 9: The page redesign

**Files:**
- Modify: `www/stem-movement-v2.html`

**Interfaces:**
- Consumes: `get_flow`, `day_ledger`, `range_ledger`, `lot_rows`, `invariants`.
- Produces: sections in this order: filters and day picker (default today, site time); the day in one line (Opening, In, Out, Closing, Live now, Unexplained) with the red banner when unexplained is not zero or any invariant fails; Stems in by reason; Stems out by type (online and offline-recorded issues as separate lines, shelf-to-shelf as a pair); Offline lens (cleared, reshelved within 24 hours for the same bucket and variety, inferred consumption labelled inferred); Closing stock by state (same labels as Stock Visibility); by shelf and by farm tables; range matrix; pipeline funnel last. Every number is a button that opens the bucket list from `lot_rows`; a bucket opens the Bucket Journey.

- [ ] **Step 1: Write the page** reusing the Stock Visibility page's tokens, KPI cards and table styles; labels for states come from the `stock_truth` meta so wording matches.
- [ ] **Step 2: Syntax check** the page script with `node --check` after extracting it.
- [ ] **Step 3: Verify on `kaitet.local`** by loading the page for a busy day, a day with a repaired banner, and today; check the red banner appears with the unrepaired data and disappears after running the repair on a copy; check a 90-day range loads.
- [ ] **Step 4: Commit** `feat: stem movement page built around the shelf ledger`.

### Task 10: Acceptance and cross-checks

**Files:**
- Test: `tests/test_v2_shelf_ledger.py` (extend); notes appended to the spec's section 9.

- [ ] **Step 1: Cross-check script** (not committed) comparing, for today and each farm: ledger closing vs `stock_truth` total on-shelf vs Avails shelf total vs Stock Take expected; all must match or the difference must be listed bucket by bucket.
- [ ] **Step 2: Acceptance run** on three real days per farm after the repair: unexplained is zero or every unexplained bucket traces to a writer in the worklist.
- [ ] **Step 3: Record results** in the spec and commit `docs: shelf ledger acceptance results`.

---

## Self-review notes

- Spec coverage: lots and splits (Tasks 2, 5, 6), entry/exit reasons and `source` (Task 1), exact reason matching (Tasks 3, 8), read model and invariants (Task 3), repair (Task 4), control total and alert (Task 7), page sections (Task 9), writers worklist items 1-11 (Tasks 5, 6), `clearShelfItem` into the repo (Task 6), acceptance and cross-checks (Task 10).
- Task 3's entries by reason are Shelved, Reshelved (the offline difference), Returned from trolley/truck and Arrived (shelf-to-shelf); `Carried` is never counted as an entry.
- Spec section 6 lists the new reasons; Task 1 adds `Arrived (shelf-to-shelf)` and `detail` as well.
