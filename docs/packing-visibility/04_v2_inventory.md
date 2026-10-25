# Packhouse v2 inventory for the new "Packing Visibility" page

Research only. Paths are relative to `apps/upande_packhouse/upande_packhouse/` unless absolute.
Screenshots: `docs/packing-visibility/shots/` (taken on kaitet.local:8002, read-only fail-closed routing).

## 1. Existing v2 pages

Sidebar (`packhouse_v2.py:26-74` NAV; optional entries `REQUIRES_APP` at :76):
- Workflow: Orders & Workflow, Allocation Planning, Production, Remote Transfers (`?tab=transfer`)
- Tracking: Cold Room, Discards, Downgrades, Stock Take, Bucket Count
- Stock & Orders: Stock Visibility, Avails, Stem Movement, Order Fulfilment, Biflorica Orders*, Floriday Orders*, Sales Order, Sales Settings (*only if `ecommerce_integration` installed)
- Reference: Variety Tree, Price Lists, New Customer Price List, Specifications

Every page: `www/<x>-v2.html` + `www/<x_>_v2.py` + `api/v2/<module>.py`; state is URL-synced via `PH.state`, so every filter is a deep-link query param.

| Page (route) | Purpose | Main KPIs | Charts / tables | Filters (URL keys) | Poll |
|---|---|---|---|---|---|
| Orders & Workflow `/packhouse-dashboard-v2` (api `orders_workflow.get_orders_workflow`) | Issuing and packing progress per team, every order's documents | Order lines, Stems to deliver, Issued of picked %, Packed of planned %, Boxes packed (complete/planned/staged/loaded), Dispatched %, Needs attention (`www/packhouse-dashboard-v2.html:236-242`) | Stage tabs with counts (All/Not allocated/Issuing/Packing/Packed/Precooling/Staged/Loaded/Dispatched/Invoiced, :7), collapsible team sections with progress bars, per-line row with 6-stage stepper (`.ow-stepper`), takt badge, completion ring, per-box time table; no Chart.js | `from,to,region,rose,team,farm,group,customer,so,dn,si,q,stage,view(team/customer)` (:124) | 60 s (:503) |
| Allocation Planning `/sales-allocation-planning-v2` (wide) | Farm-wise stock vs order lines; partial confirm | Order lines, Ordered stems, Available stock, Uncovered demand, Confirmed (:232-244) | Planner grid, write actions (confirm/unconfirm) | `status,farm,customer,order,variety,length,by,from,to` | 60 s |
| Production `/packhouse-production-v2` | Harvested/received/graded per variety | Stems harvested, received, graded, Top variety, Main stem length (:169-174) | Ranked variety table with length/farm chips, sub-tables | `from,to,region,farm,rose,q` | 60 s |
| Remote Transfers `/remote-transfer-v2?tab=` (wide; `www/remote_transfer_v2.py:20-33`) | 5 tabs: routes, scheduler, transfer, logistics, journey | see below | see below | `date`/`from,to,region` carried across tabs | 60 s on scheduler/transfer |
| - Truck Routes | Today's farms per truck | trips, capacity | route cards/forms | - | - |
| - Scheduler | Sequence teams' orders | Total stems, Scheduled/Not scheduled/Schedulable buckets | planner | `date,region,farm,team,loc,status,q,view` | 60 s |
| - Transfer Scheduling | Distribute across teams, plan trucks, dispatch trips | Trips, Not on a trip, Distributed, Arrived at hub | trip cards, modals | `from,to,region,farm,team,loc,q,view,trip` | yes |
| - Bucket Logistics | Track buckets to the sales farm | Orders, Buckets, At farm, In trolley, In transit, Shelved x/y with bar; Issued, Not found, Replaced, No trip, Avg transit; Trips, Truck transit (`templates/remote_transfer_v2/bucket_logistics.html:212-224`) | 3 KPI rows, order/farm/run tables | `region,farm,team,q,from,to` | - |
| - Bucket Journey | Trace one bucket end to end | per-bucket hero + stage timeline (`public/js/rt-journey-v2.js`) | timeline | search a bucket | - |
| Cold Room `/cold-room-v2` | Live shelf stock + capacity | Stems on shelf (dark), Shelves available, Used capacity %, Avg age, Received-not-shelved (`www/cold-room-v2.html:238-250`) | capacity bars per cold store, bar charts (variety horizontal, age bands coloured, length), doughnut by farm, sensor line chart with min/max dashed limits (:454-459) | `region,farm,from,to` | 120 s |
| Discards `/packhouse-discards-v2` | Aged buckets in approved discards | Stems/Buckets discarded, Foregone value, Avg age | table | `from,to,region,farm,variety,rose,q` | 30 s |
| Downgrades `/packhouse-downgrades-v2` | Lines filled with longer stems than ordered | Lines, Stems, Foregone KES, Orders affected | table | `from,to,region,farm,rose,team,owner,reason,q` | 30 s |
| Stock Take `/stock-take-v2` | Received/issued vs what the count found | Expected, Counted (dark), Located %, Not expected, Not located (`www/stock-take-v2.html`) | 2-series filled line (`#c-age`, :348) age-band, reconciliation tables, tabs | `view,from,to,region,farm,take,variety,rose` | no |
| Bucket Count `/bucket-count-v2` | Empty buckets counted by farm | Counted, Farms counted, In system, In use, Not yet counted | line trend `#c-trend` (:174) | `view,from,to,region,farm,location` | no |
| Stock Visibility `/stock-visibility-v2` | Variety stock vs orders | Allocatable, Coldroom, On shelf, Still to allocate, Variance | variety x farm x length table | many incl. `basis,cover,age,demand,sort` | 60 s |
| Avails `/avails-v2` (wide) | Available per variety/farm/length | Allocated, Available, Demand, Held for discard, On shelf, Unmet | farm x length pivot | `from,to,region,farm,len,variety,age,q` | 60 s |
| Stem Movement `/stem-movement-v2` | Stems per packhouse step (harvested, received, shelved, allocated, issued, packed, staged, loaded, dispatched, discarded) + box traceability | Harvested, Packed, Dispatched, On shelf now | Flow tab (the closest thing to a funnel; `api/v2/stem_movement.py:STAGES`), Trace tab | `tab,from,to,region,farm,variety,location,box` | no |
| Order Fulfilment `/order-fulfilment-v2` | Supplied vs ordered/confirmed per customer | Ordered, Confirmed, Packed, Supplied, % vs confirmed/ordered | customer table | `from,to,region,farm,customer,manager,rose,q,sort` | no |
| Biflorica / Floriday Orders (shared `templates/includes/packhouse_v2/channel_orders_v2.html`) | Channel order sync + post offers | counts | table, select + submit | `from,to,region,farm,status,q,en,hours` | - |
| Sales Order `/sales-order-v2` (2568 lines) | Create/edit orders, standing orders | Orders, Stems, Boxes, Drafts, Due next 7 days... | list + editor | `view,name,q,status,customer,region,rose,team,from,to` | - |
| Sales Settings | currencies, price lists, customer defaults | counts | tables | `q,currency` | - |
| Variety Tree | item-group tree with gaps | Varieties, Demand, Production, Missing colour/photo | tree | `region,farm,from,to,status,line,gap,q` | - (server cache) |
| Price Lists / New Customer PL / Specifications | reference data | Priced, Missing, Coverage... / Active specs | tables/editors | various | - |

Not covered by any page today: management-level packing efficiency (throughput/hour, cycle time per stage, WIP ageing, blockers, team ranking, targets). Closest: Orders & Workflow (stage tabs + per-line takt) and Stem Movement Flow (period totals only). Packing Visibility must not repeat order-line tables; it should summarise and drill out.

### Deep-link targets (what Packing Visibility can link into)
`PH.state` reads every default key from `location.search`, so links are plain query strings:
- Stage counts / stage funnel segment -> `/packhouse-dashboard-v2?from=D&to=D&stage=<notalloc|issue|pack|packed|precooling|staged|loaded|dispatched|invoiced>&team=<T>&farm=&region=&rose=`; "Needs attention" KPI opens its drawer on that page only via onClick (no URL key), so link to the stage tab or `view=team`.
- Team throughput row -> `/packhouse-dashboard-v2?team=<name>&from=&to=`
- Customer -> `...?customer=<name>&view=customer`; order/DN/SI -> `?so=`, `?dn=`, `?si=`
- Dispatched/Packed stems per step -> `/stem-movement-v2?from=&to=&region=&farm=`
- Uncovered demand / allocation gaps -> `/sales-allocation-planning-v2?from=&to=&status=&farm=`
- Cold room fullness or age -> `/cold-room-v2?region=&farm=`; stock gaps -> `/stock-visibility-v2?cover=&age=`
- Remote transfer bottlenecks (no trip, in transit, not found) -> `/remote-transfer-v2?tab=logistics&from=&to=&region=&farm=&team=`; single bucket -> `tab=journey`
- Downgrades/discards cost -> `/packhouse-downgrades-v2?team=&reason=`, `/packhouse-discards-v2?`
- Supplied vs ordered per customer -> `/order-fulfilment-v2?customer=&from=&to=`
Caveat: filter value spellings must match each page (e.g. `region` = Ravine|Karen, `rose` = all|standard|spray; check `PH.pills` markup `data-value` per page before linking).

## 2. UI building blocks

Docs: `templates/includes/packhouse_v2/README.md` (page contract, 149 lines). Design source `/home/jk/Projects/Designs/ufd-modern-index.html` (tokens copied verbatim into CSS).

### JS `window.PH` (`public/js/packhouse-v2.js`, 1492 lines)
- Data: `PH.call` :38, `PH.getList` :105; `PH.state` :370 (URL-synced), `PH.pills` :421, `PH.dateRange` :445 (presets today/yesterday/tomorrow/7d/14d/30d/next7/mtd), `PH.regionPills` :509, `PH.bindSelect` :533, `PH.options` :548, `PH.bindSearch` :572.
- Refresh: `PH.poll(fn, ms)` :628 (pauses when tab hidden, refires on visibility, `fn(true)` = background), `PH.keepScroll` :643, `PH.markUpdated` :653 ("Updated x ago", turns stale after 5 min), `PH.onRefresh` :668 (topbar button).
- States: `PH.empty` :681, `PH.error` :692 (retry), `PH.skeleton` :711 (rows/tiles/block), `PH.kpiSkeleton`/`PH.loadingKpis` :729-783.
- `PH.kpis` :739: `{label,value,unit,suffix,trend{dir,text,note},bar,barClass,onClick,active,dark,title}`. No sparkline slot (the design file has `.kpi__spark`, 80x30 svg, `ufd-modern-index.html:89`, not ported).
- `PH.table` :801: sortable, `total`, `pageSize`, `onRowClick`, `rowClass`, `compact`, `tall`, `html`/`format` cell fns.
- `PH.modal` :985 (`drawer:true`, `size`), `PH.confirm` :1058, `PH.menu` :1088, `PH.toast` :960, `PH.autocomplete` :1128, `PH.csv` :1237.
- `PH.chart` :1337 supports only `line | bar | doughnut` (+ per-series `type` mixing, `stacked`, `horizontal`, `fill` gradient, `dashed`, `colors[]`, `onClick(index,label)`, `options` deep-merged :1429). `PH.palette`, `PH.statusColor` {good,warn,bad,signal,ink,mute}, `PH.gradient`.
- Chart.js: lazy-loaded from CDN `https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js` (:1265), NOT vendored (only `www/cold-room.html` v1 also references it). Defaults themed at :1267-1285 (Poppins 11px, grey ticks, legend off, black tooltips). No plugins registered (no annotation, matrix, datalabels). On an offline/TV network the CDN is a single point of failure.
- No dark-mode theme exists in v2 CSS (no `prefers-color-scheme`; `html.ph2-root` forces `#f4f3ef`). `.ph-kpi--dark`/`.ph-kpi.dark` is a black card variant only.

### CSS (`public/css/packhouse-v2.css`, 548 lines)
Tokens `.ph2{...}` :21-52: ink scale `--ink..--ink-faint`, surfaces `--bg #f4f3ef / --surface / --surface-2 #fff`, `--signal #228883`, `--good #1a8a3a`, `--warn #9a5a00`, `--bad #c4302b` (+ `-soft`), gradients `--grad-ink/good/warn/bad/signal`, `--sp-1..9` (4-48px), radii 10/14/20/24/pill, `--page-max:1480px`, `--page-pad:40px`. Typography Poppins (400-700) + JetBrains Mono; KPI value 36px/600, label 11px caps +1.8px tracking (:215-216). Cards borderless white, `--shadow-card`. Components: `.ph-kpis` auto-fit minmax(190px) :210, `.ph-row--2-1/1-2/2/3` :241-244, `.ph-tiles` :247, `.ph-stats` :262, `.ph-list` :320, `.ph-progress` (+good/warn/bad), `.ph-hb` estimate-vs-actual bars :299, `.ph-funnel` :309 (3 stacked bars `s2`,`s3` colours; a horizontal ranked funnel with counts), `.ph-badge`, `.ph-dot`, `.ph-notice`, `.ph-chart` (280px; `--sm` 200, `--lg` 360) :465, `.ph-legend` :468. Responsive breakpoints 1200/1000/860 px (:498-540; at 860 sidebar becomes a drawer, KPIs 2-col). `@media print` strips chrome.
Rule: no hex/radii/shadows/fonts in pages; local `<style>` only for page-unique layout with an abbreviation prefix and `var(--)` tokens (README rule 1). Existing pages bend this (e.g. `.ow-*` in packhouse-dashboard uses `#f59e0b`, conic-gradient rings, a bespoke stepper) - copy the pattern but use tokens.

### How existing pages draw things
- Cold Room: stacked "capacity bars" as markup (progress track with min/max ticks + full/half/empty segment bar), age-band bar chart with per-bar colours, sensor line chart with dashed min/max limit series via `PH.chart` series `dashed:true` (`www/cold-room-v2.html:422-459`). The only existing control-limit pattern.
- Stock Take: 2-series filled line (`fill:true`, `PH.statusColor.good/bad`) with tooltip callback override (:348-356); reconciliation as rows with inline `.ph-progress`.
- Bucket Count: single-series filled trend line.
- Orders & Workflow: no chart library; per-section progress bars, stage tab counts, conic-gradient ring, 6-dot stepper (`.ow-stepper` / `.ow-st.is-done|is-part`, `www/packhouse-dashboard-v2.html` style block), takt chip (done/running/none/invalid, `api/v2/orders_workflow.py:260`).

### Screenshots (`shots/`) and what works
- `orders-workflow.png`: filter strip (pills, selects, date presets) above 5-7 KPI cards with unit line + bar; stage tab strip with count chips; collapsible team cards with dual progress bars and an amber "7 to check" badge. Works: dense but readable, status by thin bars and one badge colour. Gap: KPI row wraps to 2 lines at 1500 px (7 cards, `minmax(190px)`), no time axis.
- `cold-room.png`: black hero KPI card then white cards; per-site capacity bars with coloured status pill (Normal/Getting full/Over capacity) and tick marks for thresholds. Best existing "bullet/gauge" feel; reuse its pattern for targets.
- `stock-take.png`: KPI cards with a dark highlighted one; notice banner; pill tabs with counts; two-column cards of ledger rows with inline bars.
- `allocation-planning.png`, `rt-bucket-logistics.png` (three KPI rows, all zero tomorrow, a shelved bar), `rt-bucket-journey.png`, `stem-movement.png`: same chrome. Note Bucket Journey needs a search POST (`rt_journey.search_buckets`) that the read-only router blocked, so it shows its empty state.
Patterns that work: big-number KPI + one unit line + optional bar; status via `ph-badge--good|warn|bad` and gradient bars; pill filters; sentence-case card titles with grey meta on the right; thin uppercase eyebrow labels. Weak spots for a CEO view: nothing is time-based, no trends beside numbers, and tall tables dominate.

## 3. Missing components for an assembly-line dashboard

Chart.js 4.4.1 (CDN) core only. "Existing" = `PH.*` + `.ph-*` CSS.

| Component | Buildable with existing? | Notes / effort |
|---|---|---|
| Flow funnel Ordered -> Allocated -> Issued -> Packed -> Staged -> Loaded -> Dispatched | Yes, `.ph-funnel` (bar width + count meta) or horizontal `PH.chart` bar. Data exists: `pipeline.attach_pipeline` gives all stage stems; `_ladder` in `orders_workflow.py:415`. | S. Add per-stage drop-off % and click -> Orders & Workflow `?stage=`. Page-local only. Waterfall = stacked bar with transparent base series (`stacked`, series `color` transparent): S-M. |
| Pipeline swimlane per stage with WIP count and ageing | Not as a component. Build page-local flex lane of stage cards (count, oldest age, `ph-badge` colour) from `.ph-tiles`/`.ph-stats`. Needs a new API (WIP and oldest timestamp per stage). Timestamps exist (issue, packed, `precooling_in_at`, `staged`, loaded, delivered). | M (page-local markup) + M-L backend. |
| Heatmap hour x team throughput | No. Chart.js has no matrix without `chartjs-chart-matrix` plugin. Cheaper: CSS grid of cells with `rgba(var(--ink))` opacity scale or `--signal-soft`, ~40 lines page-local JS. | M. Needs hourly counts API (Box Label `creation`/`staged` timestamps, Pick List Item `issued_at`). |
| Run chart / trend with control limits | Mostly: `PH.chart` line + dashed constant series (precedent `cold-room-v2.html:454-459`), no plugin needed. Mean +/- 3 sigma computed server-side or in JS. Out-of-limit points need `pointBackgroundColor` array via `options`. | S-M. |
| Bullet / target gauge | Yes with `.ph-progress` + a tick `<i>` absolutely positioned (cold room does ticks). Doughnut half-gauge via `PH.chart` doughnut + `options.circumference:180, rotation:270`. | S. Needs targets (no target store exists; `Sales Settings` has none): add a config doc or constants - decision needed. |
| Sparklines in KPI cards | No slot in `PH.kpis`. Add page-local inline `<svg class="...">` appended after `PH.kpis` render, or tiny line `PH.chart` in each card (heavier: N canvases). Design has `.kpi__spark` CSS to port (shared-file change, "ask first"). | S page-local (inline SVG helper ~20 lines); shared change preferable. |
| Pareto of blockers | Yes: `PH.chart` bar + `type:'line'` cumulative series on a second y axis (`options.scales.y1`). Blocker taxonomy exists in `_flags()` (`orders_workflow.py:477`: flags, pack issues, not found, no trip, unallocated lines). | S-M. |
| Gantt-like timeline of the day | No. Chart.js floating bars (`data:[[start,end]]`, `indexAxis:'y'`, time as numbers) work with core only; else CSS lanes with absolutely positioned bars. Per-OPL start = first issue, end = last staged already in `_takt`. | M. |
| Ranked list with owner and call to action | Yes: `.ph-list` (`__rank __name __meta __qty`) + `PH.table` + `ph-link`/`ph-btn--sm` + deep link. Owner fields: Order manager (Order Fulfilment `manager`), pick list team, `packed_by`. | S. |
| Takt / cycle-time KPI, OEE-style "Availability x Performance x Quality" | KPI cards fine (`PH.kpis`). Definitions needed (planned vs actual stems/hour, downgrades/pack issues as quality). Takt exists per OPL only; no day-level aggregation endpoint. | Backend M-L, UI S. |
| Large "TV mode" typography / auto-cycle | Missing (see 4). | S-M page-local. |
| Offline-safe Chart.js | Missing (CDN only). Vendoring `chart.umd.min.js` 4.4.1 into `public/js/vendor/` needs a shared-file change (`PH.loadCharts`). | S, but touches a central file. |

Plugins not present and not needed for the above: annotation (use dashed series), matrix (use CSS grid), datalabels (render numbers in HTML).

## 4. Technical constraints for a new page

Wiring (README "Page skeleton"; reference `www/packhouse-dashboard-v2.html` + `www/packhouse_dashboard_v2.py`):
1. `www/packing-visibility-v2.html` extends `templates/web.html`, includes `shell_start.html`/`shell_end.html`, script block wrapped in `{% raw %}`.
2. `www/packing_visibility_v2.py`: `no_cache = 1`; `get_context` -> `page_context(context, "packing-visibility", "Packing Visibility")`; set `context.ph_wide = 1` for full width (flag only read at `shell_start.html:41` -> `.ph-page--wide{max-width:none}`; used today by avails, allocation planning, remote transfer).
3. Add a nav tuple to `NAV` in `packhouse_v2.py:26` (shared file: README says ask first), e.g. Workflow group `("packing-visibility", "Packing Visibility", "/packing-visibility-v2", "chart")`; icons live in `templates/includes/packhouse_v2/icons.html` (existing names used: layout, grid, home, truck, snow, trash, down, clipboard, cube, layers, chart, check, bag, flower, gear, tree, dollar, user-plus, doc).
4. Backend `api/v2/packing_visibility.py` (+ any shared number in `api/v2/core/`; README: one definition per number, "new shared modules must live in core/"). `@frappe.whitelist()` `get_*` functions only; return `{"success":True,...}`/`{"success":False,"error":""}`; filters in SQL before limits; KPIs follow every filter; `from_date`/`to_date`, `region`, `farm`; packing-page date = Sales Order `delivery_date`, orders `docstatus=1`; farm = order farm via `core/region.ORDER_FARM_SQL`; never read stale fields (`custom_total_boxes`, `custom_opl`, `custom_total_stems`, `custom_box_label`, `Bucket QR Code.status`). Reuse `pipeline.attach_pipeline`, `pipeline.stage_state`, `orders_workflow._takt/_flow_times`, `stock.bucket_rows`, `core/region.farms_for`, `rose.rose_sql`.
5. Page rules: every filter in `PH.state` and sent to the server; one request per change with `seq` guard; skeleton/empty/error for each area; background polls via `load(true)` with `PH.keepScroll`; escape server text (`PH.esc`/`PH.html`); no inline handlers; no page-level colours/fonts; refresh only via `PH.onRefresh` + `PH.markUpdated`.

Role gating: effectively none beyond login. `page_context` redirects Guest to `/login` (`packhouse_v2.py:88-91`); v2 endpoints rely on whitelisted-method default (any logged-in user) with `frappe.has_permission` only in `channel_orders.py:71,88`, `rt_inactive.py:178`, `sales_order.py:287`, `rt_watchlist.py:53`. No v2 page restricts by role, `hooks.py` has no v2 route rules except the old `/remote-transfer*` ones (:105-120). A CEO-only page would need a new explicit `frappe.only_for([...])` in both the www controller and the API (no precedent in v2) - decision needed on which roles.

Caching precedents for slow reads (Frappe Redis cache, `frappe.cache`): 
- Stock Take: `_cached_take`, key `ph2_stock_take_v5::{take}::{modified}` TTL 6 h, caches the unfiltered historical evaluation then filters in memory (`api/v2/stock_take.py:655-664`).
- Variety Tree: `_cached`, key `ph2_vt2:{kind}:{from}:{to}:{farms}` with `CACHE_TTL` (`api/v2/variety_tree.py:151-160`).
- Per-request memo: `frappe.local.ph2_rose_groups` (`core/rose.py:30`).
For a live dashboard use short TTL (30-60 s) keyed by (date range, region, farm, rose) and never cache today's partial numbers for long; include a version suffix in the key (precedent `v5`, `vt2`).

Polling and perf: existing intervals 30 s (discards, downgrades), 60 s (workflow, allocation, production, stock visibility, avails, scheduler), 120 s (cold room); `PH.poll` pauses while the tab is hidden. No explicit performance budgets exist. Observed costs: a stock take evaluation "scans a month of receipts (seconds)"; Orders & Workflow attaches the pipeline to every line in the date range then filters (`orders_workflow.py` docstring), so an equivalent "today, whole company" call is the baseline cost for Packing Visibility. Suggested budget: one aggregate request per refresh (<2 s warm), <=1 request per filter change, no per-row detail calls.

Mobile / TV: responsive CSS exists (breakpoints 1200/1000/860, KPI 2-col on phones, sidebar drawer, 16 px page pad at <=860). Not present: kiosk/TV mode, large-type scale, auto-refresh indicator beyond "Updated x ago", full-screen toggle, hidden sidebar by URL flag, cycling views, stale-data warning on the wall. Sidebar collapse is stored in `localStorage ph2SideCollapsed` (`packhouse-v2.js:1444+`). A TV mode would be page-local: `?tv=1` state key that adds a class to scale type with a `var(--...)` override scoped to the page prefix (e.g. `.pv-tv .ph-kpi__value{font-size:...}`), hides `.ph-filters`, collapses the sidebar, forces `PH.poll` at 30-60 s, and keeps the Chart.js CDN dependency in mind (vendor it). Dark mode is not supported by the design system; a dark wall display would be a design-system decision, not a page one.

## 5. Practical recommendations
- Reuse: `PH.kpis` (add sparkline inline), `PH.state/pills/dateRange/regionPills`, `.ph-funnel` or horizontal bar for the stage funnel, `.ph-progress` bullets with ticks (cold room pattern), `PH.chart` for run chart (dashed control limits) and Pareto (bar + line on y1), `.ph-list` ranked blockers with deep links, `PH.table` compact for the drill-down drawer (`PH.modal({drawer:true})`).
- New page-local helpers (prefix `pv-`): sparkline SVG, swimlane stage cards with ageing, hour x team heat grid, day timeline, TV mode. All doable without shared-file edits; vendoring Chart.js, a KPI sparkline slot, a nav entry, and any new tokens (heat scale) need the central owner.
- New backend: one `api/v2/packing_visibility.py` (+ `core/` additions for hourly throughput and stage WIP/ageing) so numbers agree with Orders & Workflow and Stem Movement; targets storage needs a decision.
- Screenshot caveat: shots were taken with only `get_*` reads allowed; `dashboard.getTeams` and `rt_journey.search_buckets` were blocked by the router (read-looking POSTs outside the allowlist), so the team list and journey search are empty in the shots. Data shown is local kaitet.local data dated 25 Oct 2026 (small for Orders & Workflow: 12 lines).
