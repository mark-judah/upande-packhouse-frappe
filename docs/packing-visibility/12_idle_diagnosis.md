# 12 Why is the packing team idle? Diagnostic logic for the Packing Visibility dashboard

Research only (no code edits, no DB writes). Paths relative to `apps/upande_packhouse/upande_packhouse/` unless stated; `api.py` = `mobile/api.py`. Local site `kaitet.local` is a thin snapshot: every SQL below was executed read-only there (MariaDB 11.8, CTEs, window functions, `GROUP_CONCAT ... LIMIT`); results are proofs of syntax and of logic, NOT of production magnitudes. Prototype sources are kept in the session scratchpad (`.../scratchpad/idle/*.sql`, `idle_minutes.py`) and are reproduced in full below. Docs 01-08 are not repeated; they are cited as "doc 0N".

SQL conventions: driver placeholders `%(name)s` (frappe.db.sql); a literal `%` is written `%%`. All times are site time (Africa/Nairobi, stored naive).

---------------------------------------------------------------------------------------------------
## 0. Corrections and facts that change the design (read first)

1. **"Packing payload" is NOT a refusal log.** `createOrUpdateFarmPackList` writes an Error Log titled `Packing payload` on EVERY call, first thing (`api.py:1000`), before any gate. A refused call is the `except Exception` branch writing a second row titled `Farm Pack List Packing Error` (`api.py:1488-1489`) whose `error` text is `str(e)` (e.g. "Schedule order for team ...", "Over-pack blocked ...", "This bunch has not been graded ...", `api.py:1060, 1156, 1270, 1286`; schedule gate `api.py:982-991`). That row carries NO OPL id; pair it with the preceding `Packing payload` row of the same `owner` within 5 s (the payload text contains `'custom_order_pick_list': 'OPL-...'`) - query S6. The issued-cap refusal ("Only P% is issued...", `api.py:1299`, `IssuedCapError`) is re-raised before the log line (`api.py:1486-1487`) so it leaves **no** `Packing Error` row: it is visible only as a payload with no following Version/FPL change (silent refusal). Error Log retention is 14 days (`Logs To Clear`), so refusals are a live/last-two-weeks signal only. Error Log `method` holds the title in v16.
2. **`Farm Pack List.modified` is NOT the last-scan time.** Every scan saves the doc (`api.py:1364`) but `modified` is also bumped by submit and by any later desk edit (local: a Sept-11 FPL has `modified` 2026-09-20). Last scan = `GREATEST(FPL.creation, MAX(Version.creation))` over the team's FPLs (FPL `track_changes=1`, `doctype/farm_pack_list/farm_pack_list.json:292`; `Version` is not in Logs To Clear locally - confirm on prod). One Version row per saving scan (only when something changed), owner = scanner (doc 05 5.2). FPL.creation is the first scan (the insert, no Version row).
3. **Scheduling hides orders from issuing AND packing, not only packing.** `_listed_opls` (`api.py:2881`) lists an OPL only if it is on a Packhouse Schedule, or `show_unscheduled_in_<station>` is on, or its sales farm is an own-packing farm (Karen: `scheduler._own_packing_farms`, `api/remote_transfer/scheduler.py:10`). It is called for packing (`api.py:3032`), issuing (`api.py:3686`) and the app scheduler (`api.py:3942`). So an unscheduled, submitted OPL cannot even be issued: it is invisible to the floor, and the team "stands" with stock physically ready.
4. **Packability rule (what "something to pack" means)** = `fetchPicklists` (`api.py:2906-2972`): `docstatus=1` AND `Sales Order.delivery_date = selected date` (app default: tomorrow; packing runs a day ahead, `api.py:6394`) AND (issued stems > 0 AND issued% >= `_pack_waiting_min_pct()` (Production Settings `pack_waiting_replacement_pct`, 50 when unset, `api.py:3427`, filter `api.py:2957, 2969`) OR every found bucket issued) AND listed (3). Packing is then capped at issued stems (`_issued_cap_error`, `api.py:3433`; call `api.py:1299`). Hence the **ready pool** `R = SUM(max(0, issued_stems - packed_stems))` over visible, packable OPLs is the exact number of stems a packer could scan right now. This is the pivot of the whole tree.
5. **Order gate**: with `allow_pack_out_of_sequence=1` (prod) `_schedule_blockers` returns no blockers (`api.py:3266-3280`), so the schedule cannot be a cause of idle; when it is 0, `PACK_DONE_SQL` (`api.py:3243`) defines "done" and a team whose #1 is blocked is blocked on everything behind it (see tier 9 note and edge case E9). Gate toggles have no history (Production Settings `track_changes=0`, doc 08 s6), so snapshot them daily.
6. **issued_at lags up to 5 min** (stamped by the `*/5` job, `api/remote_transfer/transfer_steps.py:102 reconcile`, `:141 run`; cron `hooks.py:296`), and `issued_by` is the last editor of the row. Use `COALESCE(issued_at, modified)` and never classify a team as idle inside the 5-minute stamp lag (`READY_GRACE`, below).
7. **Day frame.** A team-day is the processing day `pday = delivery_date - 1`, frame `[pday 06:00, pday+1 06:00)`. Local FPL scans exist at 01:14, 03:16, 04:21, 23:21 (night packing), so a midnight cut would split shifts; and `Packhouse Schedule.schedule_date = delivery_date - 1` (doc 08).
8. **Teams exist only on orders** (doc 05 s4), so "team activity" = scans on FPLs of that team's OPLs, whoever scanned. A worker floating to another team simply shows as the other team's activity (E2).

---------------------------------------------------------------------------------------------------
## 1. Definition of "team idle" and "since when"

### 1.1 Primitives
* **Scan event** of team T: a row of `E(T)` = `{FPL.creation}` UNION `{Version.creation where ref_doctype='Farm Pack List'}` for FPLs whose `order_pick_list` has `OPL.team = T` (query S1). Do not use FPL.modified.
* **Scan mode** per team: `scan` (Eldama A, Eldama B, Bravo: bunch-by-bunch) or `bulk` (Team A, Team B, Jamafa: a data-entry person keys the whole list and submits within about a minute). Stored as a config table row per team (override) and auto-detected by S9 (>=70% of submitted FPLs open for <=3 min AND <=3 saves => bulk). Local detection: Jamafa bulk, Bravo scan (Team A/B local data is test data, so the override table must win).
* **Work set** `W(T)`: OPLs with `team=T`, `docstatus<2`, delivery_date in {today, tomorrow} plus older still-open ones, and `stems>0`. Open = not `packed_out` (`packed >= stems - not_found_stems - 0.5` or an FPL submitted).
* **Ready pool** `R(T)` (section 0.4), split into `R_visible` (listed) and `R_hidden` (packable but unscheduled, invisible to the floor).
* **Config** (new, one doc `Packing Visibility Settings`; defaults in brackets): `day_open` [06:00], `shift_start` [08:00], `lunch` [12:30-13:30; calibrate with S10: local issuing shows the dip 11:30-14:30], `day_end` [17:30], `tick` [5 min = the issuing-stamp cadence], `scan_grace` [5 min], `ready_grace` [10 min], `amber` [30], `red` [60], `bulk_amber/bulk_red` [180/360 min of ready-pool age], `min_ready_minutes` [10], per-team packrate (stems per active minute; median from Version deltas of the last 14 days).

### 1.2 Status of a team right now (evaluated in this order)
| status | rule |
|---|---|
| `closed` | the packhouse as a whole had no scan and no issue today (holiday/Sunday) OR (now > day_end AND no scan in the last 60 min AND R=0) |
| `break` | now inside lunch window |
| `not_started` | now < shift_start AND no scan today AND no issue yet today |
| `done` | W non-empty and every OPL packed_out (tier 10: remaining work is downstream - labels, precooling, staging) |
| `working` | scan mode: last scan <= `scan_grace`+5 = 10 min ago. Bulk mode: a list was entered in the last `bulk_amber` min, OR R=0 |
| `standing` (idle) | none of the above; severity from `idle_min`: amber > 30, red > 60 (per-code overrides allowed) |

### 1.3 The idle clock
```
last_scan      = MAX(E(T))  within the day frame            (NULL if none)
clock_start    = GREATEST( IFNULL(last_scan, '0000'),
                           shift_start_today,
                           first_issue_today(T) )           -- first Pick List Item.issued_at of T's work set, today
idle_since     = clock_start                                -- "since when"
idle_min       = TIMESTAMPDIFF(MINUTE, idle_since, now) - overlap(idle_since..now, lunch)    -- break time never counts
```
Why `first_issue_today` is in the max: a team cannot pack before something was issued (packing needs >0 issued stems, section 0.4), so 08:00-09:40 with nothing issued is "waiting for issuing (5)", not "inactive since 08:00"; the dashboard still shows the wait, but under its reason. When R becomes >0 after a stall, the *team-fault* clock restarts at the moment the pool became non-empty (`ready_since`, approximated by the issued_at that lifted the pool, +`ready_grace`), so the reason-6 age is `now - GREATEST(last_scan, ready_since + ready_grace)`.
Lunch: if the gap began before `lunch_start` and is still open after `lunch_end`, `idle_min` excludes the lunch overlap, and the *threshold clock* restarts at `lunch_end` for the first 10 minutes (packers need to return): `idle_min` is clamped to `now - lunch_end` until `lunch_end + scan_grace` has passed. Day end: nothing accrues after `day_end` unless the team scanned in the last 60 min and R>0 (overtime).
SQL for the live clock (per team, net of lunch): query S2.

### 1.4 Avoiding false idle (all implemented as guards before a team can turn amber)
1. W empty -> reason 0 (grey "no work"), never amber/red.
2. All OPLs packed_out -> `done`, not idle.
3. R=0 -> the team is *waiting*, tagged upstream (reasons 1-5,7,8); severity still shows (it is standing) but attribution = `system`, not `team`.
4. R>0 but `R / team_packrate < min_ready_minutes` (a few stems) -> not reason 6; classified by the next blocker (waiting for the next issue batch).
5. Ticks inside `ready_grace` of a stock-issue event are not idle (walking the stems over; 5 min stamp lag).
6. Bulk teams are never amber on scan silence; only on ready-pool age (`bulk_amber/red`) - see E1.
7. Lunch and pre-shift/post-shift excluded (1.3).
8. Closed days excluded (`closed`).
9. A team with only `hidden` OPLs is "standing: 1 unscheduled", with the hidden stems quantified - not "team inactive".
10. Frame clipping: events outside `[pday 06:00, pday+1 06:00)` never extend the window (the prototype showed a 14,000-minute "window" for a Sept-11 FPL touched on Sept 20 before clipping).

---------------------------------------------------------------------------------------------------
## 2. The decision tree: ONE primary reason per team-moment

### 2.1 Codes
| code | reason | owner role (who to call) | owner person (resolver, first non-null) |
|---|---|---|---|
| 0 | No work assigned to the team | Assistant Packhouse Manager | creator of the unassigned OPLs (`OPL.owner`); scheduler page "Distribute to teams" |
| 1 | OPL(s) not scheduled (hidden from issuing and packing lists) | Assistant Packhouse Manager | last `Packhouse Schedule.modified_by` for the day (Administrator = auto job: `auto_remote_transfer_scheduling` off or OPL has no team/`draft_plain`) |
| 2 | OPL draft, administrative (short/unallocated lines, mix group incomplete, no rows) | Assistant Packhouse Manager (allocator) | `OPL.owner` |
| 3 | Waiting for stock: allocated stock unusable or lines have no stock | Quality Manager (held/discard/quarantine), Stock Manager (ghost/shelf), farm for empty shelf | `Discard Request.approved_by`; `Shelving Log.shelved_by`; allocator `OPL.owner` |
| 4 | Waiting for transfer arrival (awaiting / on trolley / in transit / arrived not shelved) | Assistant Packhouse Manager (planner), truck driver, Farm Manager <farm> | `Bucket Request Trip.released_by`, `Vehicle.employee`, farm of the rows |
| 5 | Waiting for issuing (OPL submitted, issued below gate or pool exhausted, issuable stock exists) | Packing Supervisor (issuers) | recent `Pick List Item.issued_by` for the team |
| 6 | Issued stems ready but team not packing (staffing/attendance) | Packing Supervisor / team lead | no team-lead field: `FPL.owner` of the team's last FPL |
| 7 | Not-found bucket blocking (remaining stems are missing, short not accepted) | Farm Manager <farm>, Packing Supervisor | `Pick List Item.not_found_by` |
| 8 | Quality reject pending approval | Packing Supervisor / Assistant Packhouse Manager (`APPROVER_ROLES`, doc 02) | `Packing Reject Log.recorded_by` (reporter), approver pool |
| 9 | Packing refusals (schedule order, over-pack, bunch not graded, issued cap) | Packing Supervisor (order/cap), grader/Quality (not graded) | `Error Log.owner` of the refused call |
| 10 | Downstream only (packing complete; labels/precool/staging pending) - not idle | n/a (dispatch cold store, precooling attendant) | none |

### 2.2 Evaluation: per-OPL classification, then pick one per team
The numbering is the brief's, but the **evaluation order is by evidence and proximity to packing**, because a literal 0-first order would label a team with one unscheduled OPL and 40 OPLs mid-issue as "not scheduled". Rules:

**A. Per OPL (first matching row wins; stems are `Pick List Item.stock_qty`, found rows only):**
| order | condition | OPL code |
|---|---|---|
| a | `packed_out` and not_found = 0 and submitted | 10 |
| b | draft AND `moving_stems>0` | 4 (specific beats 2; `opl_submit_blockers` text "N bucket(s) still in transit", `page/sales_allocation/sales_allocation.py:2245`) |
| c | draft AND `bad_stems>0` | 3 |
| d | draft (anything else: unallocated/short lines, mix group, no rows) | 2 |
| e | pending Packing Reject Log AND `ready<=0` AND nothing issuable/moving | 8 |
| f | hidden AND gate_ok AND ready>0 | 1a (reported as code 1, flag `hidden_ready`) |
| g | gate_ok AND ready>0 | 6-candidate (ready, visible) |
| h | `issuable_stems>0` | 1b if hidden else 5 |
| i | `moving_stems>0` | 4 |
| j | `bad_stems>0` | 3 |
| k | `nf_stems>0` | 7 |
| l | else | 5 |

`gate_ok` = issued >= `pack_waiting_replacement_pct`% of stems OR nothing issuable/moving/bad is left (api.py:2957/2969). `hidden` = not on that day's Packhouse Schedule AND sales farm not an own-packing farm AND `show_unscheduled_in_packing=0`. `bad_stems` = unissued, non-moving rows whose bucket is on an open discard request (Pending Approval/Approved), on no shelf (ghost), or older than `discard_age` (Production Settings, 5). `issuable_stems` = the rest of the unissued, found, non-moving rows. Quarantine (doc 02 I) and cooling (doc 03 B3e) are not in the live SQL (heavy Stock Entry join); add them as extra `bad` terms from `core/stock.bucket_rows` in the API layer.

**B. Per team (first matching row wins):**
| order | condition on the team's OPL codes | team code |
|---|---|---|
| 0 | W empty | 0 |
| 1 | all OPLs code 10 | 10 (`done`) |
| 2 | scan-refusal evidence in the last 30 min (S6: `refusals>0`) on the team's OPLs AND R_visible>0 | 9 |
| 3 | R_visible>0 AND ready minutes >= `min_ready_minutes` | 6 |
| 4 | R_hidden>0 (hidden_ready) | 1 |
| 5 | any OPL 8 | 8 |
| 6 | any OPL 5 | 5 |
| 7 | any OPL 1b | 1 |
| 8 | any OPL 4 | 4 |
| 9 | any OPL 3 | 3 |
| 10 | any OPL 7 | 7 |
| 11 | any OPL 2 | 2 |

Why this order: rows 2-4 are "the team could/should be packing" (evidence of a blocked attempt, else inference of unstarted work, else work that exists but is invisible); rows 5-11 are "nothing to pack": we name the cause closest to unblocking (an issuer is already working on it (5) < a one-click schedule fix (1) < a truck on the road (4) < stock to re-allocate/find (3) < a missing bucket (7) < an administrative draft (2))... and the full ranked distribution is always returned in `reasons[]` so nothing is hidden by the single label. Where the brief's priority (0,1,2,3...) matters - upstream causes that dominate when nothing downstream exists - it is preserved because lower rows only fire when higher (downstream) ones have no OPLs.
Gate note: with `allow_pack_out_of_sequence=0` the team-level head-of-line rule applies instead: take the OPL with the smallest `Packhouse Schedule Order.sequence` that is not `PACK_DONE`; its OPL code is the team code (a blocked #1 blocks everything behind it, `api.py:3266-3310`); `fetchPicklists` already returns `waits_for` (`api.py:3017-3030`).

### 2.3 SQL (live; validated on kaitet.local)
Parameters: `ds` tuple of delivery dates (today, tomorrow, plus older open), `gate_pct` (`pack_waiting_replacement_pct` or 50), `discard_age`, `show_unsched_pack`, `own_farms` (from `_own_packing_farms()`), `hub`.

**S3/S4: per OPL classification + team primary code (`s_team.sql`).**
```sql
-- PER-OPL PRIMARY BLOCKER (live). Params: ds = tuple of delivery dates in the work set (today, tomorrow, + older open), gate_pct (50), discard_age (5),
-- show_unsched_pack (0/1), own_farms (tuple, from scheduler._own_packing_farms()), hub (transfer_hub_farm)
WITH shelf AS (
  SELECT UPPER(bucket_id) b, SUM(stem_qty) q, MIN(COALESCE(harvest_date, DATE(date_added))) hd
  FROM `tabShelf Item` WHERE stem_qty > 0 GROUP BY UPPER(bucket_id)),
disc AS (
  SELECT DISTINCT UPPER(b.bucket_id) b FROM `tabDiscard Request Bucket` b
  JOIN `tabDiscard Request` r ON r.name = b.parent
  WHERE b.discarded = 0 AND r.workflow_state IN ('Pending Approval','Approved')),
rows_ AS (   -- per OPL stems by physical state of the UNISSUED, found rows
  SELECT p.parent opl,
    SUM(IFNULL(p.stock_qty,0)) stems,
    SUM(IF(IFNULL(p.issued,0)=1 OR IFNULL(p.issued_offline,0)=1, p.stock_qty, 0)) issued_stems,
    SUM(IF(IFNULL(p.not_found,0)=1, p.stock_qty, 0)) nf_stems,
    SUM(IF(IFNULL(p.issued,0)=0 AND IFNULL(p.issued_offline,0)=0 AND IFNULL(p.not_found,0)=0
           AND (IFNULL(p.awaiting_transfer,0)=1 OR IFNULL(p.loaded_in_trolley,0)=1 OR IFNULL(p.in_transit,0)=1), p.stock_qty, 0)) moving_stems,
    SUM(IF(IFNULL(p.issued,0)=0 AND IFNULL(p.issued_offline,0)=0 AND IFNULL(p.not_found,0)=0
           AND NOT (IFNULL(p.awaiting_transfer,0)=1 OR IFNULL(p.loaded_in_trolley,0)=1 OR IFNULL(p.in_transit,0)=1)
           AND (disc.b IS NOT NULL OR sh.b IS NULL OR DATEDIFF(CURDATE(), sh.hd) >= %(discard_age)s), p.stock_qty, 0)) bad_stems,
    SUM(IF(IFNULL(p.issued,0)=0 AND IFNULL(p.issued_offline,0)=0 AND IFNULL(p.not_found,0)=0
           AND NOT (IFNULL(p.awaiting_transfer,0)=1 OR IFNULL(p.loaded_in_trolley,0)=1 OR IFNULL(p.in_transit,0)=1)
           AND disc.b IS NULL AND sh.b IS NOT NULL AND DATEDIFF(CURDATE(), sh.hd) < %(discard_age)s, p.stock_qty, 0)) issuable_stems,
    MIN(IF(IFNULL(p.not_found,0)=1, p.not_found_at, NULL)) nf_since,
    MAX(p.issued_at) last_issued_at
  FROM `tabPick List Item` p
  LEFT JOIN shelf sh ON sh.b = UPPER(p.bucket)
  LEFT JOIN disc ON disc.b = UPPER(p.bucket)
  WHERE p.parenttype='Order Pick List' AND COALESCE(p.bucket,'') <> ''
  GROUP BY p.parent),
packed AS (
  SELECT f.order_pick_list opl, SUM(COALESCE(NULLIF(i.stock_qty,0), IFNULL(i.bunch_qty,0)*10)) packed,
         MAX(f.docstatus=1) fpl_submitted
  FROM `tabFarm Pack List` f JOIN `tabFarm Packlist Item` i ON i.parent=f.name AND i.parenttype='Farm Pack List'
  WHERE f.docstatus < 2 GROUP BY f.order_pick_list),
sched AS (
  SELECT pso.order_pick_list opl, ps.schedule_date sd, MIN(pso.sequence) seq FROM `tabPackhouse Schedule Order` pso
  JOIN `tabPackhouse Schedule` ps ON ps.name = pso.parent
  GROUP BY pso.order_pick_list, ps.schedule_date),
rej AS (
  SELECT order_pick_list opl, COUNT(*) n, MIN(creation) since FROM `tabPacking Reject Log`
  WHERE status = 'Pending Approval' GROUP BY order_pick_list),
st AS (
  SELECT o.name opl, o.team, o.docstatus ds, o.short_accepted, o.pack_issued_now, so.delivery_date,
    r.stems, r.issued_stems, r.nf_stems, r.moving_stems, r.bad_stems, r.issuable_stems, r.nf_since, r.last_issued_at,
    IFNULL(k.packed,0) packed, IFNULL(k.fpl_submitted,0) fpl_submitted,
    sc.seq, rj.n rej_n, rj.since rej_since,
    (sc.opl IS NULL AND so.farm NOT IN %(own_farms)s AND NOT %(show_unsched_pack)s) hidden,
    o.modified opl_modified
  FROM `tabOrder Pick List` o
  JOIN `tabSales Order` so ON so.name = o.sales_order AND so.docstatus = 1
  LEFT JOIN rows_ r ON r.opl = o.name
  LEFT JOIN packed k ON k.opl = o.name
  LEFT JOIN sched sc ON sc.opl = o.name AND sc.sd = DATE_SUB(so.delivery_date, INTERVAL 1 DAY)
  LEFT JOIN rej rj ON rj.opl = o.name
  WHERE o.docstatus < 2 AND so.delivery_date IN %(ds)s AND IFNULL(r.stems,0) > 0),
cls AS (
  SELECT st.*,
    GREATEST(0, st.issued_stems - st.packed) ready_stems,
    (st.issued_stems >= st.stems * %(gate_pct)s/100 OR (st.issuable_stems + st.moving_stems + st.bad_stems = 0)) gate_ok,
    (st.packed >= st.stems - st.nf_stems - 0.5 OR st.fpl_submitted = 1) packed_out
  FROM st),
oc AS (
SELECT cls.opl, cls.team, cls.ds, cls.seq, cls.hidden, cls.stems, cls.issued_stems, cls.packed, cls.ready_stems, cls.nf_since, cls.rej_since, cls.opl_modified, cls.last_issued_at, cls.moving_stems, cls.bad_stems, cls.issuable_stems, cls.nf_stems,
  CASE
    WHEN cls.packed_out AND cls.ds = 1 AND cls.nf_stems = 0 THEN 10                       -- packing complete (downstream)
    WHEN cls.ds = 0 AND cls.moving_stems > 0 THEN 4
    WHEN cls.ds = 0 AND cls.bad_stems > 0 THEN 3
    WHEN cls.ds = 0 THEN 2
    WHEN cls.rej_n > 0 AND cls.ready_stems <= 0 AND cls.issuable_stems = 0 AND cls.moving_stems = 0 THEN 8
    WHEN cls.hidden AND cls.gate_ok AND cls.ready_stems > 0 THEN 11                       -- 1a hidden + ready (reported as code 1, flag hidden_ready)
    WHEN cls.gate_ok AND cls.ready_stems > 0 THEN 6                                       -- ready, packable (6 / 9 decided at team level)
    WHEN cls.issuable_stems > 0 THEN IF(cls.hidden, 1, 5)                                 -- 1b hidden unissued / 5 issuing pending
    WHEN cls.moving_stems > 0 THEN 4
    WHEN cls.bad_stems > 0 THEN 3
    WHEN cls.nf_stems > 0 THEN 7
    ELSE 5 END AS code
FROM cls

)

-- TEAM ROLLUP: one primary reason per team (live). R = ready stems on visible packable OPLs.
SELECT IFNULL(team,'(no team)') team,
  COUNT(*) opls, SUM(code=10) done_opls, SUM(code<>10) open_opls,
  SUM(IF(code=6, ready_stems, 0)) R_visible, SUM(IF(code=11, ready_stems, 0)) R_hidden,
  SUM(code=8) n8, SUM(code=5) n5, SUM(code=1) n1b, SUM(code=4) n4, SUM(code=3) n3, SUM(code=7) n7, SUM(code=2) n2,
  CASE
    WHEN SUM(code<>10) = 0 THEN 10
    WHEN SUM(IF(code=6, ready_stems, 0)) > 0 THEN 6          -- (9 override when refusals seen, see S6)
    WHEN SUM(code=11) > 0 THEN 1
    WHEN SUM(code=8) > 0 THEN 8
    WHEN SUM(code=5) > 0 THEN 5
    WHEN SUM(code=1) > 0 THEN 1
    WHEN SUM(code=4) > 0 THEN 4
    WHEN SUM(code=3) > 0 THEN 3
    WHEN SUM(code=7) > 0 THEN 7
    WHEN SUM(code=2) > 0 THEN 2 END AS primary_code
FROM oc GROUP BY team ORDER BY team
```
Local run (delivery 2026-10-10 and 10-11; no Packhouse Schedule rows exist locally, so almost everything is `hidden`): Bravo (Karen, own-farm so not hidden) R_visible 2,800 -> code 6; Eldama A/B, Team A/B -> code 1 with R_hidden 4,590 / 1,570 / 9,800 / 9,712 stems; Jamafa R_visible 300 -> 6; teamless OPLs (4) -> 3. This is exactly the failure the dashboard must surface: ready stems nobody can see.

**S5: reason detail (count, stems, age of cause, 3 oldest OPLs) per team x code (`s_detail.sql`; same CTE chain, final SELECT only).**
```sql
-- ... same WITH shelf, disc, rows_, packed, sched, rej, st, cls, oc chain as S3/S4 above (oc additionally selects moving_since) ...
-- REASON DETAIL: per team x code: OPL count, stems affected, age of the cause, 3 oldest example OPLs
SELECT IFNULL(team,'(no team)') team, IF(code=11,1,code) code, COUNT(*) opls,
  SUM(CASE WHEN code IN (1,11) THEN GREATEST(ready_stems, issuable_stems) WHEN code=2 THEN stems WHEN code=3 THEN bad_stems
           WHEN code=4 THEN moving_stems WHEN code=5 THEN issuable_stems WHEN code=6 THEN ready_stems
           WHEN code=7 THEN nf_stems ELSE stems END) stems_affected,
  MIN(since) oldest_since, TIMESTAMPDIFF(MINUTE, MIN(since), NOW()) age_min,
  GROUP_CONCAT(opl ORDER BY since SEPARATOR ',' LIMIT 3) example_opls
FROM (SELECT oc.*, CASE WHEN code=4 THEN COALESCE(moving_since, opl_modified) WHEN code=7 THEN nf_since WHEN code=8 THEN rej_since
                        WHEN code IN (5,6) THEN COALESCE(last_issued_at, opl_modified) ELSE opl_modified END since FROM oc) x
WHERE code <> 10 GROUP BY team, IF(code=11,1,code) ORDER BY team, code
```
Age of cause per code (the `since` expression): 4 = oldest moving row's `modified` (use `left_farm_at`/trip `dispatched_at` where present); 5/6 = last `issued_at` of the OPL; 7 = `not_found_at`; 8 = `Packing Reject Log.creation`; 1/2/3 = `OPL.modified` as an upper bound (no assigned/unscheduled clock exists - snapshots fix this, section 3.5). Production should replace `NOW()` by the request time and add `AND so.business_unit='Roses'`, exclude JTEST.

**Code 3 sub-reasons (`s_sub3.sql`)** held/discard-listed vs not on any shelf (ghost) vs too old:
```sql
-- sub-reasons of code 3 (stock cannot be issued), per team; unissued found rows of open submitted OPLs not moving
SELECT IFNULL(o.team,'(no team)') team,
  SUM(IF(disc.b IS NOT NULL, p.stock_qty, 0)) held_discard_listed,
  SUM(IF(disc.b IS NULL AND sh.b IS NULL, p.stock_qty, 0)) not_on_any_shelf,
  SUM(IF(disc.b IS NULL AND sh.b IS NOT NULL AND DATEDIFF(CURDATE(), sh.hd) >= %(discard_age)s, p.stock_qty, 0)) too_old
FROM `tabPick List Item` p
JOIN `tabOrder Pick List` o ON o.name = p.parent AND o.docstatus = 1
JOIN `tabSales Order` so ON so.name = o.sales_order AND so.delivery_date IN %(ds)s
LEFT JOIN (SELECT UPPER(bucket_id) b, MIN(COALESCE(harvest_date, DATE(date_added))) hd FROM `tabShelf Item` WHERE stem_qty>0 GROUP BY 1) sh ON sh.b = UPPER(p.bucket)
LEFT JOIN (SELECT DISTINCT UPPER(b.bucket_id) b FROM `tabDiscard Request Bucket` b JOIN `tabDiscard Request` r ON r.name=b.parent
           WHERE b.discarded=0 AND r.workflow_state IN ('Pending Approval','Approved')) disc ON disc.b = UPPER(p.bucket)
WHERE p.parenttype='Order Pick List' AND COALESCE(p.bucket,'')<>'' AND IFNULL(p.issued,0)=0 AND IFNULL(p.issued_offline,0)=0 AND IFNULL(p.not_found,0)=0
  AND IFNULL(p.awaiting_transfer,0)=0 AND IFNULL(p.loaded_in_trolley,0)=0 AND IFNULL(p.in_transit,0)=0
GROUP BY o.team
```
**Code 2/3 line-level sub-reason (`s_lines.sql`)**: `allocatable_now` (stock exists, allocator has not acted: code 2), `wrong_length_only`, `empty_shelf_awaiting_harvest`, `partial_stock` (code 3). `shelf_exact` is not net of other open allocations, so `allocatable_now` is an upper bound; use `api/v2/allocation_planning.get_allocation_planning` `cover[]` where exactness matters. "Awaiting harvest" vs "nothing planned" needs the harvest forecast (not found in code): `empty_shelf` is the honest label.
```sql
-- B1/code 2-3 sub-reason: SO lines of the team's OPLs that are short-allocated, and whether ANY free stock of that variety exists
SELECT o.team, so.name so, soi.name soi, soi.item_code variety, soi.custom_length len,
  soi.stock_qty ordered, IFNULL(al.alloc,0) allocated, soi.stock_qty - IFNULL(al.alloc,0) short,
  IFNULL(fx.q,0) shelf_exact, IFNULL(fv.q,0) shelf_variety_any_len,
  CASE WHEN IFNULL(fx.q,0) >= soi.stock_qty - IFNULL(al.alloc,0) THEN 'allocatable_now'
       WHEN IFNULL(fv.q,0) > IFNULL(fx.q,0) THEN 'wrong_length_only'
       WHEN IFNULL(fv.q,0) = 0 THEN 'empty_shelf_awaiting_harvest' ELSE 'partial_stock' END sub_reason
FROM `tabOrder Pick List` o JOIN `tabSales Order` so ON so.name=o.sales_order AND so.delivery_date IN %(ds)s
JOIN `tabSales Order Item` soi ON soi.parent=so.name
JOIN (SELECT DISTINCT parent, sales_order_item FROM `tabPick List Item` WHERE parenttype='Order Pick List') x ON x.parent=o.name AND x.sales_order_item=soi.name
LEFT JOIN (SELECT sales_order_item soi, SUM(stock_qty) alloc FROM `tabPick List Item` p JOIN `tabOrder Pick List` oo ON oo.name=p.parent AND oo.docstatus<2 GROUP BY sales_order_item) al ON al.soi=soi.name
LEFT JOIN (SELECT variety, stem_length, SUM(stem_qty) q FROM `tabShelf Item` WHERE stem_qty>0 GROUP BY variety, stem_length) fx ON fx.variety=soi.item_code AND fx.stem_length=soi.custom_length
LEFT JOIN (SELECT variety, SUM(stem_qty) q FROM `tabShelf Item` WHERE stem_qty>0 GROUP BY variety) fv ON fv.variety=soi.item_code
WHERE o.docstatus<2 AND soi.stock_qty - IFNULL(al.alloc,0) > 0.5
```
Cut-offs/caveats of the detection rules: ghost (not on any shelf) is noisy (doc 03 B3a: much of it is historic issue-before-flag); locally it dominates code 3 (e.g. Eldama A 11,590 ghost / 12,230 too old / 2,740 discard-listed stems), so on production restrict ghost to OPLs delivering today/tomorrow (already done by `ds`) and show it as a sub-reason, never the headline alone. Mixed box/bunch OPLs stay draft until the whole `mix_group` is allocated (doc 03 B2).

**S6 refusals (evidence for code 9), `s_refuse.sql`.**
```sql
-- S6 refused/attempted packing calls per team in the last 30 min. A refusal = 'Farm Pack List Packing Error' written within 5 s of a 'Packing payload' by the same user.
SELECT o.team, COUNT(*) attempts, SUM(e2.name IS NOT NULL) refusals,
  SUM(e2.error LIKE 'Schedule order for team%%') gate_order, SUM(e2.error LIKE 'Over-pack blocked%%') over_pack,
  SUM(e2.error LIKE '%%has not been graded%%') not_graded, SUM(e2.error LIKE 'All scanned bunches have already%%') dup_scan,
  MIN(IF(e2.name IS NOT NULL, e1.creation, NULL)) first_refusal, MAX(e1.creation) last_attempt
FROM `tabError Log` e1
LEFT JOIN `tabError Log` e2 ON e2.method = 'Farm Pack List Packing Error' AND e2.owner = e1.owner
     AND e2.creation BETWEEN e1.creation AND DATE_ADD(e1.creation, INTERVAL 5 SECOND)
JOIN `tabOrder Pick List` o ON o.name = REGEXP_REPLACE(e1.error, '(?s)^.*custom_order_pick_list.: .([^'']+).*$', '\\1')
WHERE e1.method = 'Packing payload' AND e1.creation >= %(from_ts)s
GROUP BY o.team
```
Sub-types by `error` text: `gate_order` (schedule gate, only if `allow_pack_out_of_sequence=0`), `over_pack`, `not_graded` (grading scan missing - grader/Quality), `dup_scan`; silent issued-cap refusals = payload rows with no matching Version and no Packing Error within 60 s (compute in the API layer). Local: 62 payload rows, 0 refusals (extraction regexp validated on the payload text).

**S1/S2 events and live idle clock, `s_events.sql`** (wrap `idle_min_net_of_lunch` in `GREATEST(0, ...)` in production; the local run used a synthetic `now_ts` earlier than the data):
```sql
-- S1/S2 scan events per team and last-scan/idle clock. Params: from_ts (start of frame), now_ts, lunch_s, lunch_e (datetimes today)
WITH ev AS (
  SELECT f.order_pick_list opl, f.creation ts, f.owner usr, 'first_scan' kind
  FROM `tabFarm Pack List` f WHERE f.creation >= %(from_ts)s
  UNION ALL
  SELECT f.order_pick_list, v.creation, v.owner, 'scan'
  FROM `tabVersion` v JOIN `tabFarm Pack List` f ON f.name = v.docname
  WHERE v.ref_doctype = 'Farm Pack List' AND v.creation >= %(from_ts)s AND f.modified >= %(from_ts)s)
SELECT IFNULL(o.team,'(no team)') team, COUNT(*) events, COUNT(DISTINCT ev.usr) users,
  MIN(ev.ts) first_scan, MAX(ev.ts) last_scan,
  TIMESTAMPDIFF(MINUTE, MAX(ev.ts), %(now_ts)s)
   - GREATEST(0, TIMESTAMPDIFF(MINUTE, GREATEST(MAX(ev.ts), %(lunch_s)s), LEAST(%(now_ts)s, %(lunch_e)s))) AS idle_min_net_of_lunch
FROM ev JOIN `tabOrder Pick List` o ON o.name = ev.opl
GROUP BY o.team ORDER BY o.team
```
**S9 scan_mode auto-detect, `s_mode.sql`:**
```sql
-- scan_mode auto-detect per team over a look-back (bulk = list keyed and submitted in one burst)
SELECT o.team, COUNT(*) fpls,
  ROUND(100*AVG(TIMESTAMPDIFF(SECOND, f.creation, x.last_ts) <= 180),0) pct_burst_le_3min,
  ROUND(AVG(x.n_saves),1) avg_saves,
  IF(AVG(TIMESTAMPDIFF(SECOND, f.creation, x.last_ts) <= 180) >= 0.7 AND AVG(x.n_saves) <= 3, 'bulk', 'scan') AS scan_mode
FROM `tabFarm Pack List` f
JOIN `tabOrder Pick List` o ON o.name = f.order_pick_list
JOIN (SELECT f2.name, GREATEST(f2.creation, IFNULL(MAX(v.creation), f2.creation)) last_ts, COUNT(v.name) n_saves
      FROM `tabFarm Pack List` f2 LEFT JOIN `tabVersion` v ON v.ref_doctype='Farm Pack List' AND v.docname=f2.name
      WHERE f2.docstatus=1 AND f2.creation >= DATE_SUB(CURDATE(), INTERVAL %(days)s DAY) GROUP BY f2.name) x ON x.name = f.name
GROUP BY o.team
```
**S10 shift/lunch calibration and S11 gap distribution (run once on prod to set `lunch`, `scan_grace`, thresholds):**
```sql
-- lunch / shift calibration: issue events per half hour (run on prod over 14 days; the dip = lunch, first/last = shift)
SELECT TIME_FORMAT(SEC_TO_TIME(FLOOR(TIME_TO_SEC(TIME(issued_at))/1800)*1800),'%%H:%%i') slot, COUNT(*) n
FROM `tabPick List Item` WHERE issued_at >= DATE_SUB(CURDATE(), INTERVAL 14 DAY) GROUP BY slot ORDER BY slot

-- calibrate GRACE / thresholds: distribution of gaps between consecutive scan events per team (run on prod, 14-30 days)
WITH ev AS (
  SELECT o.team, f.creation ts FROM `tabFarm Pack List` f JOIN `tabOrder Pick List` o ON o.name=f.order_pick_list WHERE f.creation >= %(from_ts)s
  UNION ALL
  SELECT o.team, v.creation FROM `tabVersion` v JOIN `tabFarm Pack List` f ON f.name=v.docname JOIN `tabOrder Pick List` o ON o.name=f.order_pick_list
  WHERE v.ref_doctype='Farm Pack List' AND v.creation >= %(from_ts)s),
g AS (SELECT team, ts, TIMESTAMPDIFF(SECOND, LAG(ts) OVER (PARTITION BY team, DATE(DATE_SUB(ts, INTERVAL 6 HOUR)) ORDER BY ts), ts) gap_s FROM ev)
SELECT team, COUNT(gap_s) gaps,
  ROUND(AVG(gap_s)/60,1) mean_min,
  SUM(gap_s > 300)  gaps_gt5, SUM(gap_s > 600) gaps_gt10, SUM(gap_s > 1800) gaps_gt30, SUM(gap_s > 3600) gaps_gt60,
  ROUND(MAX(gap_s)/60) max_min
FROM g WHERE gap_s IS NOT NULL GROUP BY team
```
Local gap distribution (Sept test data): Bravo mean 0.1 min, max 8 min; Team B mean 0.1 min, 1 gap >10 min; Team A 14 gaps >10 min, 2 >60 min. So `scan_grace`=5 min covers continuous scanning; tune with prod percentiles (p95 of intra-session gaps) before fixing amber at 30.

### 2.4 Per-reason detection notes, age and owner lookup (what the panel's `reason_detail` fills)
* **0** `W(T)=∅`. Context: `unassigned` = OPLs with `team IS NULL` (local: 4 on 10-10; doc 03 B13: unpackable) and SO lines with no OPL at all (doc 03 B1 SQL) - work exists but no team owns it. Age: since `shift_start` (no cause record).
* **1** hidden = no row in `Packhouse Schedule Order` for `schedule_date=delivery-1`, not own-farm, `show_unscheduled_in_packing=0`. Sub-causes: team NULL (auto_schedule skips: `scheduler.py:114`), draft_plain, `auto_remote_transfer_scheduling=0`, no saveDaySchedule run. Detail also lists `ready_stems` (1a) vs `issuable_stems` (1b).
* **2** draft with `opl_submit_blockers` text (call the function for text; SQL gives the class). Age `OPL.modified`.
* **3** per sub-reason table above; owner by sub-reason; "too old" owner = discard approver, because the fix is replace/re-allocate (doc 03 B3).
* **4** moving rows by farm and trip state (`rt_logistics._trip_state`; doc 07 s4.1-4.5): awaiting (no truck), trolley, on truck/in transit, arrived-not-shelved (code 4 because `shelved=0`). Age from the oldest row stamp.
* **5** issuing pending: `issuable_stems` per OPL; owner = people who issued for this team today (`issued_by` recent) else Packing Supervisor. The age is the stall length: now - last `issued_at` of the team's OPLs. Rate hint: issue pace per hour (doc 03 B10 queries).
* **6** staffing: R, ready minutes (`R/packrate`), `ready_since`, count of unstarted OPLs ("ready but unstarted": FPL absent), FPL.owner of the last scan. Never raised for bulk teams on scan silence (E1).
* **7** `not_found=1`, `short_accepted=0`, nothing issuable/moving; also open Bucket Watch (doc 06). Resolution: replacement, `completeOplShort`, `pack_while_waiting_replacement` (needs issued% >= gate, no moving rows, `_can_pack_waiting` `api.py:3419`).
* **8** `Packing Reject Log.status='Pending Approval'` (doc 02 s3.3), age since creation, approvers = `APPROVER_ROLES`.
* **9** S6 counts by sub-type in the last 30 min, `first_refusal` = start of streak.
* **10** `done`: list the downstream queues (Box Label printed not precooled/staged: doc 01 s3 S5-S8) as an informational link, not idle minutes.

---------------------------------------------------------------------------------------------------
## 3. "Minutes idle" accounting: packing / waiting-by-reason / unexplained, per team per day

### 3.1 Partition of the working window
Window `[S, E)` per team-day (clip to the day frame):
```
S = GREATEST(day_open, LEAST(first_issued_at of the team's work set, first scan))     rounded down to a tick
E = LEAST(frame_end, GREATEST(last scan + scan_grace, day_end))     (day_end..E only while overtime scanning continues)
```
Cut into 5-minute ticks; every tick lands in exactly ONE bucket (so bars always sum to the window - verified by the prototype invariant):
1. `break` if the tick starts inside `lunch`.
2. `packing` if a scan event lies in `[tick_start - scan_grace, tick_end)` (scan mode). Bulk mode: `packing` = ticks with R>0 and no refusal (physically presumed working; the KPI for bulk teams is *ready-pool age*, not minutes).
3. else the team's code at `tick_end` from the tree (live tree, or as-of reconstruction when backfilling), with `ready_grace`: a code-6 tick is demoted to 5 if the pool did not already exist at `tick_end - ready_grace`.
   Buckets: `w0 no_work, w1 unscheduled, w2 draft, w3 stock, w4 transfer, w5 issuing, w7 not_found, w8 reject, w9 refusals` = waiting-with-reason (system-attributed); `x6 ready_not_packing` = team-attributed idle; `done` = packed_out remaining (excluded from the window if it is the rest of the day); `unknown` = tree could not classify (no OPL rows/data gap, or bulk team with R>0 and no entry for > `bulk_red`).
4. **Unexplained idle** = `x6` + `unknown`. `x6` is by construction "ready stems existed, nobody scanned": the only team-attributable bucket.

### 3.2 Two ways to produce the ticks
* **Live (accurate going forward):** a `*/5` job (next to `rank_schedules`/`auto_transfer`, `hooks.py:296`) runs S3/S4 and inserts one row per team into `Packing Visibility Tick(team, tick_ts, pday, code, scan_mode, packing, brk, r_stems, ready_min, n_open, hidden_stems, approx=0)`. This is the only way to know historical codes 1-3 exactly, because allocation/draft/schedule/stock state has no timestamps (the schedule is rebuilt on every save, `saveDaySchedule` `scheduler.py:634`, and rank_schedules deliberately does not touch `modified`, doc 08 s6).
* **Backfill (approximate, `approx=1`):** reconstruct codes by stamps. Exact: `packing` (Version stream), `issued stems(t)` (`issued_at`), `packed stems(t)` (Version stock_qty deltas), `not_found(t)` (`not_found_at`), `rejects(t)` (`Packing Reject Log.creation/approved_at`), remote rows not yet arrived (`arrived_at`), OPL submit time (first Version with `["docstatus",0,1]`, else `modified`). Current-state approximations: schedule membership (hidden), stock quality (bad), draft admin.

### 3.3 Stacked bar SQL from the tick table (production, after the job runs)
```sql
SELECT t.pday, t.team,
  SUM(5) AS window_min,
  SUM(IF(t.brk=1,5,0)) AS break_min,
  SUM(IF(t.packing=1,5,0)) AS packing_min,
  SUM(IF(t.brk=0 AND t.packing=0 AND t.code=5,5,0)) AS w5_issuing,
  SUM(IF(t.brk=0 AND t.packing=0 AND t.code=4,5,0)) AS w4_transfer,
  SUM(IF(t.brk=0 AND t.packing=0 AND t.code=3,5,0)) AS w3_stock,
  SUM(IF(t.brk=0 AND t.packing=0 AND t.code=1,5,0)) AS w1_unscheduled,
  SUM(IF(t.brk=0 AND t.packing=0 AND t.code=2,5,0)) AS w2_draft,
  SUM(IF(t.brk=0 AND t.packing=0 AND t.code=0,5,0)) AS w0_no_work,
  SUM(IF(t.brk=0 AND t.packing=0 AND t.code=7,5,0)) AS w7_not_found,
  SUM(IF(t.brk=0 AND t.packing=0 AND t.code=8,5,0)) AS w8_reject,
  SUM(IF(t.brk=0 AND t.packing=0 AND t.code=9,5,0)) AS w9_refusals,
  SUM(IF(t.brk=0 AND t.packing=0 AND t.code=6,5,0)) AS x6_ready_not_packing,
  SUM(IF(t.brk=0 AND t.packing=0 AND t.code=-1,5,0)) AS unknown
FROM `tabPacking Visibility Tick` t
WHERE t.pday BETWEEN %(from)s AND %(to)s AND t.code <> 10
GROUP BY t.pday, t.team ORDER BY t.pday, t.team
-- hourly time-series for the panel: add  , HOUR(t.tick_ts) AS hr  to SELECT and GROUP BY
```

### 3.4 Prototype (Python, reads only; `idle_minutes.py`; run from `sites/`)
Implements: ticks, lunch, scan sessionisation (`scan_grace`), as-of state from stamps, `ready_grace`, frame clipping, invariant check. Core of the file:
```python
"""Prototype: idle minutes by reason per team-day (read-only). Run from sites/: ../env/bin/python idle_minutes.py <team> <delivery_date> [synthetic]"""
import sys, json, datetime as dt, collections
import frappe
frappe.init(site="kaitet.local", sites_path="."); frappe.connect()
frappe.db.sql("SET SESSION TRANSACTION READ ONLY")
TICK=5; GRACE=5; READY_GRACE=10; GATE=50.0; HUB="Kapkolia"; LUNCH=(dt.time(12,30),dt.time(13,30))
DAY_START=dt.time(8,0); DAY_END=dt.time(17,30)
sql=lambda s,*a: frappe.db.sql(s,a,as_dict=True)

def load(team, dd):
    opls=sql("""SELECT o.name, o.docstatus ds, o.modified, o.creation, so.farm sfarm FROM `tabOrder Pick List` o JOIN `tabSales Order` so ON so.name=o.sales_order
                WHERE o.docstatus<2 AND so.delivery_date=%s AND o.team=%s""",dd,team)
    names=[o.name for o in opls]
    if not names: return opls,{},{},{}
    rows=collections.defaultdict(list)
    for r in sql("""SELECT parent,bucket,stock_qty,issued,issued_offline,issued_at,modified,not_found,not_found_at,farm,arrived_at,awaiting_transfer,in_transit,loaded_in_trolley
                    FROM `tabPick List Item` WHERE parenttype='Order Pick List' AND COALESCE(bucket,'')<>'' AND parent IN %s""",tuple(names)):
        rows[r.parent].append(r)
    sub={}
    for r in sql("""SELECT docname,MIN(creation) t FROM `tabVersion` WHERE ref_doctype='Order Pick List' AND docname IN %s AND data LIKE '%%["docstatus",0,1]%%' GROUP BY docname""",tuple(names)): sub[r.docname]=r.t
    ev=[]   # (ts, opl, delta_stems)
    for r in sql("""SELECT f.order_pick_list opl,f.creation ts FROM `tabFarm Pack List` f WHERE f.order_pick_list IN %s""",tuple(names)): ev.append((r.ts,r.opl,0))
    for r in sql("""SELECT f.order_pick_list opl,v.creation ts,v.data FROM `tabVersion` v JOIN `tabFarm Pack List` f ON f.name=v.docname WHERE v.ref_doctype='Farm Pack List' AND f.order_pick_list IN %s""",tuple(names)):
        d=0
        for ch in json.loads(r.data).get("row_changed",[]):
            for fld,a,b in ch[3]:
                if fld=="stock_qty": d+=float(b or 0)-float(a or 0)
        ev.append((r.ts,r.opl,d))
    return opls,rows,sub,ev

def issued_at(r):  # time the row counted as issued
    return r.issued_at or r.modified
def state_at(t, opls, rows, sub, ev, now):
    """-> (code, detail) by tier tree, as-of t (approximate: schedule/hidden and stock-quality are CURRENT state)"""
    per=[]
    for o in opls:
        if o.creation>t: continue
        submitted_at=sub.get(o.name) or (o.modified if o.ds==1 else None)
        is_sub = o.ds==1 and submitted_at and submitted_at<=t
        stems=iss=nf=mov=0.0
        for r in rows[o.name]:
            q=float(r.stock_qty or 0); stems+=q
            if (r.issued or r.issued_offline) and issued_at(r)<=t: iss+=q
            elif r.not_found and (r.not_found_at or now)<=t: nf+=q
            elif (r.farm and r.farm!=HUB and (r.arrived_at is None or r.arrived_at>t)): mov+=q
        packed=sum(d for ts,op,d in ev if op==o.name and ts<=t)
        per.append(dict(opl=o.name,sub=is_sub,stems=stems,iss=iss,nf=nf,mov=mov,packed=packed,
                        unissued_ok=stems-iss-nf-mov))
    if not per: return 0,{}
    open_=[p for p in per if not (p['packed']>=p['stems']-p['nf']-0.5)]
    if not open_: return 10,{}
    R=sum(max(0,p['iss']-p['packed']) for p in open_ if p['sub'] and p['iss']>=p['stems']*GATE/100 or (p['sub'] and p['unissued_ok']+p['mov']==0 and p['iss']>0))
    if R>0: return 6,dict(R=R)
    if any(not p['sub'] and p['mov']>0 for p in open_) and not any(p['sub'] and p['unissued_ok']>0 for p in open_): return 4,{}
    if any(p['sub'] and p['unissued_ok']>0 for p in open_): return 5,{}
    if any(p['mov']>0 for p in open_): return 4,{}
    if any(p['nf']>0 for p in open_): return 7,{}
    return 2,{}

def run(team, dd, synthetic=False):
    opls,rows,sub,ev=load(team,dd)
    pday=dd-dt.timedelta(days=1); now=dt.datetime.now()
    FLOOR=dt.datetime.combine(pday,dt.time(6,0)); CAP=FLOOR+dt.timedelta(days=1)
    ev=[e for e in ev if FLOOR<=e[0]<CAP]            # events outside the processing-day frame belong to another day
    first_issue=min([issued_at(r) for o in opls for r in rows[o.name] if (r.issued or r.issued_offline) and FLOOR<=issued_at(r)<CAP] or [dt.datetime.combine(pday,DAY_START)])
    first_ev=min([e[0] for e in ev] or [first_issue])
    S=max(FLOOR,min(first_issue,first_ev)).replace(second=0,microsecond=0)
    S=S-dt.timedelta(minutes=S.minute%TICK)
    last_submit=max([e[0] for e in ev] or [S])
    E=min(CAP,max(last_submit+dt.timedelta(minutes=GRACE), dt.datetime.combine(S.date(),DAY_END)))
    ticks=[]; t=S
    while t<E: ticks.append(t); t+=dt.timedelta(minutes=TICK)
    evt=sorted(e[0] for e in ev)
    acc=collections.Counter()
    for t in ticks:
        end=t+dt.timedelta(minutes=TICK)
        if LUNCH[0]<=t.time()<LUNCH[1]: acc['break']+=TICK; continue
        if any(t-dt.timedelta(minutes=GRACE)<=x<end for x in evt): acc['packing']+=TICK; continue
        code,_=state_at(end,opls,rows,sub,ev,now)
        # READY_GRACE: reason 6 only after the ready pool has been there READY_GRACE min
        if code==6:
            c2,_=state_at(end-dt.timedelta(minutes=READY_GRACE),opls,rows,sub,ev,now)
            if c2!=6: code=5
        acc[{0:'w0_no_work',1:'w1_unsched',2:'w2_draft',3:'w3_stock',4:'w4_transfer',5:'w5_issuing',6:'x6_ready_not_packing',7:'w7_not_found',10:'done'}.get(code,'?%s'%code)]+=TICK
    total=sum(acc.values())
    print(team,dd,"window",S,"->",E,"ticks",len(ticks),"events",len(ev))
    for k,v in sorted(acc.items()): print("  %-24s %4d min"%(k,v))
    print("  SUM",total,"== window",len(ticks)*TICK, "OK" if total==len(ticks)*TICK else "MISMATCH")
if __name__=="__main__":
    run(sys.argv[1], dt.date.fromisoformat(sys.argv[2]))
```

### 3.5 What the prototype showed locally
See section 4. Hard requirement from it: snapshot ticks live from day one; backfill is only indicative for codes 1-3.

---------------------------------------------------------------------------------------------------
## 4. Validation on the local DB (and what it cannot prove)

| check | result |
|---|---|
| S3/S4/S5/sub3/lines/events/refuse/mode/gaps/calib execute on MariaDB 11.8 | all run read-only; CTE chain over 96 OPLs / 7 teams for 10-10 returns in under a second |
| Tree on delivery 10-10/10-11 | 7 team rows: Bravo 6, Jamafa 6 (visible ready stems), Eldama A/B + Team A/B code 1 (R_hidden 4,590 / 1,570 / 9,800 / 9,712), teamless -> 3. Behaviour is right for the data (Packhouse Schedule has 1 test doc; scheduler never ran locally, doc 08 s5). It proves the "hidden ready stems" detector, nothing about prod teams |
| Tick partition invariant (sum of buckets = window) | OK on Eldama A / Team A / Team B / Bravo (delivery 10-10: windows 495/440/475/445 min) and on Sept scan data (Team B 09-20: 261-tick window before clipping, 19-tick after) |
| Scan sessionisation | Team B delivery 09-20: 212 scan events in 01:14-01:27 (FPL-00044: 167 Version rows in about 6 min; FPL-00043: 43 in 1 min) -> 20 packing minutes; the evening burst 21:03-21:35 (FPL-00046, 60 saves) outside the clipped day frame is correctly not stitched in |
| Frame clipping | Without clipping a Sept-11 FPL with `modified`=Sept-20 produced a 14,345-minute window; with `[pday 06:00, +24h)` clipping the problem disappears. Confirms: never use FPL.modified, always clip |
| `no_work` | Team B delivery 09-21 (no OPLs): 510 min `w0` + 60 break |
| Reason 5 vs 6 | Oct issuing is real (`issued_at` 08:17-17:19, 2,700 rows) but there are NO scans in October, so after issue every team shows `x6` (e.g. Eldama A 425 of 495 min). This is a data artefact (the local DB has no October FPLs), and itself shows why scan data must be present before reason 6 is shown |
| Lunch calibration | local issuing per half-hour dips at 11:30-14:30 (4-12 events vs 40-60), supporting a 12:30-13:30 default; calibrate on prod with S10 |
| `scan_mode` S9 | Jamafa -> bulk, Bravo -> scan; Team A/B local rows are test data, so config override required |
| Refusal pairing S6 | payload parsing works (62 payload rows grouped by team); 0 refusals locally -> pairing logic untested on real refusals |

**Production data needed to finish validation** (read-only, ~1 hour): (1) S10 on 14 days for lunch/shift; (2) S11 gap percentiles per team, set `scan_grace`/amber/red; (3) S9 per team (confirm Team A/B/Jamafa = bulk, Eldama A/B/Bravo = scan); (4) is `Version` for Farm Pack List retained (Logs To Clear on prod) and how far back; (5) OPL Version coverage for submit time; (6) Production Settings: both `allow_*_out_of_sequence`, `show_unscheduled_in_*`, `auto_remote_transfer_scheduling`, `pack_waiting_replacement_pct`, `transfer_hub_farm`, `discard_age`; (7) share of OPLs with a team and with a schedule row per delivery date (doc 08 A3); (8) number of "Farm Pack List Packing Error" rows per day and top texts (shows whether code 9 is real); (9) packers-per-team (distinct `Version.owner` per team-day) to set team packrate and floaters (E2); (10) a hand-check of 3 known stand-still episodes against the tree.

---------------------------------------------------------------------------------------------------
## 5. Edge cases and handling
| # | case | handling |
|---|---|---|
| E1 | Bulk-entry teams (Team A, Team B, Jamafa): timestamps are data entry, FPL open < 1-3 min | `scan_mode='bulk'`: no scan-silence idle, no `packing` minutes from scans; status `working` while R=0; once R>0 the KPI is **ready-pool age** (oldest issued-not-entered stems, approx `MIN(issued_at)` of OPLs with packed=0): amber 3 h, red 6 h (doc 03 B11 thresholds, to confirm); minutes by reason still computed for R=0 ticks (waiting 1-5,7,8) because those are physical facts; their `x6` becomes `entry_lag`. Panel text says "last list entered HH:MM" |
| E2 | Packers float between teams; workers have no team | Never attribute by worker. Activity belongs to the OPL's team. A floater scanning team B's OPL makes team B active and team A idle - that is operationally true (A has no hands). Optional later: distinct `Version.owner` per team-day shows head-count (staffing signal for code 6) |
| E3 | Several teams on one Sales Order | Team is per OPL; SO-level view lists each OPL with its own team and code. A team code never mixes OPLs of another team |
| E4 | OPL without team | code-0 side panel "unassigned work" (count, stems, owner = OPL owner); never counted in any team's minutes |
| E5 | Pack-while-waiting | Packable at issued% >= gate with the issued cap; R counts only issued stems, so a team that finished the issued 60% is `5` (issuer still working) or `4`/`7` (remainder on trucks/missing), not 6. `pack_issued_now` OPLs (doc 06 2.5) are treated as gate_ok. The `pack_waiting_replacement_pct` field is absent from the local doctype (default 50 used): confirm it exists on prod |
| E6 | Replacement waits (Bucket Replacement, doc 06) | Open replacement that is already re-pointed does not block (no code). Replacement still travelling = its row is awaiting/in transit -> 4. Missing with no replacement -> 7. Packing quality issue gaps (record auto-closed) show as code 2/3 short allocation |
| E7 | Day rollover / night packing | Frame `[pday 06:00, pday+1 06:00)`, pday from the OPL's SO (events inherit); tick job runs through the night; `closed` only after 60 quiet minutes with R=0. Midnight trip-closing job does not affect packing codes except moving rows with no flag (`keep_as_anomaly`, doc 03 B8): those fall to ghost (3) |
| E8 | Delivery-1 processing day, carry-over | Work set = delivery in {today, tomorrow} + older open; a late order from yesterday's delivery that is still open stays in W with `late` flag; the schedule lookup uses `schedule_date = delivery_date - 1` per OPL |
| E9 | Gate on (`allow_pack_out_of_sequence=0`) | head-of-line rule (2.2 note): code from the first not-done sequence; `waits_for` from `_schedule_blockers`; unaffected teams stay independent (teams never block each other, doc 08 s4) |
| E10 | Own-farm (Karen) vs hub (Ravine) | `own_farms` never hidden; `_schedule_blockers` compares only same-location orders (`api.py:3312`); compute W per location if one team spans both |
| E11 | Short-accepted OPL | `short_accepted=1`: not_found rows no longer block; done = packed >= issued (PACK_DONE_SQL third clause) |
| E12 | Test/JTEST rows, `custom_` junk, zero-stem OPLs | filtered (`stems>0`, exclude names starting TEST/JTEST, `so.docstatus=1`, `business_unit='Roses'`) |
| E13 | Reset/rename of team after allocation | Team is written once ("first allocation wins"); schedule team can differ (saveDaySchedule `teams` param). S-check: `OPL.team <> Packhouse Schedule.team` listed as data-quality; the tree uses OPL.team |
| E14 | issued flag without stamp, offline issue | `COALESCE(issued_at, modified)`; `issued_offline=1` counts as issued |
| E15 | Clock/timezone | all site time; frame hours config; DB `NOW()` and python `now` must be Africa/Nairobi |

---------------------------------------------------------------------------------------------------
## 6. API shape: `GET packing_visibility.why_waiting?date=YYYY-MM-DD&team=&since=HH:MM` (Why is the team waiting panel)
```json
{
  "generated_at": "2026-10-25T11:05:00",
  "pday": "2026-10-25", "delivery_date": "2026-10-26",
  "config": {"tick_min": 5, "amber": 30, "red": 60, "lunch": ["12:30","13:30"], "shift": ["08:00","17:30"],
             "gates": {"allow_pack_out_of_sequence": 1, "show_unscheduled_in_packing": 0, "pack_waiting_replacement_pct": 50}},
  "teams": [
    {
      "team": "Eldama A", "scan_mode": "scan",
      "status": "standing",              // working | standing | done | break | not_started | closed
      "severity": "red",                  // green | amber | red | grey
      "attribution": "system",           // system | team | unknown
      "last_scan": "2026-10-25T09:48:12", "scans_today": 412, "scanners_today": 9,
      "idle_since": "2026-10-25T09:53:12", "minutes": 72,
      "primary_reason": {"code": 1, "label": "Orders not scheduled (hidden from packing list)"},
      "reason_since": "2026-10-25T09:40:00", "reason_minutes": 85,
      "reason_detail": {
        "code": 1,
        "opls": 13, "stems": 4200, "ready_stems": 4200, "ready_minutes": 140,
        "oldest": {"opl": "OPL-2026-01411", "sales_order": "SAL-ORD-2026-01234", "customer": "...", "delivery_date": "2026-10-26", "since": "2026-10-25T07:47:00", "age_min": 198},
        "example_opls": ["OPL-2026-01411", "OPL-2026-01412", "OPL-2026-01413"],
        "sub_reasons": [{"key": "hidden_ready", "opls": 13, "stems": 4200}],
        "evidence": {"refusals_30m": 0, "pending_rejects": 0, "not_found_stems": 0},
        "link": "/app/packhouse-scheduler?date=2026-10-26&team=Eldama%20A"
      },
      "reasons": [          // full ranked distribution across the team's open OPLs (never lose the other causes)
        {"code": 1, "label": "...", "opls": 13, "stems": 4200, "age_min": 198},
        {"code": 3, "label": "...", "opls": 33, "stems": 9240, "age_min": 420, "sub_reasons": [
            {"key": "not_on_any_shelf", "stems": 11590}, {"key": "too_old", "stems": 12230}, {"key": "held_discard_listed", "stems": 2740}]}
      ],
      "owner": {"role": "Assistant Packhouse Manager", "person": "abiwott@karenroses.com", "person_source": "OPL.owner",
                "alt": [{"role": "Packing Supervisor", "person": null}]},
      "series": {                     // idle minutes by reason, hourly buckets for the stacked bar
        "bucket_min": 60,
        "points": [
          {"t": "2026-10-25T08:00", "packing": 0,  "break": 0,  "w5_issuing": 40, "w4_transfer": 0, "w3_stock": 0, "w1_unscheduled": 20, "w2_draft": 0,
           "w0_no_work": 0, "w7_not_found": 0, "w8_reject": 0, "w9_refusals": 0, "x6_ready_not_packing": 0, "unknown": 0},
          {"t": "2026-10-25T09:00", "packing": 35, "break": 0,  "w5_issuing": 5,  "w4_transfer": 0, "w3_stock": 0, "w1_unscheduled": 20, "...": 0}
        ],
        "totals": {"window_min": 570, "packing": 210, "break": 60, "waiting_system": 240, "x6_ready_not_packing": 60, "unknown": 0},
        "approx": false              // true when reconstructed from stamps (no tick rows)
      }
    }
  ],
  "unassigned": {"opls": 4, "stems": 840, "oldest_age_min": 310, "owner": {"role": "Assistant Packhouse Manager"}},
  "data_quality": [{"key": "bulk_team_timestamps", "teams": ["Team A","Team B","Jamafa"]}, {"key": "ghost_stock_noise", "stems": 11590}]
}
```
Endpoint implementation notes: S3-S6 for the `teams[]` block (a few ms per team); series from the tick table (3.3) or, when empty, the prototype backfill with `approx:true`; reuse existing owners/roles lookups (doc 03 s3 table) for `owner`; deep links per doc 04 s1.

---------------------------------------------------------------------------------------------------
## 7. Open questions
1. Whether Version rows for Farm Pack List are retained on prod (and for how long) and whether `Packing payload` retention (14 days) is enough for refusal analysis; if not, add a one-line counter on refusal in `createOrUpdateFarmPackList`'s except branch (out of scope here).
2. Real shift/lunch times and whether night packing is routine (local test data shows 01:14-04:21 FPLs).
3. Which people are "team leads" (no field; owner resolver uses FPL.owner) - a `Packing Teams.lead` field would make code 6 actionable.
4. Quarantine/cooling terms for `bad_stems` need `core/stock.bucket_rows` in the API layer; the SQL here covers discard-listed, ghost and too-old only.
5. Whether `pack_waiting_replacement_pct` and `show_unscheduled_in_*` exist on prod exactly as on local (the former is absent from the local doctype JSON).
