/* Remote Transfers v2 · Bucket Journey. Search a bucket, read one clean timeline per journey.
   Data: upande_packhouse.api.v2.rt_journey.get_bucket_journeys (the v1 journey rules, ported
   server side) and .search_buckets. Everything v1 printed per stage is the "Details" of the
   step it belongs to. Loaded by templates/remote_transfer_v2/bucket_journey.html. */
(function () {
	"use strict";
	const CFG = window.RTJ_CFG || {};
	const API = "upande_packhouse.api.v2.rt_journey.";
	const E = PH.esc;
	const st = PH.state({
		mode: "bucket",
		region: "",
		q: "",
		limit: "5",
		from: "",
		to: "",
		j: "",
		d: "",
	});
	PH.regionPills("#f-region", st, "region");
	if (st.get("mode") !== "bunch") st.set({ mode: "bucket" }, { silent: true });

	const $res = document.getElementById("rtj-result");
	const $q = document.getElementById("rtj-q");
	const $chips = document.getElementById("rtj-chips");
	const $date = document.getElementById("rtj-d");
	let G = null; // last bucket result
	let seq = 0; // guards against out-of-order responses

	/* ── helpers ─────────────────────────────────────────────────── */
	const gapText = (h) => {
		if (h == null || h < 0.02) return "";
		if (h < 1) return Math.round(h * 60) + " min";
		if (h < 48) return Math.round(h * 10) / 10 + " h";
		return Math.round(h / 2.4) / 10 + " days";
	};
	const dayOf = (t) => PH.fmt.date(String(t || "").slice(0, 10), true);
	const timeOf = (t) => String(t || "").slice(11, 16);
	const BADGE = { ok: "good", info: "signal", warn: "warn", bad: "bad", "": "" };
	const badge = (text, tone) =>
		`<span class="ph-badge${BADGE[tone] ? " ph-badge--" + BADGE[tone] : ""}">${E(
			text
		)}</span>`;
	const facts = (rows) => {
		const r = (rows || []).filter((x) => x[1] != null && x[1] !== "");
		return r.length
			? `<div class="rtj-facts">${r
					.map(
						(x) =>
							`<div class="rtj-fact"><small>${E(x[0])}</small><b>${E(
								x[1]
							)}</b></div>`
					)
					.join("")}</div>`
			: "";
	};
	const docs = (list) => {
		if (!list || !list.length) return "";
		const shown = list.slice(0, 40);
		return `<div class="rtj-docs">${shown
			.map(
				(d) =>
					`<a href="${E(
						PH.docUrl(d.doctype, d.name)
					)}" target="_blank" rel="noopener" title="${E(d.doctype)}">${E(
						d.label || d.name
					)}</a>`
			)
			.join("")}${
			list.length > shown.length
				? `<span class="ph-muted">+${list.length - shown.length} more</span>`
				: ""
		}</div>`;
	};

	/* One step of a timeline (shared by journeys, unplaced events and bunches). */
	const table = (t) =>
		t && t.rows && t.rows.length
			? `<table class="rtj-tbl"><thead><tr>${t.cols
					.map((c) => `<th>${E(c)}</th>`)
					.join("")}</tr></thead><tbody>${t.rows
					.map((r) => `<tr>${r.map((c) => `<td>${E(c)}</td>`).join("")}</tr>`)
					.join("")}</tbody></table>`
			: "";
	function stepHtml(s) {
		const gap = gapText(s.gap_h);
		const meta = [
			s.who ? E(s.who) : "",
			gap ? `<i>${E(gap)} after previous</i>` : "",
			s.approx ? "<i>time approximate</i>" : "",
		]
			.filter(Boolean)
			.join(" · ");
		const det = facts(s.facts) + table(s.table) + docs(s.docs);
		return `<li class="rtj-s rtj-s--${s.tone || "mute"}" data-ev="${E(s.ev || "")}">
      <div class="rtj-s__when"><b>${s.approx ? "~" : ""}${E(timeOf(s.t))}</b><span>${E(
			dayOf(s.t)
		)}</span></div>
      <span class="rtj-s__dot"></span>
      <div class="rtj-s__main">
        <div class="rtj-s__t"><b>${E(s.title)}</b>${
			s.where ? `<span class="rtj-s__where">${E(s.where)}</span>` : ""
		}</div>
        ${s.line ? `<div class="rtj-s__l">${E(s.line)}</div>` : ""}
        ${meta ? `<div class="rtj-s__m">${meta}</div>` : ""}
        ${s.reason ? `<div class="rtj-s__flag">Not tied to a journey: ${E(s.reason)}</div>` : ""}
        ${(s.flags || []).map((f) => `<div class="rtj-s__flag">${E(f)}</div>`).join("")}
        ${
			det
				? `<button type="button" class="rtj-more" data-more>Details</button><div class="rtj-det">${det}</div>`
				: ""
		}
      </div></li>`;
	}
	const timeline = (steps) => `<ol class="rtj-tl">${steps.map(stepHtml).join("")}</ol>`;

	/* ── Finder: recent buckets / search / by date ───────────────── */
	function chipsHtml(r) {
		const head =
			r.mode === "search"
				? "Matching buckets"
				: r.mode === "date"
				? "Received on " + PH.fmt.date(r.date)
				: "Recently shelved";
		if (!r.rows.length)
			return `<div class="rtj-chips-h">${E(
				head
			)}</div><div class="ph-muted">Nothing found.</div>`;
		return `<div class="rtj-chips-h">${E(head)}</div><div class="rtj-chips">${r.rows
			.map(
				(x) =>
					`<button type="button" class="rtj-chip" data-track="${E(x.bucket)}"><b>${E(
						x.bucket
					)}</b><span>${E(x.note || x.variety || "")}</span></button>`
			)
			.join("")}</div>`;
	}
	let fseq = 0;
	function loadChips() {
		const my = ++fseq,
			q = $q.value.trim(),
			d = $date.value;
		if (st.get("mode") === "bunch") {
			PH.set($chips, "");
			return;
		}
		PH.call(API + "search_buckets", q.length >= 2 ? { q } : d ? { date: d } : {}, {
			silent: true,
		})
			.then((r) => {
				if (my === fseq) PH.set($chips, chipsHtml(r));
			})
			.catch(() => {
				if (my === fseq) PH.set($chips, "");
			});
	}
	$q.addEventListener("input", PH.debounce(loadChips, 250));
	$q.addEventListener("focus", () => {
		if (!$chips.innerHTML) loadChips();
	});
	$date.addEventListener("change", () => {
		$q.value = "";
		loadChips();
	});
	document.getElementById("rtj-track").addEventListener("click", () => {
		const v = $q.value.trim();
		if (!v) {
			PH.toast(
				"Enter a " + (st.get("mode") === "bunch" ? "bunch" : "bucket") + " ID.",
				"warn"
			);
			return $q.focus();
		}
		go(v);
	});
	$q.addEventListener("keydown", (e) => {
		if (e.key === "Enter") {
			e.preventDefault();
			document.getElementById("rtj-track").click();
		}
	});
	PH.pills("#rtj-mode", st, "mode");

	function go(id, mode) {
		id = String(id || "").trim();
		$q.value = id;
		PH.set($chips, "");
		const patch = { q: id, j: "" };
		if (mode) patch.mode = mode;
		const s = st.get();
		if (s.q === id && (!mode || s.mode === mode)) return track();
		st.set(patch);
	}
	st.on((s, changed) => {
		if (changed.indexOf("region") >= 0) loadOverview();
		if (changed.indexOf("mode") >= 0) {
			$q.placeholder =
				s.mode === "bunch" ? "Scan or type a bunch ID" : "Scan or type a bucket ID";
			loadChips();
			if (changed.indexOf("q") < 0 && s.q) {
				$q.value = "";
				return st.set({ q: "", j: "" });
			}
		}
		if (changed.indexOf("q") >= 0) return track();
		if (changed.some((k) => k === "limit" || k === "from" || k === "to")) return track(true);
	});

	/* ── States ──────────────────────────────────────────────────── */
	function showHint() {
		PH.set(
			$res,
			`<section class="ph-card"><div class="ph-muted">Scan or type a bucket ID above, or pick a recent one. You will see how it moved, journey by journey: harvested, received, shelved, transferred, picked, issued.</div></section>`
		);
	}
	function showLoading() {
		PH.set($res, '<section class="ph-card"><div id="rtj-sk"></div></section>');
		PH.skeleton("#rtj-sk", "rows", 5);
	}
	function showError(msg) {
		PH.set($res, '<section class="ph-card"><div id="rtj-err"></div></section>');
		PH.error("#rtj-err", msg, () => track());
	}

	function track(keep) {
		const s = st.get();
		if (!s.q) {
			G = null;
			return showHint();
		}
		return s.mode === "bunch" ? trackBunch(s.q) : trackBucket(s.q, keep);
	}

	/* ═══ BUCKET ════════════════════════════════════════════════════ */
	async function trackBucket(id, keep) {
		const my = ++seq,
			s = st.get();
		if (!keep) showLoading();
		let r;
		try {
			r = await PH.call(
				API + "get_bucket_journeys",
				{
					bucket_id: id,
					limit: parseInt(s.limit, 10) || 0,
					from_date: s.from || "",
					to_date: s.to || "",
				},
				{ silent: true }
			);
		} catch (e) {
			return my === seq && showError(e.message);
		}
		if (my !== seq) return;
		if (!r.found) {
			PH.set($res, '<section class="ph-card"><div id="rtj-empty"></div></section>');
			return PH.empty(
				"#rtj-empty",
				"Not found",
				"No records for bucket " + id.toUpperCase() + ".",
				"search"
			);
		}
		G = r;
		renderBucket();
		PH.markUpdated();
	}

	function replLine(r, id) {
		const asOld = (r.old_bucket || "").toUpperCase() === String(id || "").toUpperCase();
		const where = r.order_name || r.order_pick_list || "";
		const head = asOld
			? `${E(r.reason || "Missing")}: replaced with <b>${E(r.new_bucket || "?")}</b> on ${E(
					where
			  )}`
			: `Took the place of <b>${E(r.old_bucket)}</b> (${E(r.reason || "Missing")}) on ${E(
					where
			  )}`;
		const meta = [
			r.farm,
			r.expected_shelf && "shelf " + r.expected_shelf,
			r.vehicle && "truck " + r.vehicle,
			r.reported_at && String(r.reported_at).slice(0, 16),
			r.status !== "Open"
				? r.status + (r.resolution ? " · " + r.resolution : "")
				: "still missing",
		]
			.filter(Boolean)
			.join(" · ");
		return `<div>${head}<div class="ph-muted">${E(meta)}</div></div>`;
	}

	/* The flow strip: what the page tracks, in the order events happen. */
	const MAIN = [
		"bucket_dispatch",
		"harvest",
		"grade",
		"receive",
		"qc",
		"quarantine",
		"release",
		"reject",
		"accept",
		"shelve",
		"shelf_move",
		"transfer_req",
		"transfer_transit",
		"transfer_recv",
		"allocate",
		"issue",
		"pack",
		"dispatch",
	];
	const SIDE = [
		"discard_listed",
		"discard",
		"clear",
		"replace",
		"not_found",
		"anomaly",
		"count",
		"edit",
		"move_bunch",
	];
	function flowHtml() {
		const by = {};
		(G.flow || []).forEach((f) => (by[f.key] = f));
		const node = (k) => {
			const f = by[k];
			if (!f) return "";
			const cls = f.tracked === "no" ? " is-no" : f.tracked === "partial" ? " is-part" : "";
			return `<span class="rtj-fn${cls}${f.count ? " has" : ""}" data-fk="${E(
				k
			)}" title="${E(f.note || "")}"><b>${E(f.label)}</b>${
				f.count ? `<em>${f.count}</em>` : ""
			}${
				f.tracked === "no"
					? "<small>not tracked</small>"
					: f.tracked === "partial"
					? "<small>partly tracked</small>"
					: ""
			}</span>`;
		};
		const chain = (keys, sep) =>
			keys
				.map(
					(k, i) =>
						node(k) +
						(i < keys.length - 1
							? `<span class="rtj-fa">${(sep && sep[k]) || "→"}</span>`
							: "")
				)
				.join("");
		return `<details class="ph-card rtj-flow" id="rtj-flow"${
			window.innerWidth > 760 ? " open" : ""
		}><summary><b>What this page tracks</b><span class="ph-muted"> · numbers are how many of each this bucket has</span></summary>
      <div class="rtj-flow__row">${chain(MAIN, { quarantine: "⇄" })}</div>
      <div class="rtj-flow__row rtj-flow__row--side"><span class="rtj-flow__lbl">Also</span>${chain(
			SIDE,
			{}
		)}</div></details>`;
	}
	function paintFlow() {
		const open = $res.querySelector(".rtj-j.is-open");
		const j = open && G.journeys.find((x) => x.number === +open.dataset.jn);
		PH.$$(".rtj-fn", $res).forEach((n) =>
			n.classList.toggle("in-open", !!(j && j.counts && j.counts[n.dataset.fk]))
		);
	}
	const stemBits = (j) =>
		[
			j.stems ? PH.fmt.num(j.stems) + " in" : "",
			j.allocated_stems ? PH.fmt.num(j.allocated_stems) + " allocated" : "",
			j.issued_stems ? PH.fmt.num(j.issued_stems) + " issued" : "",
			j.on_shelf_stems ? PH.fmt.num(j.on_shelf_stems) + " on shelf" : "",
			j.quarantined_stems ? PH.fmt.num(j.quarantined_stems) + " quarantined" : "",
			j.accepted_stems ? PH.fmt.num(j.accepted_stems) + " accepted" : "",
			j.rejected_stems ? PH.fmt.num(j.rejected_stems) + " rejected" : "",
		].filter(Boolean);

	function renderBucket() {
		const b = G.bucket,
			now = G.now,
			js = G.journeys,
			total = G.index.length;
		const s = st.get();
		const open = js.find((j) => String(j.number) === String(s.j)) || js[js.length - 1];
		const jHtml = js
			.slice()
			.reverse()
			.map((j) => journeyHtml(j, open && j.number === open.number))
			.join("");
		const filtered = !!(s.from || s.to);
		const canc = (b.cancelled || []).reduce((n, x) => n + x[1], 0);
		PH.set(
			$res,
			`
      ${(G.replacements || [])
			.map((r) => `<div class="ph-notice ph-notice--warn">${replLine(r, b.id)}</div>`)
			.join("")}
      ${flowHtml()}
      <section class="ph-card">
        <div class="rtj-sum">
          <div>
            <div class="ph-hero__eyebrow">Bucket</div>
            <h2 class="rtj-sum__id">${E(b.id)}</h2>
            <div class="rtj-sum__facts">
              <div><small>Variety</small><b>${E(b.variety || "—")}</b></div>
              <div><small>Stem length</small><b>${E(b.stem_length || "—")}</b></div>
              <div><small>Stems</small><b>${b.stems ? PH.fmt.num(b.stems) : "—"}</b></div>
              <div><small>Origin farm</small><b>${E(b.farm || "—")}</b></div>
            </div>
            <div class="rtj-sum__meta">${PH.fmt.plural(b.journeys, "journey")} · first use ${E(
				PH.fmt.date(b.first_use)
			)} · last activity ${E(PH.fmt.date(b.latest_activity))}${
				canc ? ` · ${canc} cancelled stock entries hidden` : ""
			}</div>
            <div class="rtj-ph" style="margin-top:12px"><button type="button" class="ph-btn ph-btn--sm ph-btn--ghost" id="rtj-correct">${PH.icon(
				"edit"
			)} Correct details</button><span id="rtw-bw" class="rtw-bw" data-bucket="${E(
				b.id
			)}"></span></div>
            <div id="rtw-bwline" class="rtw-bwline"></div>
          </div>
          <div>
            <div class="ph-label" style="margin-bottom:6px">Current stage</div>
            <div class="rtj-now rtj-now--${
				now.tone || ""
			}"><span class="rtj-now__dot"></span><div><div class="rtj-now__t rtj-now__big">${E(
				now.label
			)}</div>
              ${now.detail ? `<div class="rtj-now__d">${E(now.detail)}</div>` : ""}
              ${
					now.since
						? `<div class="rtj-now__s">since ${E(PH.fmt.datetime(now.since))}</div>`
						: ""
				}</div></div>
          </div>
        </div>
      </section>
      <div class="rtj-jhead">
        <h2>Journeys <span class="ph-muted" style="font-weight:400;font-size:13px">showing ${
			js.length
		} of ${total}</span></h2>
        <div class="rtj-jtools">
          <label class="ph-daterange" title="Journeys started between"><input type="date" id="rtj-from" value="${E(
				s.from
			)}" aria-label="From"><span class="ph-daterange__sep">→</span><input type="date" id="rtj-to" value="${E(
				s.to
			)}" aria-label="To"></label>
          <select id="rtj-limit" aria-label="Journeys to show">${[
				["5", "Last 5"],
				["10", "Last 10"],
				["20", "Last 20"],
				["50", "Last 50"],
				["0", "All"],
			]
				.map(
					(o) =>
						`<option value="${o[0]}"${o[0] === s.limit ? " selected" : ""}>${
							o[1]
						}</option>`
				)
				.join("")}</select>
          ${
				filtered || s.limit !== "5"
					? '<button type="button" class="ph-btn ph-btn--ghost ph-btn--sm" id="rtj-reset">Reset</button>'
					: ""
			}
        </div>
      </div>
      ${
			jHtml ||
			'<section class="ph-card"><div class="ph-muted">No journeys in that range.</div></section>'
		}
      ${unplacedHtml()}`
		);
		paintFlow();
		document.dispatchEvent(new CustomEvent("rtj:bucket", { detail: { id: b.id } })); // rt-watch-v2.js paints the Watch button
	}

	function unplacedHtml() {
		const u = G.unplaced || [];
		if (!u.length) return "";
		return `<section class="ph-card"><div class="ph-card__head"><h2 class="ph-card__title">Not tied to a journey</h2><span class="ph-card__meta">${PH.fmt.plural(
			u.length,
			"record"
		)} that could not be placed, listed so nothing is lost</span></div>${timeline(
			u
		)}</section>`;
	}

	function journeyHtml(j, isOpen) {
		const range =
			j.start_date === j.end_date || !j.end_date
				? PH.fmt.date(j.start_date, true)
				: PH.fmt.date(j.start_date, true) + " → " + PH.fmt.date(j.end_date, true);
		const sub = [j.stem_length, j.stems ? PH.fmt.num(j.stems) + " stems" : "", range]
			.filter(Boolean)
			.join(" · ");
		const bits = stemBits(j);
		return `<article class="rtj-j${isOpen ? " is-open" : ""}" data-jn="${j.number}">
      <button type="button" class="rtj-j__head" data-jt aria-expanded="${
			isOpen ? "true" : "false"
		}">
        <span class="rtj-j__no">#${j.number}</span>
        <span class="rtj-j__t"><b>${E(j.variety || "Unknown variety")}</b><span>${E(
			sub
		)}</span></span>
        ${badge(j.stage.label, j.tone)}
        <span class="rtj-j__chev">›</span>
        ${
			j.flags.length
				? `<span class="rtj-j__flag">${E(j.flags[0].text)}${
						j.flags.length > 1 ? " +" + (j.flags.length - 1) : ""
				  }</span>`
				: ""
		}
      </button>
      <div class="rtj-j__body">
        <div class="rtj-jnow"><b>${E(j.stage.label)}</b>${
			j.stage.detail ? ": " + E(j.stage.detail) : ""
		}</div>
        ${
			bits.length
				? `<div class="rtj-bits">${bits.map((x) => `<span>${E(x)}</span>`).join("")}</div>`
				: ""
		}
        ${
			j.flags.length
				? `<div class="rtj-flags">${j.flags
						.map(
							(f) => `<div class="rtj-flag rtj-flag--${f.level}">${E(f.text)}</div>`
						)
						.join("")}</div>`
				: ""
		}
        ${
			j.steps.length
				? timeline(j.steps)
				: '<div class="ph-muted">No dated events on this journey.</div>'
		}
        ${
			j.ledger.length
				? `<details class="rtj-led"><summary>${PH.fmt.plural(
						j.ledger.length,
						"stock ledger entry",
						"stock ledger entries"
				  )}</summary>${timeline(j.ledger)}</details>`
				: ""
		}
        ${
			j.batch
				? `<div class="rtj-sum__meta">First record ${docs([
						{ doctype: "Stock Entry", name: j.batch },
				  ])}</div>`
				: ""
		}
      </div></article>`;
	}

	/* ═══ BUNCH ═════════════════════════════════════════════════════ */
	const flP = async (doctype, filters, fields, orderBy) => {
		const all = [];
		let start = 0;
		for (;;) {
			let page = [];
			try {
				page =
					(await PH.call(
						"frappe.client.get_list",
						{
							doctype,
							filters,
							fields,
							limit_page_length: 500,
							limit_start: start,
							order_by: orderBy,
						},
						{ silent: true }
					)) || [];
			} catch (e) {
				page = [];
			}
			all.push(...page);
			if (page.length < 500) break;
			start += 500;
		}
		return all;
	};
	const fgP = (doctype, name) =>
		PH.call("frappe.client.get", { doctype, name }, { silent: true }).catch(() => null);
	const wh = (v) => String(v || "").split(" - ")[0];
	const d10 = (v) =>
		String(v || "")
			.replace("T", " ")
			.split(" ")[0];

	async function trackBunch(bunchId) {
		const my = ++seq;
		showLoading();
		try {
			const gradingSEs = await flP(
				"Stock Entry",
				[
					["custom_bunch_id", "=", bunchId],
					["stock_entry_type", "=", "Grading"],
				],
				[
					"name",
					"posting_date",
					"posting_time",
					"creation",
					"farm",
					"custom_graded_by",
					"custom_bunch_id",
					"custom_stem_length",
					"custom_harvest_entry",
					"custom_bunched_by",
					"custom_scanned_grading",
					"custom_scanned_packing",
					"custom_opl_scanned",
					"to_warehouse",
					"from_warehouse",
					"docstatus",
				],
				"posting_date asc, posting_time asc"
			);
			const hNames = PH.uniq(gradingSEs.map((x) => x.custom_harvest_entry));
			const harvestSEs = hNames.length
				? await flP(
						"Stock Entry",
						[
							["name", "in", hNames],
							["stock_entry_type", "=", "Harvesting"],
						],
						[
							"name",
							"posting_date",
							"posting_time",
							"custom_bucket_id",
							"farm",
							"custom_greenhouse",
							"custom_stem_length",
							"custom_receiving_entry",
							"custom_harvester",
							"to_warehouse",
						],
						"posting_date asc"
				  )
				: [];
			const rNames = PH.uniq(harvestSEs.map((x) => x.custom_receiving_entry));
			const receivingSEs = rNames.length
				? await flP(
						"Stock Entry",
						[
							["name", "in", rNames],
							["stock_entry_type", "=", "Receiving"],
						],
						[
							"name",
							"posting_date",
							"posting_time",
							"stock_entry_type",
							"farm",
							"custom_stem_length",
							"custom_bucket_id",
							"to_warehouse",
							"from_warehouse",
						],
						"posting_date asc"
				  )
				: [];
			const bucketIds = PH.uniq(harvestSEs.map((x) => x.custom_bucket_id));
			const shelfItems = [];
			for (const bid of bucketIds) {
				for (const sh of await flP(
					"Shelf",
					[["Shelf Item", "bucket_id", "=", bid]],
					["name", "farm"]
				)) {
					const doc = await fgP("Shelf", sh.name);
					if (doc)
						(doc.items || [])
							.filter((i) => (i.bucket_id || "").toLowerCase() === bid.toLowerCase())
							.forEach((i) =>
								shelfItems.push(
									Object.assign({}, i, {
										shelf_name: doc.name,
										shelf_farm: doc.farm,
										bucket_id: bid,
									})
								)
							);
				}
			}
			const gCreation =
				(gradingSEs[0] && (gradingSEs[0].creation || gradingSEs[0].posting_date)) || null;
			const direct = PH.uniq(gradingSEs.map((x) => x.custom_opl_scanned));
			let oplNames = [];
			if (direct.length) oplNames = direct;
			else if (bucketIds.length) {
				const parents = await flP(
					"Order Pick List",
					[["Pick List Item", "bucket", "in", bucketIds]],
					["name", "creation"]
				);
				oplNames = (
					gCreation
						? parents.filter(
								(o) =>
									new Date(o.creation || 0).getTime() >=
									new Date(gCreation).getTime()
						  )
						: parents
				).map((o) => o.name);
			}
			const oplRows = [];
			if (oplNames.length) {
				const lower = bucketIds.map((b) => b.toLowerCase());
				for (const doc of await Promise.all(
					oplNames.map((n) => fgP("Order Pick List", n))
				)) {
					if (!doc) continue;
					if (
						!direct.length &&
						gCreation &&
						new Date(doc.creation || 0).getTime() < new Date(gCreation).getTime()
					)
						continue;
					(doc.locations || [])
						.filter(
							(r) =>
								!bucketIds.length || lower.includes((r.bucket || "").toLowerCase())
						)
						.forEach((r) =>
							oplRows.push(
								Object.assign({}, r, {
									parent: doc.name,
									opl_customer: doc.customer,
									opl_order_name: doc.custom_order_name,
									opl_team: doc.custom_team,
									opl_date: doc.date_created,
									opl_modified: doc.modified,
									opl_creation: doc.creation,
									opl_sales_order: doc.sales_order,
									opl_consignee: doc.custom_consignee,
								})
							)
						);
				}
			}
			const packRows = [];
			try {
				const names = PH.uniq(oplRows.map((r) => r.parent)),
					sos = PH.uniq(oplRows.map((r) => r.opl_sales_order || r.sales_order)),
					cands = [];
				if (names.length)
					cands.push(
						...(await flP(
							"Farm Pack List",
							[["order_pick_list", "in", names]],
							["name"]
						))
					);
				if (sos.length)
					cands.push(
						...(await flP("Farm Pack List", [["sales_order", "in", sos]], ["name"]))
					);
				for (const n of PH.uniq(cands.map((x) => x.name))) {
					const doc = await fgP("Farm Pack List", n);
					if (doc)
						(doc.pack_list_item || doc.items || [])
							.filter(
								(r) => (r.bunch_id || "").toLowerCase() === bunchId.toLowerCase()
							)
							.forEach((r) =>
								packRows.push(
									Object.assign({}, r, {
										pack_list_name: doc.name,
										pack_farm: doc.farm,
										pack_sales_order: doc.sales_order,
									})
								)
							);
				}
			} catch (e) {
				/* the pack list lookup is optional */
			}
			if (my !== seq) return;
			if (
				!(
					gradingSEs.length ||
					harvestSEs.length ||
					receivingSEs.length ||
					shelfItems.length ||
					oplRows.length ||
					packRows.length
				)
			) {
				PH.set($res, '<section class="ph-card"><div id="rtj-empty"></div></section>');
				return PH.empty(
					"#rtj-empty",
					"Not found",
					"No records for bunch " + bunchId + ".",
					"search"
				);
			}
			renderBunch(bunchId, {
				gradingSEs,
				harvestSEs,
				receivingSEs,
				shelfItems,
				oplRows,
				packRows,
			});
			PH.markUpdated();
		} catch (err) {
			if (my === seq)
				showError("Could not trace the bunch. " + ((err && err.message) || ""));
		}
	}

	function renderBunch(bunchId, d) {
		const { gradingSEs, harvestSEs, receivingSEs, shelfItems, oplRows, packRows } = d;
		const g = gradingSEs[0],
			h = harvestSEs[0],
			issuedRow = oplRows.find((r) => r.custom_issued == 1);
		const state = packRows.length
			? ["Packed", "ok"]
			: issuedRow
			? ["Issued", "ok"]
			: oplRows.length
			? ["On pick list", "warn"]
			: shelfItems.length
			? ["On shelf", "info"]
			: receivingSEs.length
			? ["Received", "info"]
			: gradingSEs.length
			? ["Graded", "info"]
			: ["In the field", "warn"];
		const T = (date, time) =>
			date
				? String(date).slice(0, 10) +
				  " " +
				  (String(time || "00:00:00").split(".")[0].length === 8
						? String(time).split(".")[0]
						: "00:00:00")
				: "";
		const steps = [];
		if (h)
			steps.push({
				t: T(h.posting_date, h.posting_time),
				title: "Harvested",
				where: [h.farm, wh(h.custom_greenhouse)].filter(Boolean).join(" · "),
				who: h.custom_harvester,
				tone: "ok",
				line: h.custom_bucket_id ? "bucket " + h.custom_bucket_id : "",
				flags: [],
				facts: [["Stem length", h.custom_stem_length]],
				docs: [{ doctype: "Stock Entry", name: h.name }],
			});
		gradingSEs.forEach((x) =>
			steps.push({
				t: T(x.posting_date, x.posting_time),
				title: "Graded and bunched",
				where: x.farm,
				who: x.custom_graded_by,
				tone: "info",
				line: [
					x.custom_stem_length,
					x.custom_bunched_by && "bunched by " + x.custom_bunched_by,
				]
					.filter(Boolean)
					.join(" · "),
				flags: [],
				facts: [
					["From", wh(x.from_warehouse)],
					["To", wh(x.to_warehouse)],
					["Packing scanned", x.custom_scanned_packing ? "Yes" : "No"],
				],
				docs: [{ doctype: "Stock Entry", name: x.name }],
			})
		);
		receivingSEs.forEach((x) =>
			steps.push({
				t: T(x.posting_date, x.posting_time),
				title: "Received",
				where: wh(x.to_warehouse),
				tone: "ok",
				line: x.custom_stem_length || "",
				flags: [],
				facts: [
					["From", wh(x.from_warehouse)],
					["Bucket", x.custom_bucket_id],
					["Farm", x.farm],
				],
				docs: [{ doctype: "Stock Entry", name: x.name }],
			})
		);
		shelfItems.forEach((x) =>
			steps.push({
				t: String(x.date_added || "")
					.replace("T", " ")
					.slice(0, 19),
				title: "Shelved",
				where: [x.shelf_name, x.shelf_farm].filter(Boolean).join(" · "),
				tone: "ok",
				line: [x.stem_qty && x.stem_qty + " stems", x.variety, x.stem_length]
					.filter(Boolean)
					.join(" · "),
				flags: [],
				facts: [["Bucket", x.bucket_id]],
				docs: [],
			})
		);
		for (const o of [...new Map(oplRows.map((r) => [r.parent, r])).values()]) {
			const iss = o.custom_issued == 1;
			steps.push({
				t: String(o.opl_creation || o.opl_date || "")
					.replace("T", " ")
					.slice(0, 19),
				title: "Picked for " + (o.opl_order_name || o.parent),
				where: o.custom_shelf,
				tone: iss ? "ok" : "info",
				line: [o.opl_customer, iss ? "issued" : "not issued"].filter(Boolean).join(" · "),
				flags: [],
				facts: [
					["Sales order", o.opl_sales_order || o.sales_order],
					["Team", o.opl_team],
					["Box", o.custom_box_id],
					["Stems", o.stock_qty],
				],
				docs: [{ doctype: "Order Pick List", name: o.parent }],
			});
		}
		for (const p of [...new Map(packRows.map((r) => [r.pack_list_name, r])).values()])
			steps.push({
				t: String(p.creation || p.modified || "")
					.replace("T", " ")
					.slice(0, 19),
				title: "Packed into box",
				where: p.pack_farm,
				tone: "ok",
				line: [
					p.box_id && "box " + p.box_id,
					p.item_code,
					p.custom_number_of_stems && p.custom_number_of_stems + " stems",
				]
					.filter(Boolean)
					.join(" · "),
				flags: [],
				facts: [
					["Sales order", p.pack_sales_order],
					["Stem length", p.stem_length],
					["Bunch UOM", p.bunch_uom],
				],
				docs: [{ doctype: "Farm Pack List", name: p.pack_list_name }],
			});
		const ok = steps.filter((s) => s.t).sort((a, b) => a.t.localeCompare(b.t));
		ok.forEach((s, i) => {
			if (i) {
				const a = new Date(ok[i - 1].t.replace(" ", "T")),
					b = new Date(s.t.replace(" ", "T"));
				s.gap_h = Math.max(0, (b - a) / 36e5);
			}
		});
		const linked = PH.uniq(harvestSEs.map((x) => x.custom_bucket_id));
		PH.set(
			$res,
			`<section class="ph-card"><div class="rtj-sum"><div>
        <div class="ph-hero__eyebrow">Bunch</div><h2 class="rtj-sum__id">${E(
			bunchId.toUpperCase()
		)}</h2>
        <div class="rtj-ph">${badge(state[0], state[1])}</div>
        <div class="rtj-sum__facts"><div><small>Farm</small><b>${E(
			(g && g.farm) || (h && h.farm) || "—"
		)}</b></div><div><small>Stem length</small><b>${E(
				(g && g.custom_stem_length) || "—"
			)}</b></div><div><small>Graded</small><b>${E(
				g ? PH.fmt.date(g.posting_date) : "—"
			)}</b></div></div>
        ${
			linked.length
				? `<div class="rtj-ph" style="margin-top:14px">${linked
						.map(
							(b) =>
								`<button type="button" class="ph-btn ph-btn--sm" data-track="${E(
									b
								)}">Open bucket ${E(b)}</button>`
						)
						.join("")}</div>`
				: ""
		}
      </div></div></section>
      <section class="ph-card">${
			ok.length ? timeline(ok) : '<div class="ph-muted">No dated events.</div>'
		}</section>`
		);
	}

	/* ═══ CORRECT DETAILS (writes through the existing Frappe client, as v1) ═══ */
	function currentJourney() {
		if (!G) return null;
		const open = $res.querySelector(".rtj-j.is-open");
		const n = open ? +open.dataset.jn : (G.journeys[G.journeys.length - 1] || {}).number;
		return G.journeys.find((j) => j.number === n) || null;
	}
	function openCorrection() {
		const j = currentJourney();
		if (!j) return PH.toast("Open a journey first.", "warn");
		const rows = j.fix.map(
			(r) =>
				`<div class="ph-list__row"><div style="min-width:0"><div class="ph-list__name">${E(
					r.label || r.name
				)}</div><div class="ph-list__meta">${E(
					r.type
				)}</div></div><div class="ph-list__qty"><small>current</small>${E(
					r.current
				)}</div></div>`
		);
		const box = document.createElement("div");
		box.innerHTML = `<div class="ph-notice ph-notice--warn"><div><b>Bulk correction</b>: updates stem length and/or variety on the documents of this journey (harvest, grading, receiving, live shelf items, allocation). Only fill what you want to change.</div></div>
      <p class="ph-muted ph-mb">${E(
			"Bucket " +
				G.bucket.id +
				" · journey #" +
				j.number +
				" · " +
				(j.variety || "Unknown") +
				" · " +
				PH.fmt.date(j.start_date)
		)}</p>
      <div class="ph-form ph-form--2"><div class="ph-field"><label for="rtj-csl">Correct stem length</label><input id="rtj-csl" class="ph-control" placeholder="e.g. 50cm"><div class="ph-field__hint">Current: ${E(
			j.stem_length || "—"
		)}</div></div>
      <div class="ph-field"><label for="rtj-cvar">Correct variety</label><input id="rtj-cvar" class="ph-control" placeholder="Start typing a variety"><div class="ph-field__hint">Current: ${E(
			j.variety || "—"
		)}</div></div>
      <div class="ph-field__error ph-field--full" id="rtj-cerr" hidden></div></div>
      <h3 class="ph-section-title">Documents to be updated</h3>${
			rows.length
				? `<div class="ph-list ph-list--plain">${rows.join("")}</div>`
				: '<p class="ph-muted">None.</p>'
		}
      <div class="ph-list ph-list--plain ph-mt" id="rtj-clog"></div>`;
		const $sl = box.querySelector("#rtj-csl"),
			$var = box.querySelector("#rtj-cvar");
		PH.autocomplete($var, {
			minChars: 2,
			source: (q) =>
				PH.call(
					"frappe.client.get_list",
					{
						doctype: "Item",
						filters: [["item_name", "like", "%" + q + "%"]],
						fields: ["name", "item_name"],
						limit_page_length: 15,
						order_by: "item_name asc",
					},
					{ silent: true }
				).then((r) =>
					(r || []).map((x) => ({
						value: x.name,
						label: x.item_name || x.name,
						description: x.item_name && x.item_name !== x.name ? x.name : "",
					}))
				),
		});
		let applied = false;
		const m = PH.modal({
			title: "Correct bucket details",
			size: "lg",
			body: box,
			actions: [
				{ label: "Cancel" },
				{
					label: "Apply corrections",
					primary: true,
					onClick: () =>
						applied
							? true
							: applyCorrection(j, $sl.value.trim(), $var.value.trim(), box, m).then(
									(ran) => {
										if (ran) applied = true;
										return false;
									}
							  ),
				},
			],
		});
	}
	async function applyCorrection(j, newLen, newVar, box, m) {
		const err = box.querySelector("#rtj-cerr");
		if (!newLen && !newVar) {
			err.textContent = "Fill at least one field.";
			err.hidden = false;
			return;
		}
		err.hidden = true;
		const log = box.querySelector("#rtj-clog");
		let ok = 0,
			fail = 0;
		const add = (msg, tone) =>
			log.insertAdjacentHTML(
				"beforeend",
				`<div class="ph-list__row"><div class="ph-list__name">${E(msg)}</div>${badge(
					tone === "ok" ? "Updated" : tone === "fail" ? "Failed" : "Skipped",
					tone === "ok" ? "ok" : tone === "fail" ? "bad" : ""
				)}</div>`
			);
		const set = (doctype, name, field, value) =>
			PH.call(
				"frappe.client.set_value",
				{ doctype, name, fieldname: field, value },
				{ silent: true }
			);
		for (const r of j.fix) {
			const label = r.type + " " + (r.label || r.name);
			try {
				if (r.doctype === "Stock Entry") {
					if (!newLen) {
						add(label + " (stem length only)", "skip");
						continue;
					}
					await set("Stock Entry", r.name, "custom_stem_length", newLen);
				} else if (r.doctype === "Shelf Item") {
					if (newLen) await set("Shelf Item", r.name, "stem_length", newLen);
					if (newVar) await set("Shelf Item", r.name, "variety", newVar);
				} else if (r.doctype === "Bucket Allocation Status") {
					if (newLen) await set(r.doctype, r.name, "stem_length", newLen);
					if (newVar) await set(r.doctype, r.name, "item_code", newVar);
				} else {
					add(label + " (history)", "skip");
					continue;
				}
				add(label, "ok");
				ok++;
			} catch (e) {
				add(label + ": " + (e.message || e), "fail");
				fail++;
			}
		}
		setTimeout(() => {
			m.close();
			track(true);
		}, 2000);
		PH.toast(
			fail === 0
				? "Done: " + PH.fmt.plural(ok, "document") + " updated."
				: "Finished with " + PH.fmt.plural(fail, "error") + ". " + ok + " updated.",
			fail ? "bad" : "good"
		);
		return true;
	}

	/* ═══ BROWSE: buckets in use · shelves · missing ═══════════════ */
	let overview = null;
	function loadOverview() {
		return PH.call(
			"upande_packhouse.api.v2.remote_transfer.get_bucket_overview",
			{ region: st.get("region") },
			{ silent: true }
		)
			.then((o) => {
				overview = o;
				const b = o.buckets,
					s = o.shelves;
				PH.set(
					"#rtj-browse",
					`<button type="button" class="ph-btn ph-btn--sm ph-btn--ghost" data-browse="buckets">Buckets in use · ${PH.fmt.num(
						b.in_use
					)}</button>
        <button type="button" class="ph-btn ph-btn--sm ph-btn--ghost" data-browse="shelves">Shelves · ${PH.fmt.num(
			s.with_buckets
		)} of ${PH.fmt.num(s.total)} with buckets</button>
        <button type="button" class="ph-btn ph-btn--sm ph-btn--ghost" data-browse="missing">Missing buckets · ${PH.fmt.num(
			o.missing || 0
		)}</button>
        <span class="ph-muted" style="font-size:12px">${PH.fmt.num(b.empty)} empty${
						o.scoped ? " (all regions)" : ""
					}</span>`
				);
			})
			.catch(() => PH.set("#rtj-browse", ""));
	}
	document.getElementById("rtj-browse").addEventListener("click", (e) => {
		const t = e.target.closest("[data-browse]");
		if (!t) return;
		if (t.dataset.browse === "buckets") openBuckets("all");
		else if (t.dataset.browse === "shelves") openShelves(null, "all");
		else openMissing();
	});

	const D = {
		mode: "shelves",
		farm: null,
		view: "all",
		kind: "all",
		rows: [],
		m: null,
		loaded: false,
		box: null,
	};
	function openDrawer(mode, title) {
		D.mode = mode;
		D.rows = [];
		D.loaded = false;
		const box = document.createElement("div");
		box.innerHTML = `<div class="rtj-drawer-tools"><div class="ph-pills" id="rtj-dchips"></div>
      <label class="ph-search"><svg class="ph-ic" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg><input id="rtj-dq" type="search" placeholder="${
			mode === "shelves" ? "Shelf, bucket or variety" : "Bucket, variety, place"
		}" autocomplete="off"></label></div><div id="rtj-dbody"></div>`;
		D.m = PH.modal({ title, sub: " ", body: box, drawer: true, size: "lg" });
		D.box = box;
		box.querySelector("#rtj-dq").addEventListener("input", PH.debounce(drawerRender, 150));
		box.addEventListener("click", drawerClick);
		drawerChips();
		PH.skeleton(box.querySelector("#rtj-dbody"), "rows", 6);
	}
	function drawerChips() {
		const el = D.box.querySelector("#rtj-dchips");
		if (D.mode === "missing") {
			el.hidden = true;
			return;
		}
		const opts =
			D.mode === "shelves"
				? [
						["all", "All"],
						["occupied", "With buckets"],
						["free", "Free"],
				  ]
				: [
						["all", "All"],
						["shelf", "On shelves"],
						["truck", "On trucks"],
						["harvesting", "Harvesting"],
				  ];
		const cur = D.mode === "shelves" ? D.view : D.kind;
		el.innerHTML = opts
			.map(
				([k, l]) =>
					`<button type="button" data-chip="${k}" class="${
						k === cur ? "is-on" : ""
					}">${l}</button>`
			)
			.join("");
	}
	const drawerFail = (text) => PH.error(D.box.querySelector("#rtj-dbody"), text);
	function openShelves(farm, view) {
		D.farm = farm;
		D.view = view || "all";
		openDrawer("shelves", farm === null ? "All shelves" : farm || "No farm");
		const my = D.m;
		PH.call(
			"upande_packhouse.api.bucket_journey.getFarmShelves",
			farm === null ? { view: "all" } : { farm, view: "all" },
			{ silent: true }
		)
			.then((r) => {
				if (D.m !== my) return;
				const rf = PH.regionFarms(st.get("region"));
				D.rows = (r || []).filter((x) => !rf || rf.indexOf(x.farm) >= 0);
				D.loaded = true;
				drawerRender();
			})
			.catch((e) => D.m === my && drawerFail("Could not load shelves. " + e.message));
	}
	function openBuckets(kind) {
		D.kind = kind || "all";
		openDrawer("buckets", "Buckets in use");
		const my = D.m;
		PH.call(
			"upande_packhouse.api.v2.remote_transfer.get_buckets_in_use",
			{ kind: "all", region: st.get("region") },
			{ silent: true }
		)
			.then((r) => {
				if (D.m !== my) return;
				D.rows = r || [];
				D.loaded = true;
				drawerRender();
			})
			.catch((e) => D.m === my && drawerFail("Could not load buckets. " + e.message));
	}
	function openMissing() {
		openDrawer("missing", "Missing buckets");
		const my = D.m;
		PH.call("upande_packhouse.api.bucket_journey.getMissingBuckets", {}, { silent: true })
			.then((r) => {
				if (D.m !== my) return;
				const rf = PH.regionFarms(st.get("region"));
				D.rows = (r || []).filter((x) => !rf || rf.indexOf(x.farm) >= 0);
				D.loaded = true;
				drawerRender();
			})
			.catch(
				(e) => D.m === my && drawerFail("Could not load missing buckets. " + e.message)
			);
	}
	const ago = (d) => {
		if (!d) return "";
		const days = Math.floor(
			(Date.now() - new Date(String(d).replace(" ", "T")).getTime()) / 864e5
		);
		return days <= 0 ? "today" : days === 1 ? "1 day" : days + " days";
	};
	const trackBtn = (id) =>
		`<button type="button" class="ph-link ph-mono rtj-idbtn" data-track="${E(id || "")}">${E(
			id || ""
		)}</button>`;
	function drawerRender() {
		if (!D.loaded) return;
		const body = D.box.querySelector("#rtj-dbody");
		const term = (D.box.querySelector("#rtj-dq").value || "").trim().toLowerCase();
		const has = (s) =>
			!term ||
			String(s || "")
				.toLowerCase()
				.includes(term);
		const sub = D.m.el.querySelector(".ph-modal__sub");
		if (D.mode === "missing") {
			const list = D.rows.filter(
				(r) =>
					has(r.old_bucket) ||
					has(r.new_bucket) ||
					has(r.order_name) ||
					has(r.farm) ||
					has(r.item_code)
			);
			sub.textContent = PH.fmt.plural(list.length, "bucket");
			if (!list.length)
				return PH.empty(
					body,
					"No missing buckets",
					term ? "Nothing matches." : "Every replaced bucket is accounted for."
				);
			return PH.set(
				body,
				list
					.map(
						(
							r
						) => `<div class="rtj-shelf"><div class="ph-flex ph-flex--between ph-flex--wrap">${trackBtn(
							r.old_bucket
						)}${badge(r.reason || "Missing", "bad")}</div>
        <div class="ph-mt">${replLine(
			Object.assign({}, r, { status: "Open" }),
			r.old_bucket
		)}</div>
        <div class="ph-flex ph-flex--wrap ph-mt">${["Found", "Discarded", "Written Off"]
			.map(
				(s) =>
					`<button type="button" class="ph-btn ph-btn--sm" data-resolve="${E(
						r.name
					)}" data-status="${s}">${s}</button>`
			)
			.join("")}</div></div>`
					)
					.join("")
			);
		}
		if (D.mode === "shelves") {
			const list = D.rows.filter(
				(s) =>
					(D.view === "all" ||
						(D.view === "free" ? !s.buckets.length : s.buckets.length)) &&
					(has(s.shelf) ||
						s.buckets.some(
							(b) => has(b.bucket) || b.contents.some((c) => has(c.variety))
						))
			);
			sub.textContent =
				PH.fmt.plural(list.length, "shelf", "shelves") +
				" · " +
				PH.fmt.plural(
					list.reduce((n, s) => n + s.buckets.length, 0),
					"bucket"
				);
			if (!list.length)
				return PH.empty(
					body,
					"No shelves",
					term ? "Nothing matches." : "No shelves in this view."
				);
			return PH.set(
				body,
				list
					.map(
						(
							s
						) => `<div class="rtj-shelf"><div class="ph-flex ph-flex--between ph-flex--wrap"><b class="ph-strong ph-mono">${E(
							s.shelf
						)}${D.farm === null && s.farm ? " · " + E(s.farm) : ""}</b>${
							s.buckets.length
								? badge(PH.fmt.plural(s.buckets.length, "bucket"), "")
								: badge("Free", "ok")
						}</div>
        ${s.buckets
			.map((b) => {
				const c0 = b.contents[0] || {};
				return `<div class="rtj-bkt"><div class="ph-flex ph-flex--between">${trackBtn(
					b.bucket
				)}<span class="ph-strong">${PH.fmt.num(b.stems)} stems</span></div>
          ${b.contents
				.map(
					(c) =>
						`<div class="ph-flex ph-flex--between"><span>${E(
							c.variety || "—"
						)}</span><span class="ph-muted">${E(c.stem_length || "—")} · ${PH.fmt.num(
							c.stem_qty
						)}</span></div>`
				)
				.join("")}
          <div class="ph-muted" style="font-size:12px">${E(
				[
					c0.greenhouse,
					c0.harvest_date && "harvested " + c0.harvest_date,
					b.date_added && "on shelf " + ago(b.date_added),
					c0.cut_stage,
				]
					.filter(Boolean)
					.join(" · ")
			)}</div></div>`;
			})
			.join("")}</div>`
					)
					.join("")
			);
		}
		const list = D.rows.filter(
			(r) =>
				(D.kind === "all" || r.where === D.kind) &&
				(has(r.bucket) || has(r.variety) || has(r.shelf) || has(r.truck) || has(r.farm))
		);
		sub.textContent = PH.fmt.plural(list.length, "bucket");
		if (!list.length)
			return PH.empty(
				body,
				"No buckets",
				term ? "Nothing matches." : "No buckets in this view."
			);
		const WHERE = {
			shelf: ["On shelf", ""],
			truck: ["On truck", "info"],
			harvesting: ["Harvesting", "warn"],
		};
		const place = (r) =>
			(r.where === "shelf"
				? [r.shelf, r.farm]
				: r.where === "truck"
				? [r.truck, r.opl]
				: [r.farm, r.greenhouse]
			)
				.filter(Boolean)
				.join(" · ");
		PH.set(
			body,
			list
				.map(
					(
						r
					) => `<div class="rtj-shelf"><div class="ph-flex ph-flex--between ph-flex--wrap">${trackBtn(
						r.bucket
					)}${badge((WHERE[r.where] || [r.where])[0], (WHERE[r.where] || [])[1])}</div>
      <div class="ph-flex ph-flex--between ph-mt"><span>${E(
			r.variety || "—"
		)} <span class="ph-muted">${E(
						r.stem_length || ""
					)}</span></span><span class="ph-strong">${PH.fmt.num(
						r.stems
					)} stems</span></div>
      <div class="ph-muted" style="font-size:12px">${E(
			place(r) + (r.since ? " · " + ago(r.since) : "")
		)}</div></div>`
				)
				.join("")
		);
	}
	function drawerClick(e) {
		const chip = e.target.closest("[data-chip]");
		if (chip) {
			if (D.mode === "shelves") D.view = chip.dataset.chip;
			else D.kind = chip.dataset.chip;
			drawerChips();
			return drawerRender();
		}
		const t = e.target.closest("[data-track]");
		if (t) {
			D.m.close();
			go(t.dataset.track, "bucket");
			const main = document.querySelector(".ph-main");
			if (main) main.scrollTo({ top: 0, behavior: "smooth" });
			return;
		}
		const r = e.target.closest("[data-resolve]");
		if (r) {
			r.disabled = true;
			PH.call("upande_packhouse.api.bucket_replacement.resolveBucketReplacement", {
				name: r.dataset.resolve,
				status: r.dataset.status,
			})
				.then(() => {
					PH.toast("Marked " + r.dataset.status, "good");
					D.rows = D.rows.filter((x) => x.name !== r.dataset.resolve);
					drawerRender();
					loadOverview();
				})
				.catch(() => {
					r.disabled = false;
				});
		}
	}

	/* ── Clicks inside the result ────────────────────────────────── */
	$res.addEventListener("click", (e) => {
		const more = e.target.closest("[data-more]");
		if (more) {
			const li = more.closest(".rtj-s");
			li.classList.toggle("is-open");
			more.textContent = li.classList.contains("is-open") ? "Hide details" : "Details";
			return;
		}
		const jt = e.target.closest("[data-jt]");
		if (jt) {
			const a = jt.closest(".rtj-j");
			a.classList.toggle("is-open");
			jt.setAttribute("aria-expanded", a.classList.contains("is-open"));
			paintFlow();
			return;
		}
		if (e.target.closest("#rtj-correct")) return openCorrection();
		if (e.target.closest("#rtj-reset")) return st.set({ from: "", to: "", limit: "5" });
		const tb = e.target.closest("[data-track]");
		if (tb) return go(tb.dataset.track, "bucket");
	});
	$res.addEventListener("change", (e) => {
		if (e.target.id === "rtj-limit") st.set({ limit: e.target.value });
		else if (e.target.id === "rtj-from" || e.target.id === "rtj-to") {
			let a = document.getElementById("rtj-from").value,
				b = document.getElementById("rtj-to").value;
			if (a && b && a > b) [a, b] = [b, a];
			st.set({ from: a, to: b });
		}
	});
	$chips.addEventListener("click", (e) => {
		const t = e.target.closest("[data-track]");
		if (t) go(t.dataset.track, "bucket");
	});

	window.RTJ_OPEN = (id) => go(id, "bucket"); // used by the Watchlist / Inactive views (rt-watch-v2.js)

	/* ── Boot ────────────────────────────────────────────────────── */
	$q.value = st.get("q");
	$q.placeholder =
		st.get("mode") === "bunch" ? "Scan or type a bunch ID" : "Scan or type a bucket ID";
	PH.onRefresh(() => Promise.all([loadOverview(), track(true)]));
	PH.ready(() => {
		loadOverview();
		if (!st.get("q")) loadChips();
		track();
	});
})();
