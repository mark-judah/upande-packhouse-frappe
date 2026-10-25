# 06 Bucket Replacement and packing delay

Read-only research. Paths: PH = `apps/upande_packhouse/upande_packhouse`, Q = `apps/upande_quality/upande_quality`, APP = `/home/jk/Projects/upande-packhouse/src/tenants/karen`.
Local DB (kaitet.local) holds only JTEST test data for Bucket Replacement (2 rows). Volumes below are therefore code-derived plus the Pick List Item `not_found` data, which is real. Run the SQL in section 7 on the live site for real volumes.

## 0. Headline findings (what management should know)

1. **A Bucket Replacement (BR) record does not block packing.** The swap re-points the Pick List Item rows to the new bucket in the same transaction (PH/upande_packhouse/page/sales_allocation/sales_allocation.py `_replace_requested_bucket`:3251, row update 3449-3490). The BR is bookkeeping about the OLD, missing bucket ("is it still lost?"). Its `Open` age measures stock loss, not packing delay.
2. **What actually blocks packing is `Pick List Item.not_found = 1` (a bucket marked not found at a remote farm during transfer, with no replacement) and un-issued rows.** That path writes NO Bucket Replacement row (see 2.3). Locally: 58 rows / 51 buckets / 23 submitted OPLs / 4,480 stems, 22 of 23 OPLs older than 7 days (oldest 526 h), none completed short, none packed.
3. **Replacement delay = lag between the swap and the replacement being issued (scanned) at Issuing**, plus waiting for a truck when the replacement is remote. The BR carries `reported_at` but nothing records when the new bucket was issued; use `Pick List Item.issued_at` of the new bucket (SQL 7.6).
4. `Bucket Replacement` with reason "Quality issue" is created already `Discarded` (instant close), so Pareto and open-age views must exclude it or treat it separately.
5. 22 of 27 not-found buckets that are on a shelf today were shelved AFTER the not-found mark (many at Kapkolia on 2026-10-13, 3 to 8 days later), but their OPL rows still say `not_found = 1`. Nothing clears the flag when the bucket turns up (only a replacement does, sales_allocation.py:3472). Uncertain whether the stems are the same physical stems (bucket IDs are reused containers) but it is a strong "found later, order still blocked" indicator.

## 1. The doctype

Definition: PH/upande_packhouse/doctype/bucket_replacement/bucket_replacement.json. Controller is empty (`bucket_replacement.py`, `class BucketReplacement(Document): pass`). Naming `format:BRPL-{YYYY}-{#####}` (the series counter locally is at 03072 with only 2 rows left, so rows were deleted/cleaned; do not use the counter as a volume). Not submittable (docstatus 0).
Permissions: System Manager (full), Stock Manager (read/write/create, no delete), Stock User (read only). Writers in code use `ignore_permissions=True`.
Hook: `after_insert` -> `api.v2.rt_watchlist.on_replacement_insert` (PH/hooks.py:267).

| Field | Type / options | Meaning |
|---|---|---|
| old_bucket (reqd) | Data | The bucket that was replaced (missing / bad / mislabelled). |
| new_bucket | Data | The bucket swapped in. Empty only for a quality reject with no donor. |
| status | Select Open / Found / Discarded / Written Off, default Open | See 1.2. |
| reason | Select Missing / Damaged / Wrong variety / Issued offline / Quality issue / Other, default Missing | See 1.3. |
| notes | Small Text | Free text from operator. |
| defect, defect_category (Wrong stem length/Disease/Pest/Other), replace_scope (Whole bucket/Some stems), replacement_source (Sales farm/Remote farm), transfer_priority (ASAP), reject_entry (Link Stock Entry) | quality section | Only set by packing_quality (`_record`, PH/api/packing_quality.py:679). `replacement_source` is always "Sales farm" now (remote = False, line ~370). |
| order_pick_list (Link OPL), order_name, sales_order | order section | From OPL (bucket_replacement.py:33,51-53). |
| farm (Link Farm), item_code, stem_length, stems (Float) | | Farm = farm the replacement came from (or the issuing station). Stems = stems moved (needed). |
| expected_shelf / new_shelf | Data | Shelf the old bucket should have been on (anchor.shelf) / shelf the new one came off. |
| trip (Link Bucket Request Trip), vehicle (Link Vehicle) | | Most recent trip with status Draft/Requested/Scheduled/Dispatched that lists this OPL+farm (bucket_replacement.py:34-42). Blank when no trip is planned; Local: both rows blank. |
| reported_by (Link User), reported_at (Datetime) | | Set by `record()` to session user / now (62-63). |
| stock_entries | Small Text (read-only) | Comma list of stock entries from the swap. |
| resolved_by, resolved_at, resolution | read-only | Set on closing (1.2). |

### 1.2 Lifecycle and what "resolved" means
- **Created**: `bucket_replacement.record()` (PH/api/bucket_replacement.py:17-69), status `Open`, `**extra` may override status (quality path sets Discarded).
- **Open -> Found**: `mark_found(bucket_id, shelf, farm)` (72-89). Called at the end of the shelving endpoint in PH/mobile/api.py:5849-5851 for the bucket being shelved. It closes EVERY Open record whose `old_bucket` equals that bucket id, regardless of OPL, reason or date. Sets resolved_by/at and resolution "Shelved on <shelf> (<farm>)". Failures are swallowed (try/except, logged).
- **Open -> Found / Discarded / Written Off (manual)**: `resolveBucketReplacement(name, status, resolution)` (92-112), POST whitelisted, **no role check** (any logged-in user), used by the Remote Transfers v2 journey page button (PH/public/js/rt-journey-v2.js:510). Can also re-close an already closed record (no state check).
- **Discarded (automatic)**: quality-issue records are inserted Discarded with `resolution "Rejected during packing: <defect>"`, resolved_at = now (packing_quality._record, 679-715). There is no approval step in this path.
- No Cancelled/Resolved state. No timeout or escalation. "Resolved" therefore means one of: old bucket reshelved (Found), declared gone (Discarded / Written Off), or auto-discarded reject. It never means "order is complete".
- Desk list: Open ones are surfaced by `open_replacements()` (146-169, limit 500) to Bucket Journey, and counted as "missing" in PH/api/remote_transfer/bucket_journey.py:258 and PH/api/v2/remote_transfer.py:552.

### 1.3 Reason values and who sets them
| reason | Set by |
|---|---|
| Missing | default; `offline_issue.REASONS["not_found"]` (offline_issue.py:30); app Replace chip "Missing" (APP/state/karen-issuing-store.ts:43); Bucket Requests app replaceRequestedBucket |
| Damaged, Other | Bucket Requests app (Q/mobile/api.py:14832 `replaceRequestedBucket` passes `reason` through); `Damaged` is also an app Issuing chip |
| Wrong variety | app Issuing chip; Issue Offline "wrong_variety"; keeps old bucket on shelf (keep_old_on_shelf) and corrects its variety/length (`_correct`, offline_issue.py:378) |
| Issued offline | app Issuing chip. If the bucket was issued to this OPL's own line, the app calls `mark_issued_offline` instead and NO BR is made (offline_issue.py:741-776); a BR "Issued offline" arises only for a swap when it went to another line |
| Quality issue | packing_quality only |
`record()` coerces any unknown reason to "Missing" (49). The Watchlist treats only `Missing` as "not found" (rt_watchlist.py:133-140).

## 2. Every way a replacement or "not found" happens

### 2.1 During issuing: bucket not on the shelf (packhouse app "Replace")
- Screen: APP/features/issuing/IssuingScreen.tsx (row action "Replace", line ~561) with ReplacePicker.tsx (reason chips, candidate list, confirm dialog).
- Backend: `offline_issue.replacement_options` (628) then `replace_for_issuing` (671-704) -> `_swap` -> `sales_allocation._replace_requested_bucket` with `allow_shelved=True`, `allow_left=True` when a station farm is sent. Candidates only from the issuing station's farm (703).
- Effect on rows: the old bucket's Bucket Allocation Status rows are cancelled, new bucket gets the allocation, every Pick List Item row of the old bucket is set to `bucket=new`, `shelf=new.shelf`, source/origin warehouse, stem_length, `not_found=0/not_found_at/by=null`, downgrade_reason; with `allow_left` it also clears `loaded_in_trolley/in_transit/awaiting_transfer/transit_truck` (sales_allocation.py:3449-3495). New bucket's shelf row is cleared ("Replaced"); old bucket's shelf row is cleared too unless Wrong variety. One `Move To Graded Sold` Stock Entry per SO item trades the two buckets (`_post_bucket_swap`, 3027+). OPL gets an Info comment. BR inserted `Open`, reason as chosen.
- The replacement is NOT auto-issued: the issuer must scan it afterwards (offline_issue.py:681). `replace_for_issuing` does not call `_off_trips`, so the old bucket may stay on a transfer trip list (uncertain: only `issue_offline` calls it, 850).
- Alternate: Quality app Shelf Operations "Issue Offline" = `issue_offline` (780-900): swap + immediate issue (`issueBucketToSaleOrderItem`) + marks `issued_offline=1`; reason found / not_found / wrong_variety. "found" makes no BR.
- Remote-aware: if the scanned bucket is still at its remote farm for this OPL it is only put on the truck and in transit (`_send_in_transit`, 321-347); it is issued after shelving at the hub.

### 2.2 During packing: quality issue / reject
Two separate flows exist. Only (a) is used by the current mobile app.
(a) **Packing screen "Quality issue"** (APP/features/packing/QualityIssueSheet.tsx, APP/api/karen-packing-quality-api.ts) -> `packing_quality.report_packing_quality_issue` (PH/api/packing_quality.py:319). One transaction: `_post_rejects` (Packhouse Rejects stock entry to Rejects), `_shrink_old_allocation` (old allocation and pick rows shrink by the bad stems), `_allocate_replacement` (new rows on same OPL via `_allocate_stock_with_buckets_impl`, put in the same box id, `custom_ready_for_packing=1`), `_record` (BR "Quality issue", Discarded). Sales-farm donor only (remote branch hard-disabled: `remote = False`). If a donor was chosen the new bucket is issued immediately through `offline_issue._issue` (so "pack it now"); if issuing fails the packer must scan it at Issuing. With no donor chosen the stems are only rejected: the allocation is short and the BR shows `new_bucket` blank (a gap that is not an open item).
(b) **Packing Reject Log + approve_replacement** (PH/api/packing_reject.py: `record_reject`:261, `approve_replacement`:395, `close_without_replacement`:480). Separate doctype Packing Reject Log (status Pending Approval -> Replaced / No Replacement). Approver roles: Packing Supervisor, Assistant Packhouse Manager, System Manager (`APPROVER_ROLES`, 31-35). Writes NO Bucket Replacement row. No mobile app caller found in APP (grep found none); local table is empty. See 02_quality_rejects.md.
Typical wait (a): none recorded; (b): from Packing Reject Log creation to `approved_at`, a supervisor must act while the packer holds the box open.

### 2.3 During transfer: bucket missing from the remote farm, trolley or truck
- **Mark not found** (farm Bucket Requests app): `markRequestedBucketNotFound` -> `transfer_scheduling.mark_bucket_not_found` (PH/api/remote_transfer/transfer_scheduling.py:5820-5911). Sets on every waiting row of that bucket: `awaiting_transfer=0, loaded_in_trolley=0, trolley_id='', not_found=1, not_found_at=now, not_found_by=user`; adds an OPL comment; trims the planned trip by one bucket (`_plan_one_fewer`, 5914); adds a Bucket Watch ("Not found during transfer"). **No Bucket Replacement is created.** Refused if the row is already in_transit or shelved.
- **Replace** (farm app, bucket exists at the same farm): `Q/mobile/api.py:14817 findRequestedBucketReplacement` and `:14832 replaceRequestedBucket` -> `sales_allocation.replace_requested_bucket(pick_list_item, new_bucket_id, reason, notes, keep_old_on_shelf)` with no station and `allow_left=False`: replacement comes from the SAME remote farm and stays `awaiting_transfer` for the trip; BR farm = that remote farm and `trip`/`vehicle` are filled when a trip lists the OPL+farm. Watchlist classifies it as "during transfer" when trip/vehicle set or farm is not the hub (rt_watchlist.py:127).
- `to_remote=True` in `_replace_requested_bucket` (rows go back to waiting for a truck) has no caller anywhere in apps (grep): dead path. Uncertain, confirm with live traffic.
- Truck left short: `closeTripStop` leaves planned buckets "left behind" and moves them to the truck's next run (transfer_scheduling.py ~5930). Those rows stay `awaiting_transfer`, not `not_found`.
- If the transfer later brings the bucket anyway: `mark_found` only closes a BR; the `not_found=1` flag on rows is not cleared (see 0.5).

### 2.4 Offline issue 'not_found' (Quality app Shelf Operations -> Issue Offline)
`issue_offline(reason="not_found")` = swap (BR reason Missing) + issue of the scanned bucket + `_off_trips` (drops the missing bucket from trips not yet received) + `issued_offline=1`. The old bucket "leaves its shelf and its Bucket Replacement stays open until it is shelved again" (offline_issue.py:798-800). The OPL's issued % moves immediately, so this path has the smallest packing delay.

### 2.5 pack_while_waiting_replacement (Server Script) and the Production Settings %
- Source copy: PH/server_scripts/pack_while_waiting_replacement.py (live Server Script "Pack While Waiting Replacement", API POST `pack_while_waiting_replacement`; not present in local DB).
- Check (23-57): not cancelled, not `short_accepted`, not already `pack_issued_now`, no unissued row still moving (awaiting_transfer / loaded_in_trolley / in_transit), at least one open row, and issued stems % >= `Production Settings.pack_waiting_replacement_pct` (default 50 when blank). It submits a draft OPL (ignore_links) and sets `pack_issued_now=1` (82). It does NOT mark anything not_found and does NOT create a BR.
- The setting `pack_waiting_replacement_pct` is read in PH/mobile/api.py `_pack_waiting_min_pct` (3427-3430, `or 50.0`) but is not defined in any doctype JSON in the repo or in the local DB (uncertain: live-only custom field).
- The packing list no longer needs the button: `getReadySaleOrderItems`-type listing for packing adds every OPL with issued stems > 0 and pct >= min_pct (`pack_now`, mobile/api.py:2953-2967) as well as `pack_issued_now=1` ones. Issuing also auto-submits drafts at that threshold (`_submit_for_packing`, 2105-2137).
- How packing proceeds with partial stems: `_issued_cap_error` (3433-3440) refuses packing more stems than issued ("Only N% is issued - pack the rest once more stock is issued"). Sequence logic `PACK_DONE_SQL` (3262+) treats an order as done-for-now once every issued stem is packed, so the team moves to the next order. The box completes when the missing/late buckets are issued (by replacement swap + scan, or arrival) and the rest is packed into the same OPL's boxes. The order is not recorded short.
- `completeOplShort` (mobile/api.py:2140-2220): see 2.6.
- App: IssuingScreen.tsx:196-215, 324-333 (button "Pack what's issued (N%)"), `canPackWaiting` (APP/state/karen-issuing-store.ts:228). Dialog text claims "buckets not issued are marked not found" but the Server Script does not set any flag (UI text vs backend mismatch).

### 2.6 completeOplShort (short accepted)
Requires at least one `not_found` row, none still moving, and every other row issued (2160-2182). Sets `short_accepted=1`, `short_stems`, submits the OPL if draft, adds the comment "Completed short by N stems (P%): <buckets>". No role check (`@frappe.whitelist`, POST). It never creates or closes a BR. It cannot be used while any non-not-found bucket is unissued. This is the only way a transfer-not-found order becomes packable without a replacement (besides the % rule).

### 2.7 Manual
Desk: Stock Manager / System Manager can create/edit a Bucket Replacement by hand; the Bucket Journey page can close Open ones via `resolveBucketReplacement`. Manual Bucket Watch ("Manual" source, 1 row locally) is separate.

## 3. Exact packing effect per path
| Path | What blocks | What can proceed | Who must act (screen/role) | Typical wait (evidence) | Why order stays incomplete |
|---|---|---|---|---|---|
| Issuing replace (2.1) | The new bucket until scanned; if the replacement is remote it waits for a truck. Order is not listed for packing until fully issued or >= pct issued | Other buckets issue and pack (>= pct) | Issuer: Issuing screen (app) | No data; lag = `issued_at(new) - reported_at` (7.6) | Replacement never scanned, or source bucket missing again (chain) |
| Quality issue (2.2a) | Nothing if donor issued OK; box waits for stems if issuing failed | Other boxes | Packer on Packing screen; issuer if the scan failed | Seconds in the normal case | No donor chosen: line short, no open item |
| Packing Reject Log (2.2b) | The box waits for supervisor approval | Other boxes | Packing Supervisor / Assistant Packhouse Manager (approve) | None locally | Pending Approval or No Replacement (close box short with under-pack reason) |
| Transfer not-found, no replacement (2.3) | The row is `not_found` and never issued: order never reaches 100%, `opl_submit_blockers` for drafts, `completeOplShort` blocked while others unissued/moving | Order packs once issued stems >= pct (50% default), only the issued stems | Farm app operator (replace); Packing Supervisor / Assistant Packhouse Manager (complete short); Stock Manager (Bucket Requests) | Local: 22 of 23 OPLs open 7+ days, up to 22 days | No one replaces or accepts short; flag not cleared when bucket reappears |
| Transfer replace (2.3) | Waits for trip departure, arrival, shelving at hub, then issue scan | Other buckets | Farm loader, truck, hub shelver, issuer | Depends on trips; use trip `trip_date` and `shelved_at`/`issued_at` | Truck left short / replaced bucket not reloaded |
| Offline issue not_found (2.4) | Nothing after the swap (issued immediately) | Everything | Shelf operator (Quality app) | Minutes | Failed issue (`success=false`) leaves the swap done and unissued |
| Pack while waiting (2.5) | Packing stops at issued stems | Order packs partial; next order starts | Issuer sends / system auto-lists; later issuer issues remainder | Equals time until the last bucket is issued | Remaining buckets stay not issued or `not_found` |
| Short accepted (2.6) | Nothing | Packs what was issued | Supervisor | n/a | Closed as short on purpose (`short_accepted=1`) |

## 4. Local DB volume (read-only SELECTs, 2026-10-25)
- `tabBucket Replacement`: 2 rows, both `Missing` / `Open`, reported 2026-10-23 00:52 and 00:56, user Administrator, farms Kapkolia and Simotwo (JTEST-001/002/003 test buckets), age 57 h, no trip/vehicle. They form one chain JTEST-001 -> JTEST-002 -> JTEST-003. No loops, no duplicates, no resolved rows, so no resolution-time statistics exist locally.
- Pick List Item (not-found, real data, 2026-10-03 to 2026-10-23): 58 rows, 51 buckets, 23 submitted OPLs, 4,480 stems; none issued, none short-accepted, none with a Farm Pack List. By source farm: Torongo 37 buckets / 3,490 stems, Simotwo 13 / 890, Kaptumbo 1 / 100. Marked by 2 farm users (teresia.masiro, fredrick.kemboi). Repeats: buckets 30d8ca, cbd675, N5AC70 each not-found on 2 OPLs. Worst OPLs by short %: OPL-2026-00687 100%, 00843 100%, 00842 80%, 00665 67%.
- Bucket Watch: 53 rows all "Watching" (51 transfer, 1 issuing, 1 manual); none "Reappeared" although 22 of the not-found buckets were shelved later (hourly sync/run likely not executed locally; uncertain).
- `Packing Reject Log` 0 rows; `Packhouse Rejects` stock entries 0; `short_accepted` OPLs 0; `pack_issued_now` OPLs 0.
- Partial-issue OPLs that qualify for pack-while-waiting (issued 50% or more, not all, nothing moving, not short): 190 of 1,110 submitted OPLs locally (older dev data, `issued_at` blank on many).
- Replacement rate per 100 stems issued Oct 2026: Simotwo 0.10, Kapkolia ~0.00 (test rows only), others 0.

## 5. Leading indicators and who to call
| Indicator | Source | Call |
|---|---|---|
| Not-found rate by farm / shelf / marker user | `Pick List Item.not_found_at/by/farm` (7.2, 7.5) | The farm's Bucket Requests lead (Torongo, Simotwo) and Stock Manager |
| Not-found OPLs older than 4 h / 24 h without replacement or short | 7.2 | Packing Supervisor / Assistant Packhouse Manager (replace or complete short) |
| Buckets re-shelved after not-found while OPL row still `not_found` | 7.8 | Hub shelver and Stock Manager (re-point or replace), then order owner |
| Bucket Watch "Watching" aging, "Reappeared" status | `Bucket Watch` (rt_watchlist.py `run_watchlist`:410) | Stock Manager / Bucket Journey owner |
| Open BR age (old bucket unaccounted) | 7.1 | Stock Manager (Found / Discarded / Written Off) |
| Replacement lag (reported -> new bucket issued) | 7.6 | Issuer at the packhouse |
| Replacement chains (new bucket replaced again) | 7.9 | Stock Manager (shelf accuracy at that farm) |
| Partial-issue OPLs packing stalled | 7.3 | Issuer lead; Packing Supervisor |
| Quality-issue replacements per 100 stems packed by defect/farm | BR reason Quality issue | QC / farm grower for defect Pareto |

## 6. Data quality problems
1. Quality-issue BR rows are born `Discarded` with `resolved_at = reported_at` (zero wait), and no row is made when no donor is chosen. Exclude `reason='Quality issue'` from wait/open metrics.
2. Transfer not-found (the largest delay source) has no BR row. Count it from Pick List Item.
3. `mark_found` closes ALL Open BRs for an old bucket id when that id is shelved, even for a later harvest cycle (IDs are reused). A bucket re-shelved for a new reason can falsely close an old record as Found. Uncertain how often.
4. Wrong variety / Damaged BRs leave the old bucket on its shelf, so they only close if it is reshelved: they stay Open indefinitely (inference from `keep_old_on_shelf` and `mark_found` rules).
5. `resolveBucketReplacement` has no role or state check and can reopen/overwrite resolved_by/at of closed rows.
6. `trip`/`vehicle` are filled only when an active trip lists the OPL+farm at insert time; blank for issuing-station replacements, so the "during transfer" split relies on the farm vs hub test.
7. No `issued_at` link from the BR to the new bucket's issue; join through Pick List Item. `issued_at` is NULL on 423 of 3,154 issued local rows (older rows), so lag calculations need `issued_at IS NOT NULL`.
8. Chains: JTEST-001 -> 002 -> 003 locally. A dashboard must follow `new_bucket = old_bucket` to see the final bucket and to avoid counting a chain as several independent shortages.
9. Naming series: BRPL-2026-03072 with 2 rows locally means records were deleted; do not use name numbers as counts.
10. `replace_for_issuing` does not drop the old bucket from trips (only `issue_offline` does) so trip counts may stay inflated (uncertain).
11. App dialog "marked not found" for Pack While Waiting is not backed by a flag (UI text mismatch).

## 7. SQL for the dashboard (tested on kaitet.local; MariaDB)
Replacement-caused delay per OPL = (a) oldest unresolved not_found age, (b) replacement lag, (c) pending partial-issue stems. Parameters in brackets.

7.1 Open replacements and age
```sql
SELECT br.name, br.reason, br.order_pick_list, br.farm, br.expected_shelf, br.reported_by, br.reported_at,
       ROUND(TIMESTAMPDIFF(MINUTE, br.reported_at, NOW())/60,1) AS age_h
FROM `tabBucket Replacement` br WHERE br.status='Open' ORDER BY br.reported_at;
```
7.2 OPLs blocked by not-found buckets (primary delay metric)
```sql
SELECT o.name opl, o.order_name, o.team, o.short_accepted,
  COUNT(DISTINCT CASE WHEN p.not_found=1 THEN p.bucket END) nf_buckets,
  SUM(CASE WHEN p.not_found=1 THEN p.stock_qty ELSE 0 END) nf_stems, SUM(p.stock_qty) total_stems,
  ROUND(100*SUM(CASE WHEN p.not_found=1 THEN p.stock_qty ELSE 0 END)/SUM(p.stock_qty)) short_pct,
  ROUND(100*SUM(CASE WHEN p.issued=1 THEN p.stock_qty ELSE 0 END)/SUM(p.stock_qty)) issued_pct,
  MIN(p.not_found_at) first_nf_at, ROUND(TIMESTAMPDIFF(MINUTE, MIN(p.not_found_at), NOW())/60,1) age_h
FROM `tabOrder Pick List` o
JOIN `tabPick List Item` p ON p.parent=o.name AND p.parenttype='Order Pick List' AND IFNULL(p.bucket,'')<>''
WHERE o.docstatus<2 GROUP BY o.name HAVING nf_buckets>0 ORDER BY first_nf_at;
```
7.3 Pack-while-waiting boxes pending (issued >= threshold, rest not issued, nothing moving, not short)
```sql
SELECT o.name, o.order_name, SUM(p.stock_qty) total, SUM(IF(p.issued=1,p.stock_qty,0)) issued,
  ROUND(100*SUM(IF(p.issued=1,p.stock_qty,0))/SUM(p.stock_qty)) pct,
  SUM(p.issued=0 AND p.not_found=0 AND (p.awaiting_transfer=1 OR p.loaded_in_trolley=1 OR p.in_transit=1)) moving,
  MAX(p.issued_at) last_issue_at
FROM `tabOrder Pick List` o JOIN `tabPick List Item` p ON p.parent=o.name AND p.parenttype='Order Pick List' AND IFNULL(p.bucket,'')<>''
WHERE o.docstatus=1 AND IFNULL(o.short_accepted,0)=0 GROUP BY o.name
HAVING issued>0 AND issued<total AND pct>=50 AND moving=0;   -- 50 = Production Settings pack_waiting_replacement_pct
```
7.4 Replacements per 100 stems issued, by farm (swap `p.farm` for shelf, `opl.team` or `br.reported_by` for other cuts)
```sql
SELECT i.farm, i.issued_stems, IFNULL(r.br_stems,0) br_stems, ROUND(100*IFNULL(r.br_stems,0)/i.issued_stems,2) per100
FROM (SELECT farm, SUM(stock_qty) issued_stems FROM `tabPick List Item`
      WHERE parenttype='Order Pick List' AND issued=1 AND issued_at>=%(from)s GROUP BY farm) i
LEFT JOIN (SELECT farm, SUM(stems) br_stems FROM `tabBucket Replacement`
           WHERE reported_at>=%(from)s AND reason<>'Quality issue' GROUP BY farm) r ON r.farm=i.farm
ORDER BY per100 DESC;
```
7.5 Repeated not-found buckets
```sql
SELECT bucket, COUNT(*) rows_, COUNT(DISTINCT parent) opls, MIN(not_found_at) first_at, MAX(not_found_at) last_at
FROM `tabPick List Item` WHERE parenttype='Order Pick List' AND not_found=1 GROUP BY bucket HAVING opls>1;
-- plus BR repeats: SELECT old_bucket, COUNT(*) FROM `tabBucket Replacement` GROUP BY old_bucket HAVING COUNT(*)>1;
```
7.6 Replacement lag (replaced -> new bucket issued)
```sql
SELECT br.name, br.reason, br.order_pick_list,
       TIMESTAMPDIFF(MINUTE, br.reported_at, MIN(p.issued_at)) lag_min
FROM `tabBucket Replacement` br
LEFT JOIN `tabPick List Item` p ON p.parent=br.order_pick_list AND p.parenttype='Order Pick List'
     AND p.bucket=br.new_bucket AND p.issued=1
WHERE br.new_bucket<>'' AND br.reason<>'Quality issue' GROUP BY br.name;   -- NULL lag = replacement never issued
```
7.7 Reasons Pareto and resolution time
```sql
SELECT reason, status, COUNT(*) n, SUM(stems) stems,
       ROUND(AVG(TIMESTAMPDIFF(MINUTE, reported_at, resolved_at))/60,1) avg_h_to_resolve
FROM `tabBucket Replacement` WHERE reported_at>=%(from)s GROUP BY reason, status ORDER BY n DESC;
```
7.8 Not-found buckets that were shelved later (found but order still blocked)
```sql
SELECT p.bucket, p.parent opl, p.farm nf_farm, p.not_found_at, s.farm shelf_farm, si.parent shelf, si.date_added
FROM `tabPick List Item` p
JOIN `tabShelf Item` si ON si.bucket_id=p.bucket JOIN `tabShelf` s ON s.name=si.parent
WHERE p.not_found=1 AND p.parenttype='Order Pick List' AND si.date_added>p.not_found_at;
```
7.9 Chains
```sql
SELECT a.name a, a.old_bucket, a.new_bucket, b.name b, b.new_bucket b_new
FROM `tabBucket Replacement` a JOIN `tabBucket Replacement` b ON b.old_bucket=a.new_bucket AND b.reported_at>=a.reported_at;
```
Per-OPL combined delay for the dashboard: LEFT JOIN 7.2 (age_h) with 7.3 (pending stems), 7.6 (max lag per OPL) and 7.1 (open BR count per `order_pick_list`) on `opl`.
