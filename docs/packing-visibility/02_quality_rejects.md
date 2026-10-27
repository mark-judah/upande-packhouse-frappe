# 02 Quality rejects and holds: what blocks 100% packing

Scope: read-only research. Local site `kaitet.local`, queried 2026-10-24/25. Paths are relative to `apps/`.
PH = `upande_packhouse/upande_packhouse`, Q = `upande_quality/upande_quality`.
Line numbers are as of this checkout. (UNCERTAIN) marks anything not verified.

Local data is dev/test data. Real packing-reject flows are almost unused, so counts show query feasibility, not business truth.

## 1. Kinds of reject/hold that can touch an OPL's stems or boxes

| # | Kind | Written by (file:function:line) | Doctype / ledger | Key fields | Join to OPL |
|---|------|------|------|------|------|
| A | Packing Reject (packer finds bad stems while packing a box) | PH/api/packing_reject.py:record_reject:261 (log insert 304-324, stock hop 327-343, QR 350) | `Packing Reject Log` (PRL-#####) plus `Packhouse Rejects` Stock Entry (Packhouse store to `Rejects - KR`, line 27/330) plus `Quality Reporting` (mode "Packing Reject", `_quality_reporting`:107) | order_pick_list, box_id, variety, stem_length, stems_rejected, bunches, bunch_id, bucket, sale_order_item, farm, recorded_by, reason (Data, free text), issues[] (parameter,count), status, donor_bucket, stems_replaced, approved_by, approved_at, reject_stock_entry, quality_reporting | `order_pick_list` direct; bucket via bunch_id, using the Grading Stock Entry's `custom_bucket_id` (`_bunch_bucket`:78) |
| B | Packing quality issue (bucket/stems replaced from Packing screen) | PH/api/packing_quality.py:report_packing_quality_issue:319 (`_post_rejects`:486, `_shrink_old_allocation`:540, `_allocate_replacement`:600, `_record`:679) | `Bucket Replacement` (reason "Quality issue", status set straight to `Discarded`) plus `Packhouse Rejects` Stock Entry | order_pick_list, old_bucket, new_bucket, defect, defect_category (Wrong stem length/Disease/Pest/Other), replace_scope, replacement_source, stems, farm, item_code, stem_length, reported_by/at, reject_entry, trip | `order_pick_list`, `old_bucket` = `Pick List Item.bucket` |
| C | Stem replacement (donor stems into an OPL row, from the quality app's Grading QC) | Q/mobile/api.py:replaceStems:8342 (log 8452-8470 area) | `Stem Replacement Log` (SRL), no status | opl, pick_list_item, sale_order_item, destination_bucket, donor_bucket, donor_shelf, variety, stem_length, stems, replaced_at, reason | `opl`, `pick_list_item` |
| D | Packing bypass (bunch cannot be scanned: damaged/missing QR, ungraded) | PH/mobile/api.py:createPackingBypass:~1620-1680 | `Packing Bypass Log` (PBL) | order_pick_list, box_id, reason (Packing Bypass Reason), bunches, farm, packed_by, greenhouses/harvesters/supervisors (traceability from the OPL's buckets) | `order_pick_list`; no status, informational only |
| E | Under-pack reason (box closed short on purpose) | PH/mobile/api.py:setPackListBoxUnderPackReason:~1506; used by farm_pack_list.py:92-109 | `Farm Packlist Item.under_pack_reason` (child of `Farm Pack List`) | box_id, reason (Under Pack Reason master) | `Farm Pack List.order_pick_list`, box_id |
| F | Not found / missing bucket on the OPL | writes `Pick List Item.not_found/not_found_at/not_found_by` via `db.set_value` (see PH/api/v2/rt_watchlist.py:14, 170); cleared at sales_allocation.py:3472 | `Pick List Item` | not_found, not_found_at, not_found_by, bucket, stock_qty, farm, shelf | `parent` = OPL |
| G | Completed short (accept an OPL with missing buckets) | PH/mobile/api.py:completeOplShort:2121 | `Order Pick List.short_accepted` (Check) and `short_stems` | short_accepted, short_stems | OPL itself. Comment "Completed short by N stems (P%)" |
| H | Bucket Replacement (missing/damaged/wrong variety/issued offline) | PH/api/bucket_replacement.py:record:17, mark_found:72, resolveBucketReplacement:93, open_replacements:146 | `Bucket Replacement` | status Open/Found/Discarded/Written Off, reason, reported_at, resolved_at | order_pick_list, old_bucket, farm, trip |
| I | Quarantine at receiving (intake QC) | Q/mobile/api.py:submitBatchQuality:9587 and `_submit_batch_quality_impl`:10211. Release: releaseFromQuarantine:7333, releaseBatchFromQuarantine:7714, `_resolve_bucket_quarantine`:7545 | Stock Entries: `Receiving Quarantined` (into `* Quarantine*` or `* Receiving Quarantined *` warehouse), `Quarantine Accept` (back to storage), `Quarantine Rejects` (to `Rejects - KR`, 7486-7510); `Quality Reporting` control_action Quarantined/Rejected | custom_bucket_id, custom_receiving_batch_id, posting_date | bucket id = `Pick List Item.bucket` and `Shelf Item.bucket_id`. Not by OPL |
| J | Coldroom reject (bucket QC on the shelf/coldroom) | Q/mobile/api.py:saveColdroomReject:13998 (Quality Reporting 14060-14088, `Coldroom Rejects` SE 14090-14115, shelf and Bucket Allocation Status reduced 14117-14143) | `Quality Reporting` ("Coldroom-<farm>-####") plus `Coldroom Rejects` SE | stems_rejected, parameters. No OPL field | bucket only. Reduces `Shelf Item.stem_qty` and `Bucket Allocation Status.total/available_quantity` and does NOT touch allocated_quantity (UNCERTAIN: check whether allocated > total can result) |
| K | Packhouse QC: Grading QC / Reject Recorder / Final QC / Airport Returns | Q/mobile/api.py:savePackhouseQC:8584 (reject SE posted ~9104-9160: type "Packhouse Rejects", or "Airport Rejects" for airport returns; Grading QC posts NO reject SE, uses the replacement flow instead, comment at ~8610 and ~9099) | `Quality Reporting` (control_point Packhouse, inspection_mode, custom_order_pick_list, custom_team, custom_length, custom_stems_rejected, custom_bunches_affected, custom_boxes_quarantined/staged/checked) plus CAR (`Corrective Action Report`) when thresholds are exceeded (CAR_OTHER_THRESHOLD_PCT 6.0, auto-assigned to a fixed user) | child `Quality Parameter` (parameter_name, count, action Accepted/Quarantined/Reject) | `custom_order_pick_list` (Link). The standard `order_pick_list` field is NOT set by savePackhouseQC in practice (0/360 locally) |
| L | Discard Request (aged buckets) | Q/mobile/api.py:createDiscardEntry:449, getDiscardRequestBuckets:3776; PH/api/discards.py | `Discard Request` (workflow Draft/Pending Approval/Approved/Rejected) plus `Discard Request Bucket.discarded` | bucket_id, discarded | bucket. The donor logic blocks discard-listed buckets: Q/mobile/api.py:replaceStems ~8420-8470 `on_discard_list` |
| M | Packhouse inspection / hygiene | Q/mobile/api.py:submitPackhouseInspection:11108 | `Packhouse Inspection Log` (+ `Packhouse Inspection Component`, status Okay/Faulty, priority) | date, area, components[] | none. Not linked to any OPL. Not a packing blocker, only context |

Notes
- There is no `Packhouse QC` or `QC Issue` doctype on this site. The code says "Packhouse QC doctype removed on this site" (Q/mobile/api.py:3224). `Quality Reporting` replaces it. `Packing Reject Issue` is the child table of PRL (parameter,count). `Packhouse Rejection Reason` is a simple master (`reason`).
- There is no `Packing Qty Confirmation` or `Packhouse Abnormalities Log` doctype in either app (UNCERTAIN: not found by doctype directory listing). `Confirmed Stems` exists (`confirmed_stems`), but that is allocation confirmation, not QC.
- PRL `reason` is a free Data field but `record_reject` validates it against `Packhouse Rejection Reason` when that master exists (lines 291-296). Local master is empty.

## 2. Effect on packing

| Kind | Shortens OPL? | Replacement | Blocks box | Approval needed | Open states and how long they can sit |
|------|------|------|------|------|------|
| A Packing Reject | No. The OPL `short_accepted` is not set. The stems leave the packhouse stock, so the box is short until replaced | Supervisor picks a donor bucket (same variety/length, unallocated, oldest first: `replacement_options`:367 via `sa._replacement_candidates`:2921). `approve_replacement`:395 posts shelf to Graded Sold to Packhouse (`post_donor_stock`:164) and sets status Replaced | Yes, implicitly: the box cannot be filled until Replaced, or closed short with an Under Pack Reason | Yes. Roles `APPROVER_ROLES` = Packing Supervisor, Assistant Packhouse Manager, System Manager (line 31). `close_without_replacement`:480 has NO role check (uncertain whether intended) | `Pending Approval` is the only open state. No timeout or SLA in code. Unbounded dwell. Final: Replaced or No Replacement |
| B Packing quality issue | No (stems shrink via `_shrink_old_allocation`) | Immediate: from sales farm and issued at once via offline_issue._issue; if none chosen, only rejected and a gap remains | Allocation shrinks. A gap is not recorded as an open item | No | Record auto-closes as `Discarded`, so an unreplaced gap is invisible in Bucket Replacement. Detect via short allocation (OPL allocated < required) |
| C Stem replacement | No | Is the replacement | no | no | none |
| D Bypass | No | n/a | No | no | none. Count only |
| E Under-pack | Effectively ships short: box counted done. `sync_and_maybe_submit_fpl` submits the FPL once all boxes are done or under-packed (mobile/api.py:~1545-1560, farm_pack_list.py:92-109) | none | Unblocks the box | no | none |
| F not_found | Row excluded from "done" math (`NOT not_found`), OPL cannot reach 100% issued | Replacement via requested-bucket flow (`replaceBucket`:7795, `replaceRequestedBucket`:14832, `_replace_requested_bucket` sales_allocation.py:3251) which clears the flag (3472), or complete short | Blocks issuing and packing of that OPL until resolved | none | Open as long as `not_found=1` on a submitted OPL with `short_accepted=0`. Age = now - `not_found_at`. Local max age about 22 days |
| G Complete short | Yes: `short_accepted=1`, `short_stems`=missing stems. `opl_submit_blockers` skips the allocation-completeness check (sales_allocation.py:2296) | n/a | Unblocks | `completeOplShort` has no role check in code (uncertain, whitelisted for any logged-in user) | Not open once set. Requires nothing moving by truck and everything else issued (2140-2160) |
| H Bucket Replacement | No | The record of it | If Open, replaced bucket is "missing" | `resolveBucketReplacement`:93 has no role check | `Open` until Found (auto when reshelved, `mark_found`:72), or manually Discarded / Written Off. No timeout |
| I Quarantine | The bucket's stems are out of sellable stock while held. If already allocated or listed on an OPL row, that row can never be issued | Via accept (returns to storage) or reject (to Rejects) | Blocks the OPL row | Q: release is done by QC app users (role list used by the app: Packhouse Manager, Quality Manager, QUALITY CONTROLLER, System Manager: Q/mobile/api.py:3362, 12399) | Open = net stems in a quarantine warehouse > 0. No time limit. Local: 56 buckets, 10,240 stems, ALL 7+ days old, oldest 51 days |
| J Coldroom reject | Shelf and BAS totals reduced, so it can leave an OPL short of stems (allocation untouched) | none automatic | indirect | none | none, one-shot |
| K QC Packhouse | Informational, plus CAR | Grading QC offers replacement | none | CAR assigned to a hardcoded user | CAR status Pending/In Progress/Complete. Local: 1825 Pending, 5 In Progress, 44 Complete |

Roles actually present in code
- Packhouse side: Packing Supervisor, Assistant Packhouse Manager (doctype write on PRL, OPL, Bypass Log), Stock Manager/Stock User (Bucket Replacement), System Manager.
- Quality side: Packhouse Manager, Quality Manager, QUALITY CONTROLLER (QC incharge lists, Q/mobile/api.py:3362 and 12399).
- "Quality Supervisor" and "Shelf Clearer" were not found by grep in either app. Treat as non-existent here (UNCERTAIN: may be roles created only in the site DB; not checked).

## 3. SQL for the page (MariaDB, tested read-only on kaitet.local unless noted)

Join keys: `Packing Reject Log.order_pick_list` = `tabOrder Pick List.name`; `Packing Reject Log.bucket` = `Pick List Item.bucket`; `Pick List Item.parent` = OPL; `Pick List Item.farm/team/shelf`; `Order Pick List.team/farm`.

```sql
-- 3.1 Packing rejects per OPL (stems, bunches, reject rate, open count/age). PRL is empty locally (0 rows), so result is empty.
SELECT r.order_pick_list,
       SUM(r.stems_rejected) rej_stems, SUM(r.bunches) rej_bunches, COUNT(DISTINCT r.box_id) rej_boxes,
       SUM(r.status='Pending Approval') open_cnt,
       MAX(CASE WHEN r.status='Pending Approval' THEN TIMESTAMPDIFF(HOUR, r.creation, NOW()) END) oldest_open_h,
       ROUND(100*SUM(r.stems_rejected)/NULLIF(t.total,0),1) reject_rate_pct
FROM `tabPacking Reject Log` r
JOIN (SELECT parent, SUM(stock_qty) total FROM `tabPick List Item` WHERE parenttype='Order Pick List' GROUP BY parent) t ON t.parent=r.order_pick_list
GROUP BY r.order_pick_list;

-- 3.2 Per day / top reasons / by packer, farm, variety, bucket (swap the GROUP BY key)
SELECT DATE(creation) d, reason, recorded_by, farm, variety, bucket, SUM(stems_rejected) stems
FROM `tabPacking Reject Log` GROUP BY d, reason;      -- 0 rows locally
-- issue parameters (top QC defects): SELECT i.parameter, SUM(i.count) FROM `tabPacking Reject Issue` i GROUP BY 1;

-- 3.3 Open Packing Reject approvals and age
SELECT name, order_pick_list, box_id, stems_rejected, TIMESTAMPDIFF(HOUR, creation, NOW()) age_h
FROM `tabPacking Reject Log` WHERE status='Pending Approval' ORDER BY creation;   -- 0 rows locally

-- 3.4 Packhouse QC rejects per OPL (the data that exists locally). Reject stems are in child rows, not in stems_rejected.
SELECT qr.custom_order_pick_list opl, qr.custom_team team, DATE(qr.creation) d,
       SUM(IF(qp.action='Reject', qp.count, 0)) rej_stems, SUM(IF(qp.action='Quarantined', qp.count, 0)) quar_stems,
       MAX(qr.stems_checked) checked, MAX(qr.custom_bunches_affected) bunches
FROM `tabQuality Reporting` qr LEFT JOIN `tabQuality Parameter` qp ON qp.parent=qr.name
WHERE qr.control_point='Packhouse' GROUP BY qr.name;
-- Local: 360 rows, 359 with custom_order_pick_list; Quality Parameter totals: Reject 484 rows/5,391 stems, Quarantined 2,039 rows/47,281 stems (all control points).
-- Caution: Quality Reporting.stems_rejected = 0 on every packhouse row locally while custom_stems_rejected carries a value; use the child table or custom_stems_rejected (UNCERTAIN which is canonical).

-- 3.5 Not-found (missing) buckets per OPL, with age: the dominant local blocker
SELECT pli.parent opl, o.farm, o.team, COUNT(DISTINCT pli.bucket) missing_buckets, SUM(pli.stock_qty) missing_stems,
       MIN(pli.not_found_at) first_missing, TIMESTAMPDIFF(HOUR, MIN(pli.not_found_at), NOW()) age_h,
       ROUND(100*SUM(pli.stock_qty)/t.total,1) short_pct
FROM `tabPick List Item` pli JOIN `tabOrder Pick List` o ON o.name=pli.parent
JOIN (SELECT parent, SUM(stock_qty) total FROM `tabPick List Item` WHERE bucket!='' GROUP BY parent) t ON t.parent=pli.parent
WHERE pli.not_found=1 AND o.docstatus=1 AND o.short_accepted=0 GROUP BY pli.parent;
-- Local: 58 rows, 23 OPLs, 4,480 stems, all submitted, none short_accepted, not_found_at from 2026-10-03 to 2026-10-23.

-- 3.6 Completed-short OPLs: SELECT name, short_stems FROM `tabOrder Pick List` WHERE short_accepted=1;   -- 0 locally

-- 3.7 Open Bucket Replacements and age
SELECT name, order_pick_list, old_bucket, reason, stems, TIMESTAMPDIFF(HOUR, reported_at, NOW()) age_h
FROM `tabBucket Replacement` WHERE status='Open';       -- 2 rows locally (JTEST test data, 2026-10-23)

-- 3.8 Under-pack boxes (per OPL, per reason)
SELECT f.order_pick_list, i.box_id, i.under_pack_reason
FROM `tabFarm Packlist Item` i JOIN `tabFarm Pack List` f ON f.name=i.parent
WHERE IFNULL(i.under_pack_reason,'')!='' GROUP BY f.order_pick_list, i.box_id;
-- Local: 4 rows / 3 boxes ("End of day / cutoff" 3 rows, "Quality rejection" 1 row)

-- 3.9 Bypass per OPL/day: SELECT order_pick_list, DATE(creation), SUM(bunches), COUNT(*) FROM `tabPacking Bypass Log` GROUP BY 1,2;  -- 4 rows, all OPL-2026-00045

-- 3.10 Stem Replacement Log per OPL: SELECT opl, SUM(stems) FROM `tabStem Replacement Log` GROUP BY 1;  -- 2 rows (test data)
```

Not-per-OPL reject volume locally (all test/real mix): `Quarantine Accept` 57 SE, `Quarantine Rejects` 5, `Receiving Quarantined` 114, `Discard` 3 (all 2026-09-04 to 2026-10-23); `Packhouse Rejects`, `Coldroom Rejects`, `Airport Rejects` stock entries: 0 locally. `Rejects - KR` bin = 212 stems.

Per-team/packer/shelf: team = `Order Pick List.team` (Packing Teams) or `Quality Reporting.custom_team`; packer = `Packing Reject Log.recorded_by` / `Packing Bypass Log.packed_by`; shelf = `Pick List Item.shelf` via the PRL bucket.

Performance caution: `tabStock Entry Detail` is millions of rows; a naive join on stock_entry_type timed out (over 120s). Use `STRAIGHT_JOIN` from the filtered Stock Entry set, as in 4.1, or add a cache table. Needs `SET SQL_BIG_SELECTS=1` on this DB.

## 4. Detectable BEFORE packing

```sql
-- 4.1 Buckets still held in quarantine (net stems in quarantine warehouses), with age; tested (about 1 min on a cold cache)
SELECT se.custom_bucket_id bucket,
       SUM(IF(sei.t_warehouse LIKE '%Quarantin%', sei.qty, 0)) - SUM(IF(sei.s_warehouse LIKE '%Quarantin%', sei.qty, 0)) net_stems,
       MIN(se.posting_date) first_in, DATEDIFF(CURDATE(), MIN(se.posting_date)) age_d
FROM `tabStock Entry` se STRAIGHT_JOIN `tabStock Entry Detail` sei ON sei.parent = se.name
WHERE se.docstatus=1
  AND se.stock_entry_type IN ('Receiving Quarantined','Quarantine Transfer','Quarantine Accept','Quarantine Rejects','Material Transfer')
  AND (sei.t_warehouse LIKE '%Quarantin%' OR sei.s_warehouse LIKE '%Quarantin%')
GROUP BY se.custom_bucket_id HAVING net_stems > 0;
-- Local: 56 buckets, 10,240 stems, ALL older than 7 days (min first_in 2026-09-04, max age 51 days).
-- Cascade: join to Pick List Item on bucket (case: custom_bucket_id is lower case locally; Pick List Item.bucket may differ in case, normalise with LOWER()).
-- Local: those 56 buckets feed 22 OPLs / 19 buckets / 4,640 stems on 30 pick rows (24 already issued_rows). (The 7,450 stems / 40 OPLs figure is every bucket that EVER passed through quarantine, including accepted ones.)

-- 4.2 Buckets listed for discard but still on shelf/allocated
SELECT drb.bucket_id, dr.name FROM `tabDiscard Request Bucket` drb JOIN `tabDiscard Request` dr ON dr.name=drb.parent
WHERE dr.docstatus=1 AND dr.workflow_state='Approved' AND drb.discarded=0;
-- Local: 3,799 undiscarded rows (large backlog), 3 Discard stock entries only.

-- 4.3 Rejected after allocation: OPL rows whose bucket has a Quarantine Rejects / Coldroom Rejects entry after the row's creation
-- (Stock Entry.custom_bucket_id = pli.bucket, stock_entry_type IN (...), posting after pli.creation). Not run (cost); recommended as a nightly cache.
```

How the cascade works (confirmed from code)
- Quarantine/coldroom reject do not touch OPL rows. They only change stock, shelf qty and (coldroom) BAS totals (Q/mobile/api.py:14117-14143). The OPL row stays "allocated"; the shortage appears only when the issuing scan fails or the bucket is marked not found. So rejected-after-allocated is a latent blocker that this page can surface early.
- Quarantine release: the "accept" path moves stems back to `Kapkolia Receiving Storage - KR` or the original warehouse (7497). Nothing re-links to OPLs.
- Replacement candidates exclude: allocated, in-transit, discard-listed, over-age buckets (`_replacement_candidates` PH/sales_allocation.py:2921-2960; discard_age default 5, per-farm `max_allocation_age`).

## 5. Ranked reject-related blockers for the CEO page

| Rank | Blocker | Detection rule | Suggested threshold | Owner role (actual) | Local volume |
|---|---|---|---|---|---|
| 1 | OPL with bucket(s) marked not found, no replacement, not completed short | 3.5: pli.not_found=1, OPL docstatus=1, short_accepted=0 | Amber more than 4h, red more than 24h or short_pct more than 10 | Packing Supervisor / Assistant Packhouse Manager (can `completeOplShort`), Stock Manager (Bucket Replacement) | 23 OPLs, 58 rows, 4,480 stems, oldest 22 days |
| 2 | Packing reject awaiting supervisor decision | PRL.status='Pending Approval' (3.3) | Amber more than 30 min, red more than 2h (packer is idle while waiting) | Packing Supervisor, Assistant Packhouse Manager | 0 (flow unused locally) |
| 3 | Allocated bucket currently in quarantine | 4.1 joined to Pick List Item | Any stems at all on an OPL due within 48h | Packhouse Manager / Quality Manager / QUALITY CONTROLLER | 22 OPLs, 19 buckets, 4,640 stems, all quarantines older than 7 days |
| 4 | Quarantine dwell | 4.1 age_d | Amber more than 2 days, red more than 5 days (Production Settings discard_age is 5) | Quality Manager / QUALITY CONTROLLER | 56 buckets, 10,240 stems, all more than 7 days (oldest 51) |
| 5 | Open Bucket Replacement (replaced bucket still unaccounted) | 3.7 status='Open' | Amber more than 24h, red more than 72h | Stock Manager / Packing Supervisor | 2 (test data) |
| 6 | Short, unreplaced rejects (quality issue, no donor) | OPL allocated stems below required after a Bucket Replacement with reason "Quality issue" and new_bucket empty | Any | Packing Supervisor | 0 |
| 7 | Reject rate per OPL/farm/variety high | 3.1 reject_rate_pct or 3.4 rej/checked | Amber more than 2%, red more than 5% (CAR code uses 6% for Other) | Quality Manager | QC child rows: 5,391 rejected stems in 484 rows (all control points) |
| 8 | Pending Corrective Action Reports | CAR.status='Pending' older than target_date | Any older than 7 days | assigned_to (hardcoded user in code), Quality Manager | 1,825 Pending, 5 In Progress (backlog, likely noise) |
| 9 | Under-packed boxes with reason "Quality rejection" | 3.8 | Any | Packing Supervisor | 1 |
| 10 | Discard backlog (approved but not discarded buckets) | 4.2 | more than 24h after approval | Quality Controller / whoever handles discards (UNCERTAIN) | 3,799 bucket rows |

Informational only (no blocker): bypass logs (4 local, 5 bunches wait), stem replacement logs (2), packhouse inspection logs (2).

## 6. Gaps and cautions
- Local PRL, Packhouse Rejection Reason, Packhouse/Coldroom/Airport Rejects entries are all empty: validate the PRL queries against a production copy before trusting thresholds.
- No state machine timestamps for "Pending Approval" other than `creation`; use `TIMESTAMPDIFF` on `creation`.
- `Quality Reporting.order_pick_list` (standard Link) is unset for packhouse rows; use `custom_order_pick_list`. Packing Reject Log's QR writer sets both (packing_reject.py:140 and 146).
- Quality Reporting names are not unique-safe for concurrent writes (prefix+count, then loop on exists): low risk for reads.
- `completeOplShort`, `close_without_replacement` and `resolveBucketReplacement` lack role checks in code: a page can show who did them but cannot assume they were supervisors.
- Quarantine determination relies on warehouse names containing "Quarantin"; case differs between Stock Entry `custom_bucket_id` (lowercase) and some pick rows (UPPER in code paths like `UPPER(bucket_id)`). Normalise with LOWER().
