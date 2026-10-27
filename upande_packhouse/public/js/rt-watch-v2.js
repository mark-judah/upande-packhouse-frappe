/* Remote Transfers v2 · Bucket Journey · Watchlist and Inactive buckets.
   Views (?view=bucket|watch|inactive) share the Bucket Journey tab. "Bucket" is rt-journey-v2.js; this
   file adds the switch, the Watch button on the bucket summary, the Watchlist and the Inactive list.
   Data: api/v2/rt_watchlist.py (get_watchlist, get_bucket_watch; POST add_watch, add_watches, close_watch,
   reopen_watch, remove_watch, run_watchlist) and api/v2/rt_inactive.py (get_inactive_buckets).
   Loaded by templates/remote_transfer_v2/bucket_journey.html after rt-journey-v2.js. */
(function () {
  "use strict";
  const W = "upande_packhouse.api.v2.rt_watchlist.";
  const I = "upande_packhouse.api.v2.rt_inactive.";
  const E = PH.esc;
  const st = PH.state({ view: "bucket", wr: "", ws: "", wq: "", days: "", iq: "", istage: "" });
  const $bucket = document.getElementById("rtw-bucket");
  const $panel = document.getElementById("rtw-panel");
  const $hint = document.getElementById("rtw-hint");
  if (!$panel) return;

  const VIEWS = {
    bucket: "Trace one bucket end to end.",
    watch: "Buckets you are waiting to see again. Found ones are separated from the truly missing.",
    inactive: "Buckets still in the process with no activity for a while.",
  };
  const STATUS = { Watching: ["Watching", "warn"], Reappeared: ["Reappeared", "signal"], "Closed - Resolved": ["Found, resolved", "good"], "Closed - Confirmed Missing": ["Confirmed missing", "bad"] };
  const badge = (text, tone) => `<span class="ph-badge${tone ? " ph-badge--" + tone : ""}">${E(text)}</span>`;
  const parse = (t) => { const m = String(t || "").match(/^(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d)(?::(\d\d))?/); return m ? new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0)) : null; };
  let NOW = new Date();
  const ago = (t) => {
    const d = parse(t); if (!d) return "";
    const s = Math.max(0, (NOW - d) / 1000);
    if (s < 90) return "just now";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 172800) return Math.round(s / 360) / 10 + " h ago";
    return Math.round(s / 8640) / 10 + " days ago";
  };
  const when = (t) => (t ? PH.fmt.date(String(t).slice(0, 10), true) + " " + String(t).slice(11, 16) : "—");
  const openJourney = (id) => { st.set({ view: "bucket" }); if (window.RTJ_OPEN) window.RTJ_OPEN(id); };
  const fmtDays = (d) => (d == null ? "—" : (d >= 10 ? Math.round(d) : Math.round(d * 10) / 10) + " d");

  /* ── Switch ──────────────────────────────────────────────────── */
  PH.pills("#rtw-view", st, "view");
  function showView(v) {
    $bucket.hidden = v !== "bucket";
    $panel.hidden = v === "bucket";
    $hint.textContent = VIEWS[v] || "";
    if (v === "watch") renderWatch();
    else if (v === "inactive") renderInactive();
  }
  st.on((s, ch) => {
    if (ch.indexOf("view") >= 0) return showView(s.view);
    if (s.view === "watch" && ch.some((k) => k === "wr" || k === "ws" || k === "wq")) return loadWatch();
    if (s.view === "inactive" && ch.some((k) => k === "wr" || k === "days" || k === "iq")) return loadInactive();
    if (s.view === "inactive" && ch.indexOf("istage") >= 0) return paintInactive();
  });
  if (!VIEWS[st.get("view")]) st.set({ view: "bucket" }, { silent: true });

  const regionBar = () => `<div class="ph-pills" id="rtw-region" aria-label="Region"></div>`;
  function bindRegion() { PH.regionPills("#rtw-region", st, "wr"); }
  function searchBox(id, ph, val) {
    return `<label class="ph-search rtw-search"><svg class="ph-ic" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg><input id="${id}" type="search" placeholder="${E(ph)}" value="${E(val)}" autocomplete="off" spellcheck="false" aria-label="${E(ph)}"></label>`;
  }

  /* ═══ WATCHLIST ═════════════════════════════════════════════════ */
  let WL = null, wseq = 0, wTable = null;
  function renderWatch() {
    const s = st.get();
    PH.set($panel, `
      <section class="ph-card rtw-bar">
        <div class="rtw-bar__row">
          ${regionBar()}
          ${searchBox("rtw-q", "Search bucket or note", s.wq)}
          <span class="rtw-grow"></span>
          <button type="button" class="ph-btn ph-btn--ghost" id="rtw-add">${PH.icon("plus")} Watch a bucket</button>
          <button type="button" class="ph-btn ph-btn--primary" id="rtw-run" title="Look for each open watch's latest appearance and save it">${PH.icon("refresh")} Refresh appearances</button>
        </div>
        <details class="rtw-help"><summary>How the watchlist works</summary>
          <p><b>Why a bucket is here.</b> A bucket marked <i>not found</i> while issuing or during a transfer is added automatically, watched from the moment it was marked. You can also watch any bucket by hand.</p>
          <p><b>Reappeared</b> means the bucket was handled or scanned somewhere in the process after the watch began: harvested, received, shelved, moved, taken off a shelf, loaded, issued, counted, used as a replacement. Being allocated again, or the shelf clean-up that the not-found step itself does, does not count. The table always shows the latest appearance, so it keeps moving until you close it.</p>
          <p><b>Close it out:</b> <i>Found, resolved</i> when it turned up and the issue is settled; <i>Confirmed missing</i> when it is truly gone. That keeps found-again buckets apart from the truly missing ones. <i>Refresh appearances</i> saves the latest appearance on each watch; just viewing the list changes nothing.</p>
        </details>
      </section>
      <div class="ph-kpis" id="rtw-kpis"></div>
      <div class="ph-pills rtw-status" id="rtw-status" aria-label="Status">
        <button data-value="">All</button><button data-value="watching">Watching</button><button data-value="reappeared">Reappeared</button><button data-value="resolved">Found, resolved</button><button data-value="confirmed">Confirmed missing</button>
      </div>
      <section class="ph-card"><div id="rtw-tbl"></div></section>`);
    bindRegion();
    PH.pills("#rtw-status", st, "ws");
    PH.kpiSkeleton("#rtw-kpis", ["Watching", "Reappeared", "Found, resolved", "Confirmed missing"]);
    document.getElementById("rtw-q").addEventListener("input", PH.debounce((e) => st.set({ wq: e.target.value.trim() }), 300));
    loadWatch();
  }
  async function loadWatch(extra) {
    const my = ++wseq, s = st.get();
    let r = extra;
    if (!r) {
      PH.skeleton("#rtw-tbl", "rows", 5);
      try { r = await PH.call(W + "get_watchlist", { status: s.ws, search: s.wq, region: s.wr }, { silent: true, type: "GET" }); }
      catch (e) { return my === wseq && PH.error("#rtw-tbl", e.message, () => loadWatch()); }
    }
    if (my !== wseq || st.get("view") !== "watch") return;
    WL = r; NOW = parse(r.now_ts) || new Date();
    const k = r.kpi, on = (key) => s.ws === key;
    PH.kpis("#rtw-kpis", [
      { label: "Watching", value: k.watching, unit: "still missing", title: "Open watches with no appearance yet", onClick: () => st.set({ ws: on("watching") ? "" : "watching" }), active: on("watching") },
      { label: "Reappeared", value: k.reappeared, unit: "seen again, not closed", title: "Seen again somewhere in the process: review and close", onClick: () => st.set({ ws: on("reappeared") ? "" : "reappeared" }), active: on("reappeared") },
      { label: "Found, resolved", value: k.resolved, unit: "closed", onClick: () => st.set({ ws: on("resolved") ? "" : "resolved" }), active: on("resolved") },
      { label: "Confirmed missing", value: k.confirmed, unit: "closed", onClick: () => st.set({ ws: on("confirmed") ? "" : "confirmed" }), active: on("confirmed") },
    ]);
    const c = document.getElementById("rtw-c-watch");
    c.textContent = k.watching + k.reappeared; c.hidden = !(k.watching + k.reappeared);
    wTable = PH.table("#rtw-tbl", {
      rows: r.rows, pageSize: 50, sort: { key: "watching_since", dir: "desc" },
      empty: s.ws || s.wq || s.wr ? "Nothing matches" : "The watchlist is empty", emptyText: s.ws || s.wq || s.wr ? "Clear the filters to see every watch." : "Buckets marked not found are added automatically. Watch any bucket from its journey page.",
      columns: [
        { key: "bucket_id", label: "Bucket", html: (v, x) => `<a href="#" class="rtw-id" data-act="open" data-b="${E(v)}">${E(v)}</a>${x.farm ? `<span class="rtw-sub">${E(x.farm)}</span>` : ""}<span class="rtw-sub rtw-mo">${E(x.source)} · since ${E(when(x.watching_since))}</span>` },
        { key: "source", label: "Why it is here", cls: "rtw-hm", html: (v, x) => `<b>${E(v)}</b>${x.reason ? `<span class="rtw-sub">${E(x.reason)}</span>` : ""}` },
        { key: "watching_since", label: "Watching since", cls: "rtw-hm", html: (v) => `${E(when(v))}<span class="rtw-sub">${E(ago(v))}</span>` },
        { key: "days_missing", label: "Days missing", num: true, cls: "rtw-hm", html: (v, x) => (x.latest ? `<span class="ph-muted">${E(fmtDays(v))}</span>` : `<b>${E(fmtDays(v))}</b>`) },
        { key: "latest", label: "Latest appearance", sortValue: (x) => (x.latest ? x.latest.at : ""), html: (v, x) => latestHtml(x) },
        { key: "live_status", label: "Status", html: (v, x) => statusHtml(x) },
        { key: "name", label: "", sortable: false, html: (v, x) => actionsHtml(x) },
      ],
    });
    PH.markUpdated();
  }
  function latestHtml(x) {
    const l = x.latest;
    if (!l) return x.status === "Closed - Confirmed Missing" ? '<span class="ph-muted">never seen again</span>' : '<span class="rtw-miss">Not seen since it went missing</span>';
    const bits = [l.where, l.farm && l.farm !== l.where ? l.farm : "", x.journey ? "journey #" + x.journey : "", x.stage].filter(Boolean).join(" · ");
    return `<b>${E(ago(l.at))}</b> <span class="rtw-ev">${E(l.event)}</span><span class="rtw-sub">${E(when(l.at))}${bits ? " · " + E(bits) : ""}</span>`;
  }
  function statusHtml(x) {
    const [t, tone] = STATUS[x.live_status] || [x.live_status, ""];
    const closed = x.closed_on ? `<span class="rtw-sub">${E(x.closed_by)} · ${E(when(x.closed_on))}${x.close_note ? " · " + E(x.close_note) : ""}</span>` : "";
    return badge(t, tone) + closed;
  }
  function actionsHtml(x) {
    const open = x.status === "Watching" || x.status === "Reappeared";
    const b = (a, label, cls) => `<button type="button" class="ph-btn ph-btn--sm ${cls || "ph-btn--ghost"}" data-act="${a}" data-n="${E(x.name)}" data-b="${E(x.bucket_id)}">${label}</button>`;
    return `<div class="rtw-acts">${b("open", "Journey")}${open ? b("resolve", "Found", x.live_status === "Reappeared" ? "ph-btn--primary" : "") + b("missing", "Confirm missing") : b("reopen", "Reopen")}</div>`;
  }
  function closeDialog(name, bucket, outcome) {
    const resolved = outcome === "resolved";
    const box = document.createElement("div");
    box.innerHTML = `<p class="ph-muted">${resolved ? "It turned up and the issue is settled." : "It is truly missing: nobody has seen it and it is not coming back."} This closes the watch for <b>${E(bucket)}</b>.</p>
      <div class="ph-field"><label for="rtw-note">Note (optional)</label><textarea id="rtw-note" rows="3" class="ph-input rtw-ta" placeholder="${resolved ? "Where was it, who found it" : "What was checked"}"></textarea></div>`;
    PH.modal({ title: resolved ? "Close as found, resolved" : "Confirm missing", body: box, actions: [{ label: "Cancel" }, { label: resolved ? "Close as resolved" : "Confirm missing", primary: resolved, danger: !resolved,
      onClick: async () => { await PH.call(W + "close_watch", { name, outcome: resolved ? "Found - resolved" : "Confirmed missing", note: box.querySelector("#rtw-note").value.trim() }); PH.toast(resolved ? "Closed as found" : "Confirmed missing", "good"); loadWatch(); } }] });
  }
  function addDialog(prefill, done) {
    const box = document.createElement("div");
    box.innerHTML = `<div class="ph-field"><label for="rtw-nb">Bucket ID</label><input id="rtw-nb" class="ph-input" value="${E(prefill || "")}" placeholder="Scan or type a bucket ID" autocomplete="off" spellcheck="false"></div>
      <div class="ph-field" style="margin-top:12px"><label for="rtw-nn">What is the issue? (optional)</label><textarea id="rtw-nn" rows="3" class="ph-input rtw-ta" placeholder="e.g. short of stems, wrong label, not on its shelf"></textarea></div>`;
    PH.modal({ title: "Watch a bucket", sub: "You will see when it next appears anywhere in the process.", body: box, actions: [{ label: "Cancel" }, { label: "Start watching", primary: true,
      onClick: async () => { const id = box.querySelector("#rtw-nb").value.trim(); if (!id) { PH.toast("Enter a bucket ID.", "warn"); return false; }
        const r = await PH.call(W + "add_watch", { bucket_id: id, note: box.querySelector("#rtw-nn").value.trim() });
        PH.toast(r.action === "already watching" ? "Already on the watchlist" : "Watching " + id.toUpperCase(), "good"); if (done) done(r); } }] });
  }
  async function runWatch(btn) {
    btn.disabled = true; btn.classList.add("is-busy");
    try {
      const s = st.get();
      const r = await PH.call(W + "run_watchlist", { search: s.wq, region: s.wr });
      PH.toast(r.changed ? PH.fmt.plural(r.changed, "bucket") + " seen again · " + (r.sync.added ? r.sync.added + " newly watched · " : "") + (r.ms / 1000).toFixed(1) + " s" : "No new appearances" + (r.sync.added ? " · " + r.sync.added + " newly watched" : ""), r.changed ? "good" : undefined);
      loadWatch(r);
    } catch (e) { /* toasted */ } finally { btn.disabled = false; btn.classList.remove("is-busy"); }
  }

  /* ═══ INACTIVE ══════════════════════════════════════════════════ */
  let IN = null, iseq = 0, iTable = null;
  const SEL = new Set();
  function renderInactive() {
    const s = st.get();
    PH.set($panel, `
      <section class="ph-card rtw-bar">
        <div class="rtw-bar__row">
          <div class="rtw-idle" title="Buckets still in the process (on a shelf, in transit, allocated, received but not shelved) with no event for at least this many days">
            <label for="rtw-days">Idle for at least</label>
            <div class="rtw-step"><button type="button" data-step="-1" aria-label="One day less">&minus;</button><input id="rtw-days" type="number" min="1" max="365" inputmode="numeric" value="${E(s.days || "")}" placeholder="4"><button type="button" data-step="1" aria-label="One day more">+</button></div>
            <span class="rtw-idle__u">days</span>
          </div>
          ${regionBar()}
          ${searchBox("rtw-iq", "Search bucket, variety, shelf", s.iq)}
          <span class="rtw-grow"></span>
          <button type="button" class="ph-btn ph-btn--ghost" id="rtw-csv">${PH.icon("download")} Export CSV</button>
          <button type="button" class="ph-btn ph-btn--primary" id="rtw-bulk" disabled>Add selected to watchlist</button>
        </div>
        <div class="rtw-def" id="rtw-def"><span class="rtw-def__i" tabindex="0" title="">i</span><span id="rtw-deftext">The default comes from Production Settings &rarr; Inactive Bucket Days.</span></div>
      </section>
      <div class="ph-kpis" id="rtw-kpis"></div>
      <div class="rtw-stages" id="rtw-stages"></div>
      <section class="ph-card"><div id="rtw-tbl"></div></section>`);
    bindRegion();
    PH.kpiSkeleton("#rtw-kpis", ["Inactive buckets", "Stems idle", "Oldest idle", "On watchlist"]);
    document.getElementById("rtw-iq").addEventListener("input", PH.debounce((e) => st.set({ iq: e.target.value.trim() }), 300));
    const $d = document.getElementById("rtw-days");
    const setDays = PH.debounce(() => { const v = parseInt($d.value, 10); st.set({ days: v > 0 && !(IN && v === IN.default_days) ? String(v) : "" }); }, 450);
    $d.addEventListener("input", setDays);
    $d.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); setDays(); } });
    $panel.querySelectorAll("[data-step]").forEach((b) => b.addEventListener("click", () => { const cur = parseInt($d.value, 10) || (IN ? IN.days : 4); $d.value = Math.max(1, Math.min(365, cur + +b.dataset.step)); setDays(); }));
    loadInactive();
  }
  async function loadInactive() {
    const my = ++iseq, s = st.get();
    PH.skeleton("#rtw-tbl", "rows", 6);
    let r;
    try { r = await PH.call(I + "get_inactive_buckets", { days: s.days, search: s.iq, region: s.wr, limit: 1000 }, { silent: true, type: "GET" }); }
    catch (e) { return my === iseq && PH.error("#rtw-tbl", e.message, () => loadInactive()); }
    if (my !== iseq || st.get("view") !== "inactive") return;
    IN = r; NOW = parse(r.now_ts) || new Date();
    const $d = document.getElementById("rtw-days");
    if ($d && document.activeElement !== $d) $d.value = r.days;
    $d.placeholder = r.default_days;
    document.getElementById("rtw-deftext").innerHTML = `Default <b>${r.default_days} days</b> from Production Settings &rarr; Inactive Bucket Days; your choice here is kept in the address. ${r.in_process ? PH.fmt.num(r.in_process) + " buckets are in the process now." : ""}`;
    document.querySelector(".rtw-def__i").title = r.definition + " In the process: on a shelf, in transit, allocated, received but not shelved. Buckets fully issued or discarded are not listed.";
    const k = r.kpi;
    PH.kpis("#rtw-kpis", [
      { label: "Inactive buckets", value: k.buckets, unit: "idle " + r.days + "+ days" },
      { label: "Stems idle", value: k.stems, unit: "in those buckets" },
      { label: "Oldest idle", value: fmtDays(k.oldest_days), unit: "no activity since" },
      { label: "On watchlist", value: k.watched, unit: "already watched" },
    ]);
    const c = document.getElementById("rtw-c-inactive");
    c.textContent = PH.fmt.num(k.buckets); c.hidden = !k.buckets;
    paintInactive();
  }
  function paintInactive() {
    if (!IN) return;
    const s = st.get();
    const stages = Object.keys(IN.stages).sort((a, b) => IN.stages[b] - IN.stages[a]);
    PH.set(document.getElementById("rtw-stages"), stages.length ? `<div class="ph-pills" id="rtw-stagepills"><button data-value="">All ${PH.fmt.num(IN.count)}</button>${stages.map((x) => `<button data-value="${E(x)}">${E(x)} ${PH.fmt.num(IN.stages[x])}</button>`).join("")}</div>` : "");
    if (stages.length) PH.pills("#rtw-stagepills", s.istage, (v) => st.set({ istage: v }));
    const rows = IN.rows.filter((x) => !s.istage || x.stage === s.istage);
    iTable = PH.table("#rtw-tbl", {
      rows, pageSize: 100, sort: { key: "days_idle", dir: "desc" }, compact: true,
      empty: "No inactive buckets", emptyText: "Everything in the process has moved in the last " + IN.days + " days" + (s.iq || s.wr || s.istage ? " (with these filters)." : "."),
      columns: [
        { key: "sel", label: "", sortable: false, html: (v, x) => `<input type="checkbox" data-sel="${E(x.bucket)}" aria-label="Select ${E(x.bucket)}"${SEL.has(x.bucket) ? " checked" : ""}>` },
        { key: "bucket", label: "Bucket", html: (v, x) => `<a href="#" class="rtw-id" data-act="open" data-b="${E(v)}">${E(v)}</a><span class="rtw-sub rtw-mo">${E([x.variety, PH.fmt.num(x.stems) + " stems", x.farm, x.loc, x.stage].filter(Boolean).join(" · "))}</span>` },
        { key: "variety", label: "Variety", cls: "rtw-hm" },
        { key: "stems", label: "Stems", num: true, cls: "rtw-hm" },
        { key: "farm", label: "Farm · place", cls: "rtw-hm", sortValue: (x) => x.farm + x.loc, html: (v, x) => `${E(x.farm || "—")}<span class="rtw-sub">${E(x.loc)}</span>` },
        { key: "last_at", label: "Last activity", html: (v, x) => `<b>${E(x.last_event)}</b><span class="rtw-sub">${v ? E(when(v)) : "no date on record"}</span>` },
        { key: "days_idle", label: "Days idle", num: true, html: (v) => `<b class="rtw-idlev">${E(fmtDays(v))}</b>` },
        { key: "stage", label: "Stage", cls: "rtw-hm", html: (v, x) => badge(v, v === "In transit" || v === "On trolley" || v === "Awaiting transfer" ? "signal" : v === "On shelf" ? "" : "warn") + (x.allocated && v !== "Allocated" ? '<span class="rtw-sub">allocated</span>' : "") },
        { key: "watched", label: "", sortable: false, html: (v, x) => `<div class="rtw-acts">${x.watched ? badge("Watching", "warn") : `<button type="button" class="ph-btn ph-btn--sm ph-btn--ghost" data-act="watch" data-b="${E(x.bucket)}">Watch</button>`}<button type="button" class="ph-btn ph-btn--sm ph-btn--ghost" data-act="open" data-b="${E(x.bucket)}">Journey</button></div>` },
      ],
    });
    document.getElementById("rtw-tbl").insertAdjacentHTML("afterbegin", IN.truncated ? `<div class="ph-notice ph-notice--warn">Showing the ${PH.fmt.num(IN.rows.length)} oldest of ${PH.fmt.num(IN.count)} buckets. Raise the days, or filter, to narrow it. The tiles and the CSV cover all ${PH.fmt.num(IN.count)}.</div>` : "");
    bulkState();
    PH.markUpdated();
  }
  function bulkState() {
    const b = document.getElementById("rtw-bulk"); if (!b) return;
    b.disabled = !SEL.size; b.textContent = SEL.size ? "Add " + SEL.size + " to watchlist" : "Add selected to watchlist";
  }
  async function exportCsv(btn) {
    btn.disabled = true;
    try {
      const s = st.get();
      const r = await PH.call(I + "get_inactive_buckets", { days: s.days, search: s.iq, region: s.wr, limit: 0 }, { silent: false, type: "GET" });
      const rows = r.rows.filter((x) => !s.istage || x.stage === s.istage);
      PH.csv("inactive-buckets-" + r.days + "d-" + String(r.now_ts).slice(0, 10) + ".csv", rows, [
        { key: "bucket", label: "Bucket" }, { key: "variety", label: "Variety" }, { key: "stems", label: "Stems" }, { key: "farm", label: "Farm" }, { key: "loc", label: "Shelf / place" },
        { key: "stage", label: "Stage" }, { key: "allocated", label: "Allocated", csv: (v) => (v ? "yes" : "") }, { key: "last_event", label: "Last activity" }, { key: "last_at", label: "Last activity at" },
        { key: "days_idle", label: "Days idle" }, { key: "watched", label: "On watchlist", csv: (v) => (v ? "yes" : "") }]);
    } catch (e) { /* toasted */ } finally { btn.disabled = false; }
  }

  /* ── Clicks (both views) ─────────────────────────────────────── */
  $panel.addEventListener("click", async (e) => {
    const a = e.target.closest("[data-act]");
    if (a) {
      e.preventDefault();
      const act = a.dataset.act;
      if (act === "open") return openJourney(a.dataset.b);
      if (act === "resolve") return closeDialog(a.dataset.n, a.dataset.b, "resolved");
      if (act === "missing") return closeDialog(a.dataset.n, a.dataset.b, "missing");
      if (act === "reopen") { a.disabled = true; try { await PH.call(W + "reopen_watch", { name: a.dataset.n }); PH.toast("Watching again", "good"); loadWatch(); } catch (x) { a.disabled = false; } return; }
      if (act === "watch") return addDialog(a.dataset.b, () => loadInactive());
    }
    if (e.target.closest("#rtw-add")) return addDialog("", () => loadWatch());
    if (e.target.closest("#rtw-run")) return runWatch(e.target.closest("#rtw-run"));
    if (e.target.closest("#rtw-csv")) return exportCsv(e.target.closest("#rtw-csv"));
    if (e.target.closest("#rtw-bulk")) {
      const ids = Array.from(SEL), btn = e.target.closest("#rtw-bulk"); btn.disabled = true;
      try { const r = await PH.call(W + "add_watches", { bucket_ids: JSON.stringify(ids) }); PH.toast(r.added + " added to the watchlist" + (r.already ? ", " + r.already + " already there" : ""), "good"); SEL.clear(); loadInactive(); }
      catch (x) { btn.disabled = false; }
    }
  });
  $panel.addEventListener("change", (e) => {
    const c = e.target.closest("[data-sel]");
    if (!c) return;
    c.checked ? SEL.add(c.dataset.sel) : SEL.delete(c.dataset.sel);
    bulkState();
  });

  /* ── Bucket view: Watch button + status line ─────────────────── */
  async function paintBucketWatch(id) {
    const $b = document.getElementById("rtw-bw"), $l = document.getElementById("rtw-bwline");
    if (!$b) return;
    let w = null;
    try { w = (await PH.call(W + "get_bucket_watch", { bucket_id: id }, { silent: true, type: "GET" })).watch; } catch (e) { return; }
    if (!document.getElementById("rtw-bw") || document.getElementById("rtw-bw").dataset.bucket !== id) return;
    const open = w && (w.status === "Watching" || w.status === "Reappeared");
    NOW = new Date();
    $b.innerHTML = open
      ? `${badge("Watched", "warn")}<button type="button" class="ph-btn ph-btn--sm ph-btn--ghost" data-bw="stop">Stop watching</button>`
      : `<button type="button" class="ph-btn ph-btn--sm ph-btn--ghost" data-bw="watch">${PH.icon("plus")} Watch this bucket</button>`;
    if (!w) { $l.innerHTML = ""; return; }
    const [t, tone] = STATUS[w.live_status] || [w.live_status, ""];
    let line;
    if (open) {
      const seen = w.latest ? `<b>Reappeared ${E(ago(w.latest.at))}</b>: ${E(w.latest.event)}${w.latest.farm ? " · " + E(w.latest.farm) : ""}` : `<b>Still missing</b> for ${E(fmtDays(w.days_missing))}`;
      line = `${seen} · watching since ${E(when(w.watching_since))} · ${E(w.source)}`;
    } else line = `${E(t)} ${E(when(w.closed_on))}${w.close_note ? " · " + E(w.close_note) : ""} · was watched since ${E(when(w.watching_since))}`;
    $l.innerHTML = `<div class="rtw-bwline__in rtw-bwline--${open ? (w.latest ? "seen" : "miss") : "closed"}">${line} <a href="#" data-bw="list">Open watchlist</a></div>`;
  }
  document.addEventListener("rtj:bucket", (e) => paintBucketWatch(e.detail.id));
  document.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-bw]");
    if (!b) return;
    e.preventDefault();
    const id = (document.getElementById("rtw-bw") || {}).dataset && document.getElementById("rtw-bw").dataset.bucket;
    if (b.dataset.bw === "list") return st.set({ view: "watch", wq: id || "" });
    if (!id) return;
    if (b.dataset.bw === "watch") return addDialog(id, () => paintBucketWatch(id));
    if (b.dataset.bw === "stop") {
      if (!(await PH.confirm("Stop watching " + id + "?", "The watch is removed from the list. Use Close on the watchlist to keep a record that it was found or is confirmed missing.", { ok: "Stop watching", danger: true }))) return;
      try { const g = await PH.call(W + "get_bucket_watch", { bucket_id: id }, { silent: true, type: "GET" }); await PH.call(W + "remove_watch", { name: g.watch.name }); PH.toast("Stopped watching " + id, "good"); paintBucketWatch(id); } catch (x) { /* toasted */ }
    }
  });

  /* ── Boot ────────────────────────────────────────────────────── */
  PH.ready(() => showView(st.get("view")));
})();
