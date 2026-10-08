# Packhouse backend v2 audit (analysis phase, 6 Oct 2026)

Read-only audit of every packhouse dashboard API against the packhouse data model,
proven on kaitet.local data (which runs to 25 Sep 2026). Nothing in the app was changed.

- `report.html` — the consolidated report: root causes, source of truth per number,
  slowest endpoints, prerequisites, v2 backend shape, and all 173 findings page by page.
- `method.md` — the ground-truth rules every finding was checked against.
- One detailed file per area, with file:line evidence and per-page design notes:
  `workflow.md` (Workflow, Production, Discards, Downgrades), `orders.md` (Order Summary,
  Fulfilment, Allocation Planning), `stock.md` (Stock Visibility, Avails, Cold Room),
  `movement.md` (Stem Movement, Bucket Journey, Bucket Logistics), `transfers.md`
  (Transfer Scheduling, Scheduler, Truck Routes), `sales.md` (Sales Order, Specifications,
  price lists, Sales Settings, Variety Tree, channel orders).
- `sql/` — read-only queries behind the Sales Order box-total checks.

Finding IDs (WF-, OR-, ST-, MV-, TR-, SO-) are stable; use them when fixing.
