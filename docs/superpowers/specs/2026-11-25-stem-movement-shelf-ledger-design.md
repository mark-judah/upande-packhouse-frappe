# Stem Movement v2: the shelf ledger page (design spec)

Status: draft for review. Nothing here is built. Written 2026-11-25.

## 1. Why

Management reads one thing on Stem Movement: the shelf ledger. They want to open the page and see, for any day, the stems on the shelves at the start of the morning, every way stems came onto the shelves, every way they left, and the stems on the shelves at the end, with nothing left unexplained. Today the page is a pipeline funnel (harvested to dispatched) with a small ledger bolted on. The ledger is not trustworthy, and the data it reads cannot be made trustworthy by changing the page alone.

## 2. What we found (evidence)

All numbers are from the local copy of kaitet (data to about 12 Nov) unless marked otherwise. They show the shape of the problem; re-run them on live before quoting them.

### 2.1 The Shelving Log is not a ledger

The current ledger (`api/v2/stem_movement.py: _shelf_ledger`) is built from `Shelving Log` only: opening = rows shelved before the day and not removed before it; in = rows shelved; out = rows removed, split by reason. It balances arithmetically (opening + in - out = closing) because it is its own source. It does not agree with the shelf:

- Live Shelf Item: 3,248 rows, 478,479 stems. Open log rows (`removed_on` empty): 7,216 rows, 991,408 stems. Gap: 512,929 stems.
- 5,553 open log rows (748,891 stems) have no Shelf Item any more. The Shelf Item was deleted by a path that never closed the log. 5,044 of them (690k stems) have no later closed row for that bucket and variety: real exits that were never logged.
- 1,585 live Shelf Items (236,682 stems) have a log row that is already closed. Either stems were re-shelved on the same Shelf Item without a new log row, or the Shelf Item is stale.
- A partial issue lowers the Shelf Item quantity but not the log quantity, so the final "Issued to Sales Order" row overstates what left (18 live items already show log qty above live qty, 1,390 against 670).
- 992 log rows have a removal but no `shelved_on` (741 "Issued to Sales Order" with qty 0 and 251 "Shelf Cleared" with 47,820 stems). 605 bucket and variety pairs have more than one open row.

### 2.2 Exits that leave no log row (silent exits)

From the code inventory (`Q` = upande_quality/mobile/api.py, `P` = upande_packhouse/mobile/api.py):

| Silent exit | Where |
|---|---|
| Partial issue draw: Shelf Item `stem_qty` reduced, log untouched | P:6386-6392 |
| Discard consume: all Shelf Items deleted, SE type Discard, no log | Q:905-925 |
| Reshelve self-heal: stale row on another shelf deleted, log left open | P:7123-7129, Q:2553-2567 |
| Stem replacement donor draw, bunch move source decrement, packing-reject partial donor draw: only `stem_qty` reduced | Q:9188, 9411, 8094, 8185; packing_reject.py:487 |
| Bucket reuse cleanup: Shelf Item deleted, log not closed | upande_agriculture mobile/api.py:230 |
| `_clear_shelf_item` when no "Shelved" log exists: row deleted, nothing closed or created | sales_allocation_v2.py:4647 |
| Merge duplicates: dropped rows' logs hard-deleted | merge_duplicate_shelf_items.py:50-56 |

Also wrong or misleading: `_restore_farm_shelf` flips an old trolley-exit row back to "Shelved" (erasing the exit and the return from history); `transferBucket` writes a "Shelved" row at the destination, so any sum over reason "Shelved" counts a shelf move as new stock; and the exit log row always carries the original shelving quantity.

### 2.3 Offline: three different things share the word

1. **Offline packing on Rosenta** (the daily case). The ERP never sees the issue. The shelf is later cleared and the stems that are still physically there are reshelved. The difference between what was cleared and what was reshelved is what was used offline. Evidence: 80,126 "Shelf Cleared" rows (10.1M stems), 46% reshelved for the same bucket and variety within 24 hours (median gap 11.9 h); reshelved quantity exceeds cleared by 0.74M stems, 26,757 pairs equal, 9,714 reshelved greater, 111 less. This is not yet netted per bucket, so treat it as indicative. Most "Shelf Cleared" rows come from a Server Script that exists only in the database (`clearShelfItem`, role Shelf Clearer), not in the repo.
2. **Offline issuing done online** (sales). `issue_offline` / `mark_issued_offline` swap the allocation to the scanned bucket and issue it through the normal issue path. The shelf exit is logged as "Issued to Sales Order"; the only trace is `Pick List Item.issued_offline = 1` (316 rows). The log cannot tell it from a normal issue.
3. **Shelving Log reason "Offline Issuing"** is a different thing: the farm app's "report bucket removed from shelf" action with a free-text reason. 15,082 log rows carry it but only 69 Stock Entries of type Offline Issuing exist (12,930 stems). The reason is only in a Comment, and a stock entry is written only when the text matches a loss pattern.

No event time exists for any of these. `removed_on` and the Stock Entry time are the server clock when the call arrives, so sync lag cannot be measured and an offline action synced the next morning lands on the sync day.

### 2.4 There is no history of the shelf

Shelf Item is current state only. No past day's opening stock can be read from anywhere; it can only be inferred from the log, which section 2.1 shows is wrong.

## 3. Goals, non-goals, anti-goals

Goals
- For any day and any farm / variety / length filter: opening, every entry, every exit by named reason, closing, and an explicit "unexplained" line that is zero or red.
- Closing equals the live shelf for today (from the same `core/stock.bucket_rows` that Stock Visibility and Avails use), and is broken down by the same `stock_truth` states.
- Offline and offline-recorded movements are visible as their own lines, with the inferred offline consumption from cleared-then-reshelved.
- Every number drills down to buckets and then to the Bucket Journey.

Non-goals
- Rebuilding history before go-live. Earlier days show an "approximate, from the Shelving Log" banner.
- Changing the allocation or issue logic, or discard rules.
- Replacing Stock Take (that reconciles a physical count; this reconciles movements).

Anti-goals (would make this a failure even if it runs)
- A ledger that balances because it is its own source. Balance must be checked against something independent (the live shelf and the nightly control total).
- Hiding unexplained stems in "other".
- Two definitions of available or free stock. Opening, closing and the state split reuse `core/stock.py` and `stock_truth.py`.

## 4. Approach: make the Shelving Log the ledger

Decision (user, 2026-11-25): no new ledger doctype and no snapshots. The Shelving Log already records every shelving and every removal with a reason and a time, and Stock Visibility, Bucket Journey and Stem Movement already read it. The work is to make it complete and truthful.

Why it can be the ledger: treat each log row as a lot of stems sitting on a shelf from `shelved_on` to `removed_on`. Opening stock at midnight = the lots open at that moment, with their quantity at that moment. History then comes for free, for every past day, with no snapshot.

What has to be true for that to hold (the rules the writers must follow):
1. **Every exit closes its lot.** Any code that deletes or reduces a Shelf Item closes or splits the matching log row in the same transaction, with a reason. No silent exits (section 8).
2. **A partial draw splits the lot.** When some stems leave and some stay, the existing row is closed at the quantity drawn (reason = the exit) and a new row opens for the stems that remain, `shelved_on` = the time of the draw, reason "Shelved" is replaced by a neutral "Carried" so it does not count as an entry. Then quantities are right at every instant, and the exit row carries what really left.
3. **Every entry is its own row** with an entry reason (Shelved from receiving, Reshelved, Returned from trolley, Arrived by shelf-to-shelf), so entries can be told apart. A shelf move and a return are new rows, never a rewrite of an old row.
4. **Two new fields on the Shelving Log:** `source` (online, offline-recorded, system, maintenance) and `shelf_item` is always filled. No event-time field: the apps cannot send one, so every time is server time and the page says so.
5. **One-off repair** of the existing data: the open rows whose Shelf Item is gone are closed as "Unrecorded exit" with `removed_on` = the last evidence of the bucket (last stock entry or later shelving of the same bucket), and the rows with a Shelf Item but a closed log are reopened or flagged. Past days then show "repaired" banners; the exact day of an old unrecorded exit is approximate.

The independent check without snapshots: for today, the sum of open lots must equal the live shelf (`core/stock.bucket_rows`); the difference is the unexplained line. A tiny nightly control row per farm (open-lot stems, live stems, difference) is stored so drift is visible over time; it is not a snapshot of the shelf.

Rejected: a parallel ledger doctype (a second source of truth that every writer must also feed), full snapshots (they only add what rule 1 and 2 give us), and a Shelf save hook (kept only as a possible later safety net that logs an "unattributed" row if a writer still bypasses the rules).

## 5. The page

Top: date (default today), day boundary at site midnight (see open question 1), filters region / farm / variety / length / rose type (same options as Stock Visibility).

1. **The day in one line.** Opening 00:00, plus In, minus Out, equals Closing, then Live now (for today) and Unexplained. A status chip: "Balanced" or "N stems unexplained" in red. A small hourly strip shows shelf stems through the day.
2. **Stems in**, by type: shelved from receiving; reshelved after a clear (the offline difference lives here); returned from trolley or truck; arrived by shelf-to-shelf; found / corrected.
3. **Stems out**, by type, each with stems, buckets and a link to its buckets:
   - Issued to an order (online)
   - Issued to an order, offline-recorded (from `issued_offline`)
   - Removed from shelf offline by the farm app (with the typed reason; loss stock-entry marked)
   - Shelf cleared: replace or not-found flow, and emergency clear shown separately
   - Used as a replacement bucket
   - Moved on trolley or truck (transfer out)
   - Discarded
   - Rejected (coldroom, packing reject)
   - Stem replacement donor draw, bunch moved
   - Clean-up (reuse anomaly, self-heal, merge)
   - Unexplained
   Shelf-to-shelf moves appear as a pair (out of one shelf, in to another) and net to zero for the region.
4. **Offline lens.** For the window: stems cleared, stems reshelved within 24 hours for the same bucket and variety, inferred offline consumption = cleared minus reshelved (per bucket, then summed), clearly labelled inferred. Plus offline-recorded issues and late-synced entries once event time exists.
5. **Closing stock by state**, using the same `stock_truth` states (available, cooling, committed, withheld and each withheld reason). It must sum to Closing.
6. **By shelf and by farm** tables with opening, in, out, closing per shelf; row click opens the bucket list; bucket click opens the Bucket Journey.
7. **Date range view:** a per-day matrix of opening, in, out, closing; each day's opening equals the previous day's closing (a visible check).
8. The existing pipeline funnel (harvested to dispatched) stays, moved below, since it is secondary.

## 6. Data model

No new doctype. Changes to `Shelving Log`:
- New field `source` (Select: Online, Offline recorded, System, Maintenance), default Online, and `detail` (Small Text) for the typed reason of a farm-app removal.
- New reasons (Select): entries "Reshelved", "Returned from trolley/truck", "Arrived (shelf-to-shelf)", "Carried" (remainder after a partial draw, never counted as an entry); exits "Unrecorded exit", "Discarded", "Rejected", "Stem replacement", "Bunch moved", "Clean-up". Existing reasons stay valid. Exact match on reasons everywhere (the journey code that matches substrings such as "Transferred", "Issue" or "Sales" is fragile and must change).
- `shelf_item` always set; `stem_qty` is the quantity of this lot, never edited after the row is closed.
- Index on (shelved_on), (removed_on), (farm, removed_on) so a day's opening and flows are cheap.
- A small `Shelf Control Total` table, one row per farm per night: open-lot stems, live stems, difference.

One helper, `shelving_log.close(...)`, `shelving_log.split(...)` and `shelving_log.open(...)`, is the only way writers change the log, so the rules in section 4 live in one place.

## 7. How it ties to stock truth

- Closing for today is `core/stock.bucket_rows` (the same rows Stock Visibility and Avails use) summed after the filters, and the state split is `stock_truth.classify_shelf_row`. No second definition.
- Opening for any day is the open lots at that day's midnight, from the log.
- Invariants checked on every load and by a daily job, each shown on the page when it fails:
  1. opening + in - out = closing (expected)
  2. for today, open lots = live shelf; the difference is "unexplained"
  3. closing by state sums to closing
  4. the sum of each section equals its heading
  5. each day's opening equals the previous day's closing
  6. shelf-to-shelf moves net to zero across the region
- The daily job writes the control total and alerts when unexplained exceeds a threshold, with the top buckets.

## 8. Fixing the writers (worklist)

Convert in this order, biggest unexplained stems first. Each gets a replay test, and the repair in section 4 runs once after the first batch.
1. Partial issue draw (P:6386): split the lot; the exit row carries the stems that left.
2. Discard consume (Q:905-925): close the lot as "Discarded".
3. `clearShelfItem` Server Script (database only): move it into the repo; close the lot, or insert a lot with a real `shelved_on` when none exists (never a null `shelved_on`).
4. Reshelve self-heal (P:7123, Q:2553) and reuse cleanup (agriculture:230): close the stale lot as "Clean-up".
5. Stem replacement donor, bunch move, packing-reject partial donor: split the lot.
6. `_restore_farm_shelf`: write a "Returned from trolley/truck" row; stop rewriting the old exit.
7. `transferBucket`: destination row is an arrival, not "Shelved"; the pair nets to zero.
8. Merge duplicates: never hard-delete logs; close and re-open instead.
9. `issue_offline` / `mark_issued_offline`: set `source = Offline recorded` on the exit row.
10. `createOfflineIssuingEntry`: store the typed reason on the row, not only in a Comment.
11. The two copies of `_clear_shelf_item` (v1 and v2): fix both, or remove v1.

## 9. Testing and acceptance

- One replay test per exit path (create bucket, shelve, perform the action, assert the log row, the Shelf Item and the live shelf agree).
- A reconciliation test: after the repair, open lots equal the live shelf per farm on the local data.
- Page acceptance: for three real days per farm, unexplained is zero or every unexplained bucket is listed and traced to a writer in section 8.
- Cross-check against Stock Visibility closing, Avails shelf total and Stock Take expected counts.

## 10. Decisions and open questions

Decided (user, 2026-11-25)
1. Day boundary: midnight, in site time (Nairobi). The database stores UTC, so day edges are computed in site time, not by the DATE() of the stored value.
2. Rosenta offline packing cannot be matched to buckets. The offline lens is therefore inferred only (cleared minus reshelved) and is labelled inferred.
3. The mobile apps cannot send event time. All times are server time; an offline action synced later lands on the sync day and the page says so. The late-sync idea is dropped.
4. No snapshots and no new ledger doctype; the Shelving Log is the ledger.

Open
- Does management want unexplained to hold up the page (red banner) or only report it?
- Which other database-only Server Scripts touch the shelf besides `clearShelfItem`? They must be found and moved into the repo.
- Default farm and region scope (Karen, Kapkolia and the remote farms).
- Repair policy for the 5,000-plus open rows with no Shelf Item: close as "Unrecorded exit" at the last evidence (proposed), or leave open and exclude from history.

## 11. What is left to the builder

Naming of types and labels, table indexes, chart style, the CSV export columns, and the hourly strip, which is drawn from the log's open lots at each hour.
