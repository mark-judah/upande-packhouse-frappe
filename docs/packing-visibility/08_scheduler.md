# 08 Packhouse scheduler: how it works, settings, data, dashboard SQL

Research only (no edits). Paths relative to apps/upande_packhouse/upande_packhouse unless stated. Local DB is a frozen snapshot (kaitet.local).

## 1. End to end
- Doctype Packhouse Schedule (one doc per team per PROCESSING day = delivery date - 1; name `PSCH-<date>-<team>`, track_changes=1) with child Packhouse Schedule Order (order_pick_list, order_name, customer, sequence). OPL.team is the team (set at sales allocation: sales_allocation.py ~1876-1898 stamps the OPL team, first allocation wins; teams valid only if a Packing Teams record). OPL.schedule_number is a LEGACY global number (setSchedulerOrder, mobile/api.py:5452, mostly 0/NULL); the number the packhouse works to is Packhouse Schedule Order.sequence (mobile/api.py getSchedulerMeta ~4100 says so).
- Who creates it: (a) a person on Remote Transfers > Scheduler (www/packhouse_scheduler.py just redirects to /remote-transfer/scheduler; templates/remote_transfer_v2/scheduler.html) via saveDaySchedule (api/remote_transfer/scheduler.py:633, POST: rebuilds EVERY team's schedule from the ordered list; params date, order, teams); "Distribute to teams" passes {opl: team} for OPLs with no team (only written if OPL.team empty, line ~702); "Distribute to trips" = auto_transfer.distributeTrips (api/auto_transfer.py:300) plans trips in sequence order. (b) automatically: auto_transfer.run (cron */5, hooks.py:309; no-op unless Production Settings.auto_remote_transfer_scheduling) calls _auto_schedule (auto_transfer.py:285) -> scheduler.auto_schedule (scheduler.py:114) for delivery today+1 and today+2: every OPL in getSchedulerFeed with a team and not a "draft_plain" joins its team schedule through saveDaySchedule. Orders with no team ("Unassigned") are left out and reported in `no_team`; transfer planning only sees scheduled orders so their remote buckets get no truck (comment ~line 685).
- Ordering inside a team: _tiers (scheduler.py:24): 0 fully issued; (1,0) submitted OPL; (1,1) draft with all buckets at hub; (2,..) still coming from farms ordered by truck departure (dispatched, scheduled, planned run order, no trip). Every saveDaySchedule re-ranks each team by tier (page order kept within tier); rank_schedules (scheduler.py:160, cron */5, hooks.py:309) re-ranks TODAY's schedules continuously as buckets get issued/arrive. Fully issued orders stay on the schedule (kept rows). Karen-type orders (sales farm != transfer hub; _own_packing_farms) are never scheduled by the page and keep their slots (_keep_own_slots).
- Not-scheduled orders: getSchedulerFeed excludes orders with ANY issued bucket unless already scheduled. In the apps unscheduled OPLs are hidden from issuing/packing/app-scheduler unless show_unscheduled_in_* is on (mobile/api.py _listed_opls:2881; Karen own-farm orders always listed).
- "Followed": there is no stored flag. Enforcement is a server gate per team (see 3); "followed" must be derived from timestamps (queries C1/C2).
- Bypass: ONLY the two Production Settings toggles allow_issue_out_of_sequence / allow_pack_out_of_sequence (global, per kind). No log is written when the gate is bypassed. "Packing Bypass Log" (mobile/api.py createPackingBypass:1623) is something else: unscannable bunch (damaged/missing QR) reasons; do not use it as schedule bypass evidence. Other implicit exemptions: team's first order (sequence<=1), earlier orders at a different stock location (Karen vs Ravine/hub) don't block (_schedule_blockers), PACK_DONE_SQL counts an order done when FPL submitted, or pack-while-waiting draft FPL with not_found rows, or packed >= issued stems.

## 2. The gate and what a packer sees
- Packing: createOrUpdateFarmPackList calls _throw_if_pack_blocked (mobile/api.py:982, called :1020). Message (frappe.throw, title "Packing order"): "Schedule order for team {team}: pack #{n} first. #{n} isn't fully packed yet, so #{seq} can't be packed before it." Packing also has an issued cap (IssuedCapError, _issued_cap_error ~3435): cannot pack more stems than issued so far.
- Issuing: issue scan (mobile/api.py:4898) returns HTTP 409, data.status "schedule_blocked" with team, attempted_schedule_number, start_from, pending_lower_numbers; message "Schedule order for team {team}: issue #{n} first. #{n} isn't fully issued yet, so #{seq} can't be issued before it." ("fully issued" = every bucket not marked not_found issued.)
- Lists: fetchPicklists (packing, ~2985-3110) and issuing list (~3660-3720) return schedule, schedule_team, is_next, waits_for (0 if bypass on). Sort: not-fully-packed first, scheduled before unscheduled, by sequence then team (every team's #1, then #2...). Mobile app (/home/jk/Projects/upande-packhouse/src/tenants/karen/...): stores keep isNext/waitsFor (karen-packing-store.ts:357, karen-issuing-store.ts:205-216) but the screens do not use them: PackingScreen.tsx:252 / IssuingScreen.tsx:136 only show a corner tag "Team #n" ("Not scheduled" on issuing; none on packing; "Not scheduled" on the packing order header :621). There is NO client-side blocking: the packer picks any order, scans, and gets the server error. Packing list only shows orders for delivery_date = today (not processing day) with docstatus=1 OPL. (uncertain: other tenant app variants outside src/tenants/karen not checked.)

## 3. Settings (Production Settings, single, apps/upande_quality/.../production_settings/production_settings.json; track_changes = 0)
Defaults are doctype defaults; "local" is the current local value (all frozen).
| field | default | local | read at | effect |
|---|---|---|---|---|
| allow_issue_out_of_sequence | 0 | 1 | mobile/api.py:_schedule_blockers (~3272, via SEQUENCE_BYPASS:3235), issue list ~3683 | on: no issue ordering (waits_for 0, no 409) and remote loading in any order; off: team issues #1 fully before #2 |
| allow_pack_out_of_sequence | 0 | 1 | _schedule_blockers, fetchPicklists ~3026 | on: pack any order; off: gate error above |
| show_unscheduled_in_issuing / _packing / _scheduler | 0 | not set | _listed_opls via mobile/api.py:3032 (packing), 3686 (issuing), 3942 (app scheduler) | on: unscheduled OPLs listed (after scheduled) |
| auto_remote_transfer_scheduling | 0 | 1 | api/auto_transfer.py:34,enabled() ~60, run(); also transfer_scheduling.py:6446 | cron auto-schedules orders and plans trips; this is the "scheduler is ON" switch for automatic mode |
| auto_transfer_frequency (Select) | 10 | NULL | auto_transfer.py frequency_minutes (default 10) | re-plan interval min (5-60) |
| auto_dispatch_full_trips | 0 | 1 | transfer_scheduling.py:6445 | auto-dispatch full trips (needs auto scheduling on) |
| release_trips_without_confirmation | 0 | not set | transfer_scheduling.py:6375 | trips released without request/confirm |
| allow_hub_shelving_without_transfer | 0 | 1 | transfer_scheduling.py:7604 | shelving of buckets not transferred |
| allow_shelving_in_transit | 0 | 1 | transfer_scheduling.py:7489 | shelve while in transit |
| transfer_hub_farm | - | Kapkolia | scheduler.py:292,457; transfer_scheduling.py:98; offline_issue.py:366 | defines hub; other sales farms = own-packing (Karen), unscheduled by design |
| takt_time (min/box) | 0 | 2.5 | scheduler.py:261 (getSchedulerFeed), mobile/api.py:4077 | scheduler page/app takt threshold display (not enforcement) |
| pack_waiting_replacement_pct | none (50 used) | field NOT in local doctype | mobile/api.py:_pack_waiting_min_pct:3430; server_scripts/pack_while_waiting_replacement.py:18 | min % of stems issued before an order may be packed while waiting for replacement buckets (lets next team order proceed); field is absent from production_settings.json locally (uncertain: maybe a prod Custom Field/DB-only script) |
| discard_age 5 / amber_time 3 | - | 5 / 3 | page/sales_allocation/sales_allocation.py:62-63; api/v2/core/stock.py:122; coldroom.py:317 | allocation age limits (amber = needs approval, >=discard blocked) |
| max_allocation_age (Shelf Locations child table, per farm; default 5 in code) | - | 4 (Kapkolia) | quality mobile/api.py:6046 etc, v2/core/stock.py:142, sales_allocation.py:2931 | per-farm max stock age at allocation |
| max_shelving_age | - | 1 | shelving code (upande_quality) | shelving refuses older buckets |
| inactive_bucket_days | 4 | n/a | api/v2/rt_inactive.py:46 | bucket journey inactive flag |
| peak hours | NOT FOUND | | | no peak-hours setting exists in Production Settings or Sales Settings (Sales Settings has only default_warehouse) |
Key point: locally BOTH out-of-sequence toggles are 1 (gate OFF); production may differ. Note the labels' defaults are 0 = enforced, so on a fresh site the gate is ON.

## 4. Interaction with transfers, issuing, speed
- Issue gate counts every bucket not not_found: an order with a bucket awaiting_transfer/in transit is not fully issued, so #2 cannot be issued (and #3 can't be packed) until #1's remote buckets arrive, are shelved and issued. Head-of-line blocking is real when gate is on. Mitigations in code: _tiers puts submitted/at-hub orders before ones waiting for trucks and re-ranks every 5 min; per-location blockers (Karen vs hub); not_found buckets don't block; pack-while-waiting (pct setting) and "packed >= issued stems" count as done; issued cap prevents packing beyond issued.
- Residual risk (inference, uncertain): re-ranking changes sequence numbers while a team works (an order mid-pack can slide back); a stuck bucket (lost/never found, not marked not_found) idles the whole team behind it; orders with no team never get trucks.
- Transfer planning uses sequence (distribute "in sequence order"); unscheduled orders get no trips.

## 5. Local data (frozen snapshot, read-only SELECTs)
- Packhouse Schedule: 1 doc (PSCH-2026-09-17-Team A, 2 orders OPL-2026-00039 seq1 / 00038 seq2, TEST-REPRO-174, saved by judah@upande.com 2026-09-17 17:42). Test data. Nothing since.
- OPLs (docstatus<2): 1196; team set on 1177 (19 NULL); teams Eldama A 612, Team A 122, Eldama B 121, Bravo 111, Jamafa 105, Team B 104, Eldama 2. schedule_number: 1 OPL=1, rest NULL (legacy field unused). Delivery 2026-10-05..10-25: ~26-139 OPLs/day, 0 on any schedule (auto/manual schedule effectively never ran locally, though auto_remote_transfer_scheduling=1 in settings).
- Production Settings Version rows: only 2 (2026-08-25 by dev@upande.com: initial amber/discard/takt/shelf_locations). The doctype has track_changes=0, so toggle changes are NOT recorded in Version; no history of the out-of-sequence toggles exists locally (and likely none in prod unless track_changes is on there).
- Farm Pack List: 17 rows (2026-09-10..09-25); Packing Bypass Log: 4 rows (2026-09-20, damaged/missing QR). Packhouse Schedule Version rows: 1.

## 6. When was it on/off, how to tell (design notes)
- "Gate ON" now = both allow_* = 0. History of toggles is only available if Version tracking exists; otherwise use proxies: (a) existence/saves of Packhouse Schedule per date (A2; modified_by = Administrator/system user = auto; person = manual), (b) share of OPLs with a schedule row (A3), (c) behaviour: share of out-of-sequence starts (C1/C2) is ~0 only while the gate is on. Recommendation: have the dashboard snapshot the two toggles + counts daily into its own table, or enable track_changes on Production Settings (a change outside this research scope).
- Packhouse Schedule has track_changes, but saveDaySchedule deletes and re-adds all orders on every save, so Version "added/removed/row_changed" shows reorder events; first creation = `creation`, last save = `modified` (rank_schedules uses update_modified=False, so it does NOT touch modified).
- Caveat: Farm Pack List.creation is the first scan of the order, FPL docstatus=1 `modified` is approximately submit time (uncertain: modified moves on later edits; use the Submit Version/Comment if exact time needed). Pick List Item.issued_at/arrived_at/shelved_at exist per bucket.

## Dashboard queries (read-only, all validated to execute on local MariaDB 11.8; local data is mostly empty so results unverified)

### A1. Scheduler settings now
```sql
SELECT field, value FROM tabSingles WHERE doctype='Production Settings' AND field IN
('allow_issue_out_of_sequence','allow_pack_out_of_sequence','show_unscheduled_in_issuing','show_unscheduled_in_packing',
'show_unscheduled_in_scheduler','auto_remote_transfer_scheduling','auto_transfer_frequency','auto_dispatch_full_trips',
'release_trips_without_confirmation','takt_time','transfer_hub_farm','pack_waiting_replacement_pct','discard_age','amber_time','inactive_bucket_days','allow_shelving_in_transit','allow_hub_shelving_without_transfer','max_shelving_age')
```

### A2. Is the scheduler being used: Packhouse Schedule saves per processing day (schedule_date = delivery_date - 1)
```sql
SELECT p.schedule_date, COUNT(DISTINCT p.name) teams, COUNT(o.name) orders, MIN(p.creation) first_created,
 MAX(p.modified) last_saved, GROUP_CONCAT(DISTINCT p.modified_by) saved_by
FROM `tabPackhouse Schedule` p LEFT JOIN `tabPackhouse Schedule Order` o ON o.parent=p.name
WHERE p.schedule_date >= CURDATE() - INTERVAL 30 DAY GROUP BY p.schedule_date ORDER BY p.schedule_date DESC
```

### A3. Share of OPLs on a schedule per delivery date (ps.schedule_date is delivery-1; the per-OPL EXISTS ignores date, fine because an OPL sits on one day)
```sql
SELECT so.delivery_date, COUNT(*) opls,
 SUM(EXISTS(SELECT 1 FROM `tabPackhouse Schedule Order` pso JOIN `tabPackhouse Schedule` ps ON ps.name=pso.parent WHERE pso.order_pick_list=opl.name)) scheduled,
 ROUND(100*SUM(EXISTS(SELECT 1 FROM `tabPackhouse Schedule Order` pso JOIN `tabPackhouse Schedule` ps ON ps.name=pso.parent WHERE pso.order_pick_list=opl.name))/COUNT(*),1) pct_scheduled,
 SUM(IFNULL(opl.team,'')='') no_team
FROM `tabOrder Pick List` opl JOIN `tabSales Order` so ON so.name=opl.sales_order
WHERE opl.docstatus<2 AND so.delivery_date >= CURDATE() - INTERVAL 30 DAY GROUP BY so.delivery_date ORDER BY 1 DESC
```

### B1. Settings change history (UNRELIABLE, see notes)
```sql
SELECT creation, owner, data FROM tabVersion WHERE ref_doctype='Production Settings' AND docname='Production Settings'
 AND (data LIKE '%out_of_sequence%' OR data LIKE '%show_unscheduled%' OR data LIKE '%auto_remote_transfer%') ORDER BY creation DESC
```

### C1. Per team-day Kendall tau between schedule sequence and order in which packing STARTED (first Farm Pack List.creation); 1 = perfectly in sequence
```sql
SELECT sched, schedule_date, team, n_orders,
 SUM(inv) discordant, SUM(conc) concordant,
 ROUND((SUM(conc)-SUM(inv))/NULLIF(SUM(conc)+SUM(inv),0),3) kendall_tau
FROM (
 SELECT a.sched, a.schedule_date, a.team, a.n_orders,
  (a.seq<b.seq AND a.t_start<b.t_start) OR (a.seq>b.seq AND a.t_start>b.t_start) conc,
  (a.seq<b.seq AND a.t_start>b.t_start) OR (a.seq>b.seq AND a.t_start<b.t_start) inv
 FROM (SELECT ps.name sched, ps.schedule_date, ps.team, pso.order_pick_list opl, pso.sequence seq, MIN(f.creation) t_start,
        COUNT(*) OVER (PARTITION BY ps.name) n_orders
       FROM `tabPackhouse Schedule` ps JOIN `tabPackhouse Schedule Order` pso ON pso.parent=ps.name
       JOIN `tabFarm Pack List` f ON f.order_pick_list=pso.order_pick_list AND f.docstatus<2
       GROUP BY ps.name, pso.order_pick_list, pso.sequence) a
 JOIN (SELECT ps.name sched, pso.order_pick_list opl, pso.sequence seq, MIN(f.creation) t_start
       FROM `tabPackhouse Schedule` ps JOIN `tabPackhouse Schedule Order` pso ON pso.parent=ps.name
       JOIN `tabFarm Pack List` f ON f.order_pick_list=pso.order_pick_list AND f.docstatus<2
       GROUP BY ps.name, pso.order_pick_list, pso.sequence) b ON b.sched=a.sched AND a.seq<b.seq
) x GROUP BY sched, schedule_date, team, n_orders
```

### C2. Out-of-sequence cases: orders whose packing started while an earlier-sequence order of the same team was not yet submitted (column lists those earlier sequence numbers)
```sql
SELECT ps.schedule_date, ps.team, cur.sequence seq, cur.order_pick_list opl, cur.t_start started,
 (SELECT GROUP_CONCAT(e.sequence ORDER BY e.sequence) FROM (
   SELECT pso.parent, pso.sequence, pso.order_pick_list, MAX(CASE WHEN f.docstatus=1 THEN f.modified END) t_done
   FROM `tabPackhouse Schedule Order` pso LEFT JOIN `tabFarm Pack List` f ON f.order_pick_list=pso.order_pick_list AND f.docstatus=1
   GROUP BY pso.parent, pso.sequence, pso.order_pick_list) e
  WHERE e.parent=cur.parent AND e.sequence<cur.sequence AND (e.t_done IS NULL OR e.t_done>cur.t_start)) earlier_not_done_at_start
FROM (SELECT pso.parent, pso.sequence, pso.order_pick_list, MIN(f.creation) t_start
      FROM `tabPackhouse Schedule Order` pso JOIN `tabFarm Pack List` f ON f.order_pick_list=pso.order_pick_list AND f.docstatus<2
      GROUP BY pso.parent, pso.sequence, pso.order_pick_list) cur
JOIN `tabPackhouse Schedule` ps ON ps.name=cur.parent
HAVING earlier_not_done_at_start IS NOT NULL ORDER BY ps.schedule_date DESC, ps.team, seq
```

### D1. Head-of-line / idle: per consecutive pair in a team schedule, minutes the team waited for the next order to be fully issued after finishing the previous, and minutes it then sat ready before starting (negative minutes_ready_not_started = started before fully issued, i.e. pack-while-waiting)
```sql
SELECT ps.schedule_date, ps.team, n.sequence seq, n.order_pick_list opl,
 prev.t_done prev_done, rdy.t_ready next_fully_issued, st.t_start next_started,
 TIMESTAMPDIFF(MINUTE, prev.t_done, GREATEST(IFNULL(rdy.t_ready,prev.t_done), prev.t_done)) minutes_blocked_on_stock,
 TIMESTAMPDIFF(MINUTE, GREATEST(IFNULL(rdy.t_ready,prev.t_done), prev.t_done), st.t_start) minutes_ready_not_started
FROM `tabPackhouse Schedule Order` n JOIN `tabPackhouse Schedule` ps ON ps.name=n.parent
JOIN (SELECT pso.parent, pso.sequence, MAX(f.modified) t_done FROM `tabPackhouse Schedule Order` pso
      JOIN `tabFarm Pack List` f ON f.order_pick_list=pso.order_pick_list AND f.docstatus=1 GROUP BY pso.parent, pso.sequence) prev
  ON prev.parent=n.parent AND prev.sequence=n.sequence-1
LEFT JOIN (SELECT parent opl, MAX(issued_at) t_ready FROM `tabPick List Item`
      WHERE parenttype='Order Pick List' AND IFNULL(bucket,'')!='' AND IFNULL(not_found,0)=0 GROUP BY parent) rdy ON rdy.opl=n.order_pick_list
LEFT JOIN (SELECT order_pick_list opl, MIN(creation) t_start FROM `tabFarm Pack List` WHERE docstatus<2 GROUP BY order_pick_list) st ON st.opl=n.order_pick_list
```

### D2. Right-now blockers: per scheduled order unissued buckets and rows waiting on transfer (the exact things that block the next order)
```sql
SELECT ps.schedule_date, ps.team, o.sequence, o.order_pick_list,
 SUM(IFNULL(pli.not_found,0)=0 AND IFNULL(pli.issued,0)=0) unissued_buckets,
 SUM(IFNULL(pli.awaiting_transfer,0)=1 OR IFNULL(pli.loaded_in_trolley,0)=1 OR IFNULL(pli.in_transit,0)=1) rows_waiting_transfer
FROM `tabPackhouse Schedule Order` o JOIN `tabPackhouse Schedule` ps ON ps.name=o.parent
JOIN `tabOrder Pick List` opl ON opl.name=o.order_pick_list AND opl.docstatus<2
LEFT JOIN `tabPick List Item` pli ON pli.parent=o.order_pick_list AND pli.parenttype='Order Pick List' AND IFNULL(pli.bucket,'')!=''
GROUP BY ps.schedule_date, ps.team, o.sequence, o.order_pick_list ORDER BY 1 DESC,2,3
```

### E1. Packing Bypass Log (unscannable bunches, NOT a schedule bypass)
```sql
SELECT DATE(creation) d, reason, COUNT(*) logs, SUM(bunches) bunches FROM `tabPacking Bypass Log` GROUP BY 1,2 ORDER BY 1 DESC
```
