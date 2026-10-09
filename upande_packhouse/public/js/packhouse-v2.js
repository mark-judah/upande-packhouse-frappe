/* Copyright (c) 2026, Upande and contributors
 *
 * Packhouse v2 — shared page runtime (window.PH).
 *
 * One implementation of everything the packhouse pages used to re-write
 * per page: waiting for Frappe, server calls, formatting, URL-synced
 * filters, tables, modals, toasts, skeletons, charts, CSV export.
 * Pages must use these instead of rolling their own.
 *
 * Contract and examples: templates/includes/packhouse_v2/README.md
 */
(function () {
	"use strict";
	if (window.PH) return;

	const PH = {};
	window.PH = PH;

	/* ── Frappe readiness & server calls ───────────────────────────── */

	/** Run fn once frappe.call exists (website pages load it late). */
	PH.ready = function (fn) {
		const go = () => {
			if (window.frappe && frappe.call) return fn();
			setTimeout(go, 30);
		};
		if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", go);
		else go();
	};
	const frappeReady = new Promise((res) => PH.ready(res));

	/**
	 * Call a whitelisted method. Resolves with r.message.
	 * Rejects (and toasts, unless opts.silent) on transport/server error,
	 * or when the method returns {success:false, ...}.
	 * opts: { silent, freeze(false), type:'GET'|'POST' }
	 */
	PH.call = function (method, args, opts) {
		opts = opts || {};
		return frappeReady.then(
			() =>
				new Promise((resolve, reject) => {
					let settled = false;
					const fail = (r) => {
						if (settled) return;
						settled = true;
						const msg = PH.serverError(r) || (r && r.status === 403 ? "You don't have permission for this" : "Could not reach the server");
						if (!opts.silent) PH.toast(msg, "bad");
						reject(new Error(msg));
					};
					const req = frappe.call({
						method,
						args: args || {},
						type: opts.type || "POST",
						freeze: !!opts.freeze,
						callback: (r) => {
							if (settled) return;
							settled = true;
							const m = r ? r.message : undefined;
							if (m && typeof m === "object" && m.success === false) {
								const err = new Error(m.error || m.message || "Request failed");
								err.response = m;
								if (!opts.silent) PH.toast(err.message, "bad");
								return reject(err);
							}
							resolve(m);
						},
						error: fail,
					});
					// Website-side frappe.call doesn't always invoke `error` (e.g. 403); the
					// returned jqXHR always settles, so it is the safety net.
					if (req && typeof req.fail === "function") req.fail((xhr) => fail((xhr && xhr.responseJSON) || xhr));
				})
		);
	};

	/** Best-effort human message out of a Frappe error response. */
	PH.serverError = function (r) {
		try {
			if (r && r._server_messages) {
				const msgs = JSON.parse(r._server_messages).map((m) => {
					try {
						return JSON.parse(m).message;
					} catch (e) {
						return m;
					}
				});
				return PH.stripHtml(msgs.join(" "));
			}
			if (r && r.exception) return PH.stripHtml(String(r.exception).split(":").slice(1).join(":").trim());
			if (r && r.responseJSON) return PH.serverError(r.responseJSON);
		} catch (e) {}
		return "";
	};

	/** frappe.client.get_list shorthand. */
	PH.getList = function (doctype, opts) {
		opts = opts || {};
		return PH.call(
			"frappe.client.get_list",
			{
				doctype,
				fields: opts.fields || ["name"],
				filters: opts.filters || {},
				order_by: opts.order_by,
				limit_page_length: opts.limit == null ? 0 : opts.limit,
			},
			{ silent: opts.silent, type: "GET" }
		);
	};

	/* ── Escaping & HTML ───────────────────────────────────────────── */

	PH.esc = function (s) {
		return String(s == null ? "" : s)
			.replace(/&/g, "&amp;")
			.replace(/</g, "&lt;")
			.replace(/>/g, "&gt;")
			.replace(/"/g, "&quot;")
			.replace(/'/g, "&#39;");
	};
	PH.stripHtml = function (s) {
		const d = document.createElement("div");
		d.innerHTML = String(s || "");
		return d.textContent || "";
	};
	/** Tagged template that escapes every interpolation unless wrapped in PH.raw(). */
	PH.html = function (strings, ...vals) {
		let out = strings[0];
		vals.forEach((v, i) => {
			if (Array.isArray(v)) out += v.map((x) => (x && x.__raw != null ? x.__raw : PH.esc(x))).join("");
			else if (v && v.__raw != null) out += v.__raw;
			else out += PH.esc(v == null || v === false ? "" : v);
			out += strings[i + 1];
		});
		return { __raw: out, toString: () => out };
	};
	PH.raw = (s) => ({ __raw: String(s == null ? "" : s), toString: () => String(s == null ? "" : s) });
	/** Set innerHTML from a string or PH.html result. */
	PH.set = function (el, html) {
		el = PH.$(el);
		if (el) el.innerHTML = html && html.__raw != null ? html.__raw : String(html == null ? "" : html);
		return el;
	};
	PH.$ = (sel, root) => (typeof sel === "string" ? (root || document).querySelector(sel) : sel);
	PH.$$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
	PH.text = function (sel, v) {
		const el = PH.$(sel);
		if (el) el.textContent = v == null ? "" : v;
	};

	/* ── Icons (stroke icons, 24px grid) ───────────────────────────── */
	const ICONS = {
		refresh: '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
		search: '<circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
		close: '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
		download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
		plus: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
		external: '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>',
		box: '<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/>',
		inbox: '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
		alert: '<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>',
		check: '<polyline points="20 6 9 17 4 12"/>',
		edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>',
		trash: '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
		more: '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
		chevronDown: '<polyline points="6 9 12 15 18 9"/>',
		chevronRight: '<polyline points="9 18 15 12 9 6"/>',
		chevronLeft: '<polyline points="15 18 9 12 15 6"/>',
		calendar: '<rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>',
		filter: '<polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/>',
		print: '<polyline points="6 9 6 2 18 2 18 9"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/>',
		truck: '<path d="M10 17h4V5H2v12h3"/><path d="M20 17h2v-3.34a4 4 0 0 0-1.17-2.83L19 9h-5v8h1"/><circle cx="7.5" cy="17.5" r="2.5"/><circle cx="17.5" cy="17.5" r="2.5"/>',
		save: '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/>',
		copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
		upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>',
		menu: '<line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/>',
		sidebar: '<rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="3" x2="9" y2="21"/>',
		arrowRight: '<line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/>',
		info: '<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>',
	};
	PH.icon = function (name, size) {
		const s = size || 16;
		return `<svg class="ph-ic" width="${s}" height="${s}" style="width:${s}px;height:${s}px" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ""}</svg>`;
	};
	PH.ICONS = ICONS;

	/* ── Formatting ────────────────────────────────────────────────── */

	const nfCache = {};
	function nf(dp) {
		const k = String(dp);
		if (!nfCache[k]) nfCache[k] = new Intl.NumberFormat("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
		return nfCache[k];
	}
	PH.fmt = {
		/** 12,345 — dp decimals (default 0). null/'' -> '—' */
		num(v, dp) {
			if (v === null || v === undefined || v === "" || isNaN(+v)) return "—";
			return nf(dp || 0).format(+v);
		},
		/** Like num, but drops trailing decimals when they are zero. */
		qty(v, dp) {
			if (v === null || v === undefined || v === "" || isNaN(+v)) return "—";
			const n = +v;
			return Number.isInteger(n) ? nf(0).format(n) : nf(dp == null ? 2 : dp).format(n);
		},
		/** 12.3k / 1.2M */
		compact(v) {
			if (v === null || v === undefined || isNaN(+v)) return "—";
			const n = +v,
				a = Math.abs(n);
			if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e7 ? 0 : 1).replace(/\.0$/, "") + "M";
			if (a >= 1e4) return (n / 1e3).toFixed(a >= 1e5 ? 0 : 1).replace(/\.0$/, "") + "k";
			return nf(0).format(n);
		},
		/** 45% (value already a percentage). dp default 0. */
		pct(v, dp) {
			if (v === null || v === undefined || isNaN(+v)) return "—";
			return nf(dp || 0).format(+v) + "%";
		},
		/** Money with currency code prefix: "KES 12,000.00" */
		money(v, currency, dp) {
			if (v === null || v === undefined || isNaN(+v)) return "—";
			return (currency ? currency + " " : "") + nf(dp == null ? 2 : dp).format(+v);
		},
		/** '2026-10-06' -> '6 Oct 2026' (or '6 Oct' with short=true) */
		date(v, short) {
			const d = PH.date.parse(v);
			if (!d) return v ? String(v) : "—";
			const o = { day: "numeric", month: "short" };
			if (!short) o.year = "numeric";
			return d.toLocaleDateString("en-GB", o);
		},
		/** '2026-10-06 14:05:00' -> '6 Oct, 14:05' */
		datetime(v) {
			if (!v) return "—";
			const d = new Date(String(v).replace(" ", "T"));
			if (isNaN(d)) return String(v);
			return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" }) + ", " + d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
		},
		time(v) {
			if (!v) return "—";
			const d = v instanceof Date ? v : new Date(String(v).replace(" ", "T"));
			if (isNaN(d)) return String(v).slice(0, 5);
			return d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
		},
		/** '3 min ago' */
		ago(v) {
			const d = v instanceof Date ? v : new Date(String(v).replace(" ", "T"));
			if (isNaN(d)) return "";
			const s = Math.round((Date.now() - d.getTime()) / 1000);
			if (s < 45) return "just now";
			if (s < 3600) return Math.round(s / 60) + " min ago";
			if (s < 86400) return Math.round(s / 3600) + " h ago";
			return Math.round(s / 86400) + " d ago";
		},
		plural(n, one, many) {
			return PH.fmt.num(n) + " " + (+n === 1 ? one : many || one + "s");
		},
	};

	/* ── Dates (all ISO yyyy-mm-dd, local time) ────────────────────── */
	PH.date = {
		iso(d) {
			d = d || new Date();
			return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
		},
		today() {
			return PH.date.iso(new Date());
		},
		parse(v) {
			if (!v) return null;
			if (v instanceof Date) return v;
			const m = String(v).match(/^(\d{4})-(\d{2})-(\d{2})/);
			if (!m) return null;
			return new Date(+m[1], +m[2] - 1, +m[3]);
		},
		add(iso, days) {
			const d = PH.date.parse(iso) || new Date();
			d.setDate(d.getDate() + days);
			return PH.date.iso(d);
		},
		diff(a, b) {
			return Math.round((PH.date.parse(b) - PH.date.parse(a)) / 86400000);
		},
		/** Standard presets used by every page's date filter. */
		preset(key) {
			const t = PH.date.today();
			switch (key) {
				case "today":
					return [t, t];
				case "yesterday":
					return [PH.date.add(t, -1), PH.date.add(t, -1)];
				case "tomorrow":
					return [PH.date.add(t, 1), PH.date.add(t, 1)];
				case "7d":
					return [PH.date.add(t, -6), t];
				case "14d":
					return [PH.date.add(t, -13), t];
				case "30d":
					return [PH.date.add(t, -29), t];
				case "next7":
					return [t, PH.date.add(t, 6)];
				case "mtd": {
					const d = new Date();
					return [PH.date.iso(new Date(d.getFullYear(), d.getMonth(), 1)), t];
				}
				default:
					return null;
			}
		},
		/** Which preset (if any) a from/to pair matches. */
		presetOf(from, to, keys) {
			return (keys || ["today", "yesterday", "tomorrow", "7d", "14d", "30d", "next7", "mtd"]).find((k) => {
				const p = PH.date.preset(k);
				return p && p[0] === from && p[1] === to;
			});
		},
		label(from, to) {
			if (!from && !to) return "All dates";
			if (from === to) return PH.fmt.date(from);
			return PH.fmt.date(from, true) + " → " + PH.fmt.date(to, true);
		},
	};

	/* ── URL-synced page state (filters survive refresh & are shareable) ─ */
	/**
	 * const st = PH.state({ from: today, to: today, team: '' });
	 * st.get('team'); st.set({team:'A'}); st.on(fn(state, changedKeys))
	 * Values equal to the default are kept out of the URL.
	 */
	PH.state = function (defaults) {
		const params = new URLSearchParams(location.search);
		const s = {};
		Object.keys(defaults).forEach((k) => {
			s[k] = params.has(k) ? params.get(k) : defaults[k];
			if (typeof defaults[k] === "number" && params.has(k)) s[k] = +s[k];
			if (typeof defaults[k] === "boolean" && params.has(k)) s[k] = s[k] === "1" || s[k] === "true";
		});
		const subs = [];
		function writeUrl() {
			const p = new URLSearchParams(location.search);
			Object.keys(defaults).forEach((k) => {
				const v = s[k];
				if (v === defaults[k] || v === "" || v == null) p.delete(k);
				else p.set(k, typeof v === "boolean" ? (v ? "1" : "0") : v);
			});
			const q = p.toString();
			history.replaceState(history.state, "", location.pathname + (q ? "?" + q : "") + location.hash);
		}
		return {
			get: (k) => (k ? s[k] : Object.assign({}, s)),
			set(patch, opts) {
				const changed = Object.keys(patch).filter((k) => s[k] !== patch[k]);
				if (!changed.length) return;
				changed.forEach((k) => (s[k] = patch[k]));
				writeUrl();
				if (!(opts && opts.silent)) subs.forEach((fn) => fn(Object.assign({}, s), changed));
			},
			reset() {
				this.set(Object.assign({}, defaults));
			},
			on(fn) {
				subs.push(fn);
			},
			defaults: Object.assign({}, defaults),
		};
	};

	/* ── Filter controls (bind once; they keep themselves in sync) ──── */

	/**
	 * Wire a .ph-pills group. Buttons carry data-value.
	 * PH.pills('#rose', st, 'rose')  — binds to a state key; or
	 * PH.pills('#rose', value, onChange)
	 */
	PH.pills = function (sel, stOrValue, keyOrOnChange) {
		const el = PH.$(sel);
		if (!el) return;
		const bound = stOrValue && typeof stOrValue.get === "function";
		const paint = (v) => PH.$$("button", el).forEach((b) => b.classList.toggle("is-on", b.dataset.value === String(v)));
		paint(bound ? stOrValue.get(keyOrOnChange) : stOrValue);
		el.addEventListener("click", (e) => {
			const b = e.target.closest("button[data-value]");
			if (!b) return;
			paint(b.dataset.value);
			if (bound) stOrValue.set({ [keyOrOnChange]: b.dataset.value });
			else keyOrOnChange(b.dataset.value);
		});
		if (bound) stOrValue.on((s) => paint(s[keyOrOnChange]));
		return { paint };
	};

	/**
	 * Date range = optional preset pills + a from/to pill.
	 * PH.dateRange({ pills:'#datePresets', from:'#from', to:'#to', state:st, fromKey:'from', toKey:'to' })
	 */
	PH.dateRange = function (o) {
		const st = o.state,
			fk = o.fromKey || "from",
			tk = o.toKey || "to";
		const pills = PH.$(o.pills),
			f = PH.$(o.from),
			t = PH.$(o.to);
		const paint = () => {
			const s = st.get();
			if (f) f.value = s[fk] || "";
			if (t) t.value = s[tk] || "";
			if (pills) {
				const keys = PH.$$("button[data-value]", pills).map((b) => b.dataset.value);
				// Single-date pickers (no `to` input) match a preset by its start day.
				const k = t ? PH.date.presetOf(s[fk], s[tk], keys) : keys.find((x) => (PH.date.preset(x) || [])[0] === s[fk]);
				PH.$$("button", pills).forEach((b) => b.classList.toggle("is-on", b.dataset.value === k));
			}
		};
		if (pills)
			pills.addEventListener("click", (e) => {
				const b = e.target.closest("button[data-value]");
				if (!b) return;
				const p = PH.date.preset(b.dataset.value);
				if (p) st.set({ [fk]: p[0], [tk]: p[1] });
			});
		const onInput = () => {
			let a = f ? f.value : "",
				b = t ? t.value : "";
			if (!a || (t && !b)) return;
			if (t && a > b) [a, b] = [b, a];
			st.set(t ? { [fk]: a, [tk]: b } : { [fk]: a });
		};
		if (f) f.addEventListener("change", onInput);
		if (t) t.addEventListener("change", onInput);
		st.on(paint);
		paint();
	};

	/* ── Region: the universal Ravine / Karen filter ─────────────────
	 * Mirror of api/v2/core/region.py REGIONS — keep the two in step.
	 * Markup:  <div class="ph-pills" id="f-region"></div>
	 * Bind:    PH.regionPills('#f-region', st, 'region')   (state default "")
	 * Send `region` to the server as-is; the server narrows by farm via
	 * region.farms_for(). Use PH.regionFarms() only to narrow a farm <select>.
	 */
	PH.REGIONS = {
		Karen: ["Karen"],
		Ravine: ["Kapkolia", "Torongo", "Simotwo", "Chepsito", "Kaptumbo"],
	};
	PH.regionFarms = function (region) {
		return region && PH.REGIONS[region] ? PH.REGIONS[region].slice() : null;
	};
	PH.regionOf = function (farm) {
		const f = String(farm || "").toLowerCase();
		return Object.keys(PH.REGIONS).find((r) => PH.REGIONS[r].some((x) => x.toLowerCase() === f)) || null;
	};
	PH.regionPills = function (sel, st, key) {
		const el = PH.$(sel);
		if (!el) return;
		if (!el.querySelector("button[data-value]")) {
			el.innerHTML =
				'<button data-value="">All sites</button>' +
				Object.keys(PH.REGIONS)
					.sort((a, b) => (a === "Ravine" ? -1 : b === "Ravine" ? 1 : 0))
					.map((r) => '<button data-value="' + r + '" title="' + PH.esc(PH.REGIONS[r].join(", ")) + '">' + r + "</button>")
					.join("");
		}
		return PH.pills(sel, st, key || "region");
	};

	/** Bind a native <select> to a state key. Options via PH.options(). */
	PH.bindSelect = function (sel, st, key) {
		const el = PH.$(sel);
		if (!el) return;
		el.value = st.get(key) || "";
		el.addEventListener("change", () => st.set({ [key]: el.value }));
		st.on((s) => {
			if (el.value !== (s[key] || "")) el.value = s[key] || "";
		});
		return el;
	};

	/**
	 * Fill a <select>. items: array of strings or {value,label}.
	 * Keeps the first option when it has value "" (the "All …" option).
	 */
	PH.options = function (sel, items, selected) {
		const el = PH.$(sel);
		if (!el) return;
		const keep = selected != null ? selected : el.value;
		const first = el.options[0] && el.options[0].value === "" ? el.options[0] : null;
		el.innerHTML = "";
		if (first) el.appendChild(first);
		(items || []).forEach((it) => {
			const o = document.createElement("option");
			if (typeof it === "object") {
				o.value = it.value;
				o.textContent = it.label != null ? it.label : it.value;
			} else {
				o.value = it;
				o.textContent = it;
			}
			el.appendChild(o);
		});
		el.value = keep || "";
		if (el.value !== (keep || "")) el.value = "";
		return el;
	};

	/** Bind a .ph-search input to a state key, debounced. */
	PH.bindSearch = function (sel, st, key, ms) {
		const el = PH.$(sel);
		if (!el) return;
		el.value = st.get(key) || "";
		el.addEventListener("input", PH.debounce(() => st.set({ [key]: el.value.trim() }), ms || 250));
		return el;
	};

	/* ── Misc utils ────────────────────────────────────────────────── */
	PH.debounce = function (fn, ms) {
		let t;
		return function () {
			const a = arguments,
				self = this;
			clearTimeout(t);
			t = setTimeout(() => fn.apply(self, a), ms || 250);
		};
	};
	PH.sum = (arr, k) => (arr || []).reduce((a, r) => a + (+(typeof k === "function" ? k(r) : k ? r[k] : r) || 0), 0);
	PH.groupBy = function (arr, k) {
		const m = new Map();
		(arr || []).forEach((r) => {
			const key = typeof k === "function" ? k(r) : r[k];
			if (!m.has(key)) m.set(key, []);
			m.get(key).push(r);
		});
		return m;
	};
	PH.uniq = (arr) => Array.from(new Set((arr || []).filter((x) => x != null && x !== "")));
	PH.match = function (row, q, keys) {
		if (!q) return true;
		q = q.toLowerCase();
		return (keys || Object.keys(row)).some((k) => String(row[k] == null ? "" : row[k]).toLowerCase().includes(q));
	};
	/** Desk URL for a document. */
	PH.docUrl = (doctype, name) => "/app/" + doctype.toLowerCase().replace(/ /g, "-") + (name ? "/" + encodeURIComponent(name) : "");
	PH.openDoc = (doctype, name) => window.open(PH.docUrl(doctype, name), "_blank", "noopener");
	PH.user = function () {
		return (window.frappe && frappe.session && frappe.session.user) || "";
	};

	/** Run fn every ms while the tab is visible; fn(true) marks a background refresh. */
	PH.poll = function (fn, ms) {
		let t = null;
		const start = () => {
			stop();
			t = setInterval(() => !document.hidden && fn(true), ms);
		};
		const stop = () => t && clearInterval(t);
		document.addEventListener("visibilitychange", () => {
			if (!document.hidden) fn(true);
		});
		start();
		return { stop, start };
	};

	/** Keep the main pane's scroll position across an in-place re-render. */
	PH.keepScroll = function (fn) {
		const m = document.querySelector(".ph-main");
		const y = m ? m.scrollTop : 0;
		const r = fn();
		if (m) m.scrollTop = y;
		return r;
	};

	/* ── Topbar: "Updated 2 min ago" + refresh button ──────────────── */
	let lastUpdated = null;
	PH.markUpdated = function () {
		lastUpdated = new Date();
		paintUpdated();
	};
	function paintUpdated() {
		const el = document.getElementById("ph-updated");
		if (!el || !lastUpdated) return;
		el.hidden = false;
		el.textContent = "Updated " + PH.fmt.ago(lastUpdated);
		el.title = lastUpdated.toLocaleString();
		el.classList.toggle("is-stale", Date.now() - lastUpdated.getTime() > 5 * 60000);
	}
	setInterval(paintUpdated, 30000);

	/** Register what the topbar refresh button does. */
	PH.onRefresh = function (fn) {
		const b = document.getElementById("ph-refresh");
		if (!b) return;
		b.hidden = false;
		b.onclick = () => {
			b.classList.add("is-spinning");
			Promise.resolve(fn()).finally(() => setTimeout(() => b.classList.remove("is-spinning"), 300));
		};
	};

	/* ── States: skeleton / empty / error ──────────────────────────── */
	PH.empty = function (el, title, text, icon) {
		return PH.set(
			el,
			`<div class="ph-empty"><div class="ph-empty__icon">${PH.icon(icon || "inbox", 20)}</div><div class="ph-empty__title">${PH.esc(title || "Nothing to show")}</div>${
				text ? `<div class="ph-empty__text">${PH.esc(text)}</div>` : ""
			}</div>`
		);
	};
	PH.error = function (el, text, retry) {
		el = PH.$(el);
		if (!el) return;
		PH.set(
			el,
			`<div class="ph-empty ph-empty--error"><div class="ph-empty__icon">${PH.icon("alert", 20)}</div><div class="ph-empty__title">Couldn’t load this</div><div class="ph-empty__text">${PH.esc(
				text || "The server returned an error."
			)}</div>${retry ? '<button class="ph-btn ph-btn--sm ph-mt" data-retry>Try again</button>' : ""}</div>`
		);
		if (retry) el.querySelector("[data-retry]").onclick = retry;
	};
	/** Skeleton placeholders. kind: 'rows' | 'tiles' | 'block' */
	PH.skeleton = function (el, kind, n) {
		n = n || (kind === "tiles" ? 6 : 6);
		let h = "";
		if (kind === "tiles") {
			const tile =
				'<div class="ph-tile"><span class="ph-sk" style="height:16px;width:55%"></span><span class="ph-sk" style="height:11px;width:80%"></span><span class="ph-sk" style="height:44px;width:100%"></span><span class="ph-sk" style="height:8px;width:70%"></span></div>';
			h = tile.repeat(n);
		} else if (kind === "block") {
			h = '<span class="ph-sk" style="height:' + (n * 40 || 240) + 'px;width:100%"></span>';
		} else {
			for (let i = 0; i < n; i++)
				h += `<div style="display:flex;gap:16px;padding:14px 0;border-bottom:1px solid var(--hairline)"><span class="ph-sk" style="height:12px;width:${20 + ((i * 13) % 20)}%"></span><span class="ph-sk" style="height:12px;flex:1"></span><span class="ph-sk" style="height:12px;width:12%"></span></div>`;
		}
		return PH.set(el, h);
	};
	/** Toggle KPI skeletons: PH.loadingKpis('#kpis', true) */
	PH.loadingKpis = function (sel, on) {
		const el = PH.$(sel);
		if (el) el.classList.toggle("is-loading", !!on);
	};

	/* ── KPI rendering ─────────────────────────────────────────────── */
	/**
	 * PH.kpis('#kpis', [{label, value, unit, trend:{dir:'up'|'down'|'flat', text, note}, bar:pct, id, dark, onClick}])
	 * value can be a preformatted string.
	 */
	PH.kpis = function (sel, items) {
		const el = PH.$(sel);
		if (!el) return;
		el.classList.remove("is-loading");
		PH.set(
			el,
			items
				.map(
					(k, i) => `<div class="ph-kpi${k.dark ? " ph-kpi--dark" : ""}${k.onClick ? " is-link" : ""}${k.active ? " is-on" : ""}" data-i="${i}"${k.title ? ` title="${PH.esc(k.title)}"` : ""}>
				<div class="ph-kpi__label">${PH.esc(k.label)}</div>
				<div class="ph-kpi__value">${typeof k.value === "number" ? PH.fmt.num(k.value) : PH.esc(k.value == null ? "—" : k.value)}${k.suffix ? `<small>${PH.esc(k.suffix)}</small>` : ""}</div>
				${k.unit ? `<div class="ph-kpi__unit">${PH.esc(k.unit)}</div>` : ""}
				${k.trend ? `<div class="ph-kpi__trend ${k.trend.dir || "flat"}">${PH.esc(k.trend.text || "")} ${k.trend.note ? `<small>${PH.esc(k.trend.note)}</small>` : ""}</div>` : ""}
				${k.bar != null ? `<div class="ph-kpi__bar ph-progress"><i class="${k.barClass || ""}" style="width:${Math.max(0, Math.min(100, +k.bar || 0))}%"></i></div>` : ""}
			</div>`
				)
				.join("")
		);
		PH.$$(".ph-kpi", el).forEach((card) => {
			const k = items[+card.dataset.i];
			if (k.onClick) card.addEventListener("click", () => k.onClick(k));
		});
	};
	/** Initial skeleton KPI cards so the layout never jumps. */
	PH.kpiSkeleton = function (sel, labels) {
		PH.kpis(
			sel,
			labels.map((l) => ({ label: l, value: "—" }))
		);
		PH.loadingKpis(sel, true);
	};

	/* ── Table ─────────────────────────────────────────────────────── */
	/**
	 * const t = PH.table('#el', {
	 *   columns: [{ key, label, num:true, align, width, sortable:true(default), format:(v,row)=>text,
	 *               html:(v,row)=>htmlString, cls, total:true|fn(rows) }],
	 *   rows, sort:{key, dir:'asc'|'desc'}, empty:'No orders', emptyText,
	 *   onRowClick:(row)=>{}, rowClass:(row)=>'', compact, tall, pageSize, footer:true
	 * });
	 * t.update(rows); t.rows(); t.sorted()
	 */
	PH.table = function (sel, cfg) {
		const el = PH.$(sel);
		let rows = cfg.rows || [];
		let sort = cfg.sort ? Object.assign({}, cfg.sort) : null;
		let page = 0;
		const cols = cfg.columns;
		const hasTotal = cfg.footer || cols.some((c) => c.total);

		function cmp(a, b, c) {
			const va = c.sortValue ? c.sortValue(a) : a[c.key],
				vb = c.sortValue ? c.sortValue(b) : b[c.key];
			if (va == null && vb == null) return 0;
			if (va == null || va === "") return 1;
			if (vb == null || vb === "") return -1;
			const na = +va,
				nb = +vb;
			if (!isNaN(na) && !isNaN(nb) && va !== "" && vb !== "") return na - nb;
			return String(va).localeCompare(String(vb), undefined, { numeric: true, sensitivity: "base" });
		}
		function sorted() {
			if (!sort) return rows.slice();
			const c = cols.find((x) => x.key === sort.key);
			if (!c) return rows.slice();
			const s = rows.slice().sort((a, b) => cmp(a, b, c));
			return sort.dir === "desc" ? s.reverse() : s;
		}
		function cell(c, r) {
			const v = r[c.key];
			if (c.html) {
				const h = c.html(v, r);
				return h && h.__raw != null ? h.__raw : h == null ? "" : String(h);
			}
			if (c.format) return PH.esc(c.format(v, r));
			if (c.num) return PH.esc(PH.fmt.qty(v));
			return PH.esc(v == null || v === "" ? "—" : v);
		}
		function cls(c) {
			return [c.num ? "is-num" : "", c.align === "center" ? "is-center" : "", c.cls || ""].filter(Boolean).join(" ");
		}
		function render() {
			if (!el) return;
			if (!rows.length) {
				PH.empty(el, cfg.empty || "No records", cfg.emptyText);
				return;
			}
			const all = sorted();
			const ps = cfg.pageSize || 0;
			const pages = ps ? Math.max(1, Math.ceil(all.length / ps)) : 1;
			if (page >= pages) page = pages - 1;
			const view = ps ? all.slice(page * ps, page * ps + ps) : all;
			const head = cols
				.map((c) => {
					const sortable = c.sortable !== false;
					const on = sort && sort.key === c.key;
					return `<th class="${cls(c)}${sortable ? " is-sortable" : ""}${on ? " is-sorted" : ""}" data-k="${PH.esc(c.key)}"${c.width ? ` style="width:${c.width}"` : ""}>${PH.esc(c.label)}${
						sortable ? `<span class="ph-sort">${on ? (sort.dir === "asc" ? "▲" : "▼") : "↕"}</span>` : ""
					}</th>`;
				})
				.join("");
			const body = view
				.map((r, i) => {
					const rc = (cfg.rowClass ? cfg.rowClass(r) : "") + (cfg.onRowClick ? " is-link" : "");
					return `<tr class="${rc.trim()}" data-i="${i}">${cols.map((c) => `<td class="${cls(c)}">${cell(c, r)}</td>`).join("")}</tr>`;
				})
				.join("");
			let foot = "";
			if (hasTotal) {
				foot =
					"<tfoot><tr>" +
					cols
						.map((c, i) => {
							let v = "";
							if (typeof c.total === "function") v = c.total(all);
							else if (c.total) v = PH.fmt.qty(PH.sum(all, c.key));
							else if (i === 0) v = "Total";
							return `<td class="${cls(c)}">${PH.esc(v)}</td>`;
						})
						.join("") +
					"</tr></tfoot>";
			}
			const pager =
				ps && pages > 1
					? `<div class="ph-pager"><span>${PH.fmt.num(page * ps + 1)}–${PH.fmt.num(Math.min(all.length, page * ps + ps))} of ${PH.fmt.num(all.length)}</span><span class="ph-flex"><button class="ph-iconbtn ph-iconbtn--sm" data-pg="-1" ${
							page === 0 ? "disabled" : ""
					  }>${PH.icon("chevronLeft")}</button><button class="ph-iconbtn ph-iconbtn--sm" data-pg="1" ${page >= pages - 1 ? "disabled" : ""}>${PH.icon("chevronRight")}</button></span></div>`
					: "";
			el.innerHTML = `<div class="ph-table-wrap${cfg.tall ? " ph-table-wrap--tall" : ""}"><table class="ph-table${cfg.compact ? " ph-table--compact" : ""}"><thead><tr>${head}</tr></thead><tbody>${body}</tbody>${foot}</table></div>${pager}`;
			el._view = view;
		}
		// One delegated listener per holder: calling PH.table() again on the same
		// element swaps in the new table's handler instead of stacking another.
		if (el && el._phTableClick) el.removeEventListener("click", el._phTableClick);
		if (el) {
			el._phTableClick = (e) => {
				const th = e.target.closest("th.is-sortable");
				if (th && el.contains(th)) {
					const k = th.dataset.k;
					sort = sort && sort.key === k ? { key: k, dir: sort.dir === "asc" ? "desc" : "asc" } : { key: k, dir: cols.find((c) => c.key === k).num ? "desc" : "asc" };
					render();
					return;
				}
				const pg = e.target.closest("[data-pg]");
				if (pg) {
					page += +pg.dataset.pg;
					render();
					return;
				}
				if (cfg.onRowClick && !e.target.closest("a,button,input,select,textarea,label")) {
					const tr = e.target.closest("tbody tr");
					if (tr && el._view) cfg.onRowClick(el._view[+tr.dataset.i], e);
				}
			};
			el.addEventListener("click", el._phTableClick);
		}
		render();
		return {
			update(r) {
				rows = r || [];
				page = 0;
				PH.keepScroll(render);
			},
			rows: () => rows,
			sorted,
			render,
		};
	};

	/* ── Toast ─────────────────────────────────────────────────────── */
	PH.toast = function (msg, kind, ms) {
		let box = document.querySelector(".ph-toasts");
		if (!box) {
			box = document.createElement("div");
			box.className = "ph-toasts";
			(document.querySelector(".ph2") || document.body).appendChild(box);
		}
		const t = document.createElement("div");
		t.className = "ph-toast" + (kind ? " ph-toast--" + kind : "");
		t.setAttribute("role", "status");
		t.textContent = PH.stripHtml(msg);
		box.appendChild(t);
		setTimeout(() => {
			t.classList.add("is-out");
			setTimeout(() => t.remove(), 250);
		}, ms || (kind === "bad" ? 6000 : 3200));
	};

	/* ── Modal / drawer ────────────────────────────────────────────── */
	/**
	 * const m = PH.modal({ title, sub, body: html|Node, size:'lg'|'xl', drawer:false,
	 *   actions:[{label, primary, danger, onClick:(m)=>{ return false to keep open }}],
	 *   onClose })
	 * m.el (the .ph-modal), m.body, m.close(), m.busy(true)
	 */
	PH.modal = function (o) {
		const ov = document.createElement("div");
		ov.className = "ph-overlay" + (o.drawer ? " ph-overlay--drawer" : "");
		ov.innerHTML = `<div class="ph-modal${o.size ? " ph-modal--" + o.size : ""}" role="dialog" aria-modal="true">
			<div class="ph-modal__head"><div><div class="ph-modal__title"></div>${o.sub ? '<div class="ph-modal__sub"></div>' : ""}</div>
			<button class="ph-iconbtn ph-iconbtn--flat ph-iconbtn--sm" data-close aria-label="Close">${PH.icon("close")}</button></div>
			<div class="ph-modal__body"></div>
			${o.actions && o.actions.length ? '<div class="ph-modal__foot"></div>' : ""}</div>`;
		const md = ov.firstElementChild;
		md.querySelector(".ph-modal__title").textContent = o.title || "";
		if (o.sub) md.querySelector(".ph-modal__sub").textContent = o.sub;
		const body = md.querySelector(".ph-modal__body");
		if (o.body instanceof Node) body.appendChild(o.body);
		else PH.set(body, o.body || "");
		const api = {
			el: md,
			body,
			close() {
				document.removeEventListener("keydown", onKey);
				ov.remove();
				if (o.onClose) o.onClose();
			},
			busy(on) {
				md.querySelectorAll(".ph-modal__foot button").forEach((b) => (b.disabled = !!on));
			},
		};
		if (o.actions) {
			const foot = md.querySelector(".ph-modal__foot");
			o.actions.forEach((a) => {
				const b = document.createElement("button");
				b.className = "ph-btn" + (a.primary ? " ph-btn--primary" : a.danger ? " ph-btn--danger" : " ph-btn--ghost");
				b.textContent = a.label;
				b.onclick = async () => {
					if (!a.onClick) return api.close();
					api.busy(true);
					try {
						const keep = await a.onClick(api);
						if (keep !== false) api.close();
					} catch (e) {
						if (e && e.message) PH.toast(e.message, "bad");
					} finally {
						api.busy(false);
					}
				};
				foot.appendChild(b);
			});
		}
		const onKey = (e) => {
			if (e.key === "Escape" && !e.defaultPrevented) api.close();
		};
		document.addEventListener("keydown", onKey);
		ov.addEventListener("mousedown", (e) => {
			if (e.target === ov) api.close();
		});
		md.querySelector("[data-close]").onclick = api.close;
		(document.querySelector(".ph2") || document.body).appendChild(ov);
		const f = md.querySelector("input,select,textarea");
		if (f) setTimeout(() => f.focus(), 50);
		return api;
	};
	/** Promise<boolean> confirmation dialog. */
	PH.confirm = function (title, text, opts) {
		opts = opts || {};
		return new Promise((res) => {
			let done = false;
			PH.modal({
				title,
				body: text ? `<p class="ph-muted">${PH.esc(text)}</p>` : "",
				actions: [
					{ label: opts.cancel || "Cancel", onClick: () => { done = true; res(false); } },
					{ label: opts.ok || "Confirm", primary: !opts.danger, danger: !!opts.danger, onClick: () => { done = true; res(true); } },
				],
				onClose: () => !done && res(false),
			});
		});
	};
	/** Popup menu anchored to an element. items: [{label, icon, danger, onClick}] */
	PH.menu = function (anchor, items) {
		document.querySelectorAll(".ph-menu").forEach((m) => m.remove());
		const m = document.createElement("div");
		m.className = "ph-menu";
		items.forEach((it) => {
			const b = document.createElement("button");
			if (it.danger) b.className = "is-danger";
			b.innerHTML = (it.icon ? PH.icon(it.icon) : "") + "<span></span>";
			b.querySelector("span").textContent = it.label;
			b.onclick = () => {
				m.remove();
				it.onClick && it.onClick();
			};
			m.appendChild(b);
		});
		(document.querySelector(".ph2") || document.body).appendChild(m);
		const r = anchor.getBoundingClientRect();
		const w = m.offsetWidth,
			h = m.offsetHeight;
		m.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, r.right - w)) + "px";
		m.style.top = (r.bottom + h + 8 > window.innerHeight ? r.top - h - 6 : r.bottom + 6) + "px";
		setTimeout(() => {
			const off = (e) => {
				if (!m.contains(e.target)) {
					m.remove();
					document.removeEventListener("mousedown", off);
				}
			};
			document.addEventListener("mousedown", off);
		});
		return m;
	};

	/* ── Autocomplete (Link-field style) ───────────────────────────── */
	/**
	 * PH.autocomplete(inputEl, { source: async (q)=>[{value,label,description}], onSelect:(item)=>{}, minChars:0,
 *   display:(item)=>text  // what the input shows after a pick (default item.value) })
	 * Use PH.linkSource('Customer', {filters, fields}) for doctype search.
	 */
	PH.autocomplete = function (input, o) {
		input = PH.$(input);
		if (!input) return;
		const wrap = document.createElement("div");
		wrap.className = "ph-ac";
		input.parentNode.insertBefore(wrap, input);
		wrap.appendChild(input);
		input.setAttribute("autocomplete", "off");
		let menu = null,
			items = [],
			active = -1,
			seq = 0;
		const close = () => {
			if (menu) menu.remove();
			menu = null;
			active = -1;
		};
		const paint = () => {
			if (!menu) {
				menu = document.createElement("div");
				menu.className = "ph-ac__menu";
				wrap.appendChild(menu);
				menu.addEventListener("mousedown", (e) => {
					const it = e.target.closest(".ph-ac__item");
					if (!it) return;
					e.preventDefault();
					pick(+it.dataset.i);
				});
			}
			menu.innerHTML = items.length
				? items
						.map((it, i) => `<div class="ph-ac__item${i === active ? " is-active" : ""}" data-i="${i}">${PH.esc(it.label || it.value)}${it.description ? `<small>${PH.esc(it.description)}</small>` : ""}</div>`)
						.join("")
				: '<div class="ph-ac__empty">No matches</div>';
		};
		const pick = (i) => {
			const it = items[i];
			if (!it) return;
			input.value = o.display ? o.display(it) : it.value;
			close();
			o.onSelect && o.onSelect(it);
			input.dispatchEvent(new Event("change", { bubbles: true }));
		};
		const search = PH.debounce(async () => {
			const q = input.value.trim();
			if (q.length < (o.minChars || 0)) return close();
			const my = ++seq;
			try {
				const r = await o.source(q);
				if (my !== seq || document.activeElement !== input) return;
				items = r || [];
				active = items.length ? 0 : -1;
				paint();
			} catch (e) {
				close();
			}
		}, 180);
		input.addEventListener("input", search);
		input.addEventListener("focus", search);
		input.addEventListener("blur", () => setTimeout(close, 120));
		input.addEventListener("keydown", (e) => {
			if (!menu) return;
			if (e.key === "ArrowDown") {
				active = Math.min(items.length - 1, active + 1);
				paint();
				e.preventDefault();
			} else if (e.key === "ArrowUp") {
				active = Math.max(0, active - 1);
				paint();
				e.preventDefault();
			} else if (e.key === "Enter" && active >= 0) {
				pick(active);
				e.preventDefault();
			} else if (e.key === "Escape") {
				// Closing the dropdown must not also close a modal around it.
				e.preventDefault();
				close();
			}
		});
		return { close };
	};
	/** Doctype search source for PH.autocomplete (uses frappe.desk.search.search_link). */
	PH.linkSource = function (doctype, opts) {
		opts = opts || {};
		return (q) =>
			PH.call("frappe.desk.search.search_link", { doctype, txt: q, filters: opts.filters || null, page_length: opts.limit || 20 }, { silent: true, type: "GET" }).then((r) =>
				(r || []).map((x) => ({ value: x.value, label: x.label || x.value, description: x.description }))
			);
	};

	/* ── CSV export ────────────────────────────────────────────────── */
	/** PH.csv('orders.csv', rows, columns[{key,label,format?}]) */
	PH.csv = function (filename, rows, columns) {
		const cols = columns || Object.keys(rows[0] || {}).map((k) => ({ key: k, label: k }));
		const q = (v) => {
			v = v == null ? "" : String(v);
			return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
		};
		const lines = [cols.map((c) => q(c.label)).join(",")].concat(rows.map((r) => cols.map((c) => q(c.csv ? c.csv(r[c.key], r) : r[c.key])).join(",")));
		const blob = new Blob(["﻿" + lines.join("\n")], { type: "text/csv;charset=utf-8" });
		const a = document.createElement("a");
		a.href = URL.createObjectURL(blob);
		a.download = filename;
		document.body.appendChild(a);
		a.click();
		setTimeout(() => {
			URL.revokeObjectURL(a.href);
			a.remove();
		}, 0);
	};

	/* ── Charts (Chart.js, themed to the design tokens) ────────────── */
	let chartLib = null;
	PH.loadCharts = function () {
		if (window.Chart) return Promise.resolve(window.Chart);
		if (chartLib) return chartLib;
		chartLib = new Promise((res, rej) => {
			const s = document.createElement("script");
			s.src = "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js";
			s.onload = () => {
				const C = window.Chart;
				C.defaults.font.family = "Poppins, -apple-system, sans-serif";
				C.defaults.font.size = 11;
				C.defaults.color = "#8a8780";
				C.defaults.borderColor = "rgba(10,10,10,0.05)";
				C.defaults.plugins.legend.display = false;
				C.defaults.plugins.tooltip.backgroundColor = "#0a0a0a";
				C.defaults.plugins.tooltip.titleFont = { family: "Poppins", weight: "600", size: 12 };
				C.defaults.plugins.tooltip.bodyFont = { family: "Poppins", size: 12 };
				C.defaults.plugins.tooltip.padding = 10;
				C.defaults.plugins.tooltip.cornerRadius = 10;
				C.defaults.plugins.tooltip.displayColors = true;
				C.defaults.plugins.tooltip.boxPadding = 4;
				C.defaults.maintainAspectRatio = false;
				res(C);
			};
			s.onerror = () => rej(new Error("Chart library failed to load"));
			document.head.appendChild(s);
		});
		return chartLib;
	};
	/** Series palette: ink first, then greys, then the single signal accent. */
	PH.palette = ["#0a0a0a", "#228883", "#8a8780", "#3a3a34", "#73b3a0", "#b8b6ae", "#5a5a52", "#c4302b", "#f59e0b", "#1a8a3a"];
	PH.statusColor = { good: "#1a8a3a", warn: "#f59e0b", bad: "#c4302b", signal: "#228883", ink: "#0a0a0a", mute: "#b8b6ae" };
	/** Vertical gradient fill for area charts. */
	PH.gradient = function (ctx, color, a1, a0) {
		const h = ctx.canvas.clientHeight || 280;
		const g = ctx.createLinearGradient(0, 0, 0, h);
		const rgb = hexToRgb(color);
		g.addColorStop(0, `rgba(${rgb},${a1 == null ? 0.22 : a1})`);
		g.addColorStop(1, `rgba(${rgb},${a0 == null ? 0 : a0})`);
		return g;
	};
	function hexToRgb(h) {
		h = h.replace("#", "");
		if (h.length === 3) h = h.split("").map((c) => c + c).join("");
		const n = parseInt(h, 16);
		return [(n >> 16) & 255, (n >> 8) & 255, n & 255].join(",");
	}
	/**
	 * PH.chart('#holder', { type:'line'|'bar'|'doughnut', labels, series:[{label, data, color, fill, stack, type}],
	 *   stacked, horizontal, yFormat:(v)=>str, onClick:(index,label)=>{} })
	 * Returns a promise of the Chart instance. Re-calling on the same holder replaces it.
	 */
	PH.chart = function (sel, o) {
		const holder = PH.$(sel);
		if (!holder) return Promise.resolve(null);
		return PH.loadCharts().then((C) => {
			if (holder._chart) holder._chart.destroy();
			holder.innerHTML = "<canvas></canvas>";
			const ctx = holder.firstChild.getContext("2d");
			const isLine = o.type === "line";
			const isDough = o.type === "doughnut";
			const datasets = (o.series || []).map((s, i) => {
				const color = s.color || PH.palette[i % PH.palette.length];
				const d = { label: s.label, data: s.data, type: s.type, stack: s.stack };
				if (isDough) {
					d.backgroundColor = s.colors || PH.palette;
					d.borderWidth = 0;
					d.hoverOffset = 6;
				} else if (isLine || s.type === "line") {
					d.borderColor = color;
					d.borderWidth = s.width || 2.5;
					d.pointRadius = 0;
					d.pointHoverRadius = 4;
					d.pointBackgroundColor = color;
					d.tension = 0.35;
					d.fill = s.fill !== false && i === 0 ? "origin" : !!s.fill;
					if (d.fill) d.backgroundColor = PH.gradient(ctx, color);
					if (s.dashed) d.borderDash = [5, 5];
				} else {
					d.backgroundColor = s.colors || color;
					d.borderRadius = 6;
					d.borderSkipped = false;
					d.maxBarThickness = 36;
				}
				return d;
			});
			const fmt = o.yFormat || ((v) => PH.fmt.compact(v));
			const valueAxis = { beginAtZero: true, stacked: !!o.stacked, grid: { color: "rgba(10,10,10,0.05)", drawTicks: false }, border: { display: false }, ticks: { padding: 8, callback: fmt } };
			const catAxis = { stacked: !!o.stacked, grid: { display: false }, border: { display: false }, ticks: { padding: 6, autoSkip: true, maxRotation: 0 } };
			const cfg = {
				type: isDough ? "doughnut" : isLine ? "line" : "bar",
				data: { labels: o.labels, datasets },
				options: {
					indexAxis: o.horizontal ? "y" : "x",
					interaction: { mode: isDough ? "nearest" : "index", intersect: isDough },
					animation: { duration: 400 },
					plugins: {
						legend: { display: !!o.legend, position: "bottom", labels: { usePointStyle: true, boxWidth: 8, padding: 16 } },
						tooltip: { callbacks: { label: (c) => ` ${c.dataset.label || c.label}: ${PH.fmt.qty(c.parsed && typeof c.parsed === "object" ? (o.horizontal ? c.parsed.x : c.parsed.y) : c.parsed)}` } },
					},
					onClick: o.onClick
						? (e, els) => {
								if (els && els.length) o.onClick(els[0].index, o.labels[els[0].index]);
						  }
						: undefined,
				},
			};
			if (isDough) cfg.options.cutout = "68%";
			else cfg.options.scales = o.horizontal ? { x: valueAxis, y: catAxis } : { x: catAxis, y: valueAxis };
			if (o.options) deepMerge(cfg.options, o.options);
			holder._chart = new C(ctx, cfg);
			return holder._chart;
		});
	};
	function deepMerge(t, s) {
		Object.keys(s).forEach((k) => {
			if (s[k] && typeof s[k] === "object" && !Array.isArray(s[k]) && t[k] && typeof t[k] === "object") deepMerge(t[k], s[k]);
			else t[k] = s[k];
		});
	}

	/* ── Shell behaviour: sidebar collapse, mobile drawer, scroll state ─ */
	function initShell() {
		const root = document.querySelector(".ph2");
		if (!root) return;
		document.documentElement.classList.add("ph2-root");
		const collapse = document.getElementById("ph-collapse");
		if (collapse)
			collapse.addEventListener("click", () => {
				root.classList.toggle("is-collapsed");
				try {
					localStorage.setItem("ph2SideCollapsed", root.classList.contains("is-collapsed") ? "1" : "0");
				} catch (e) {}
			});
		const menuBtn = document.getElementById("ph-menu-btn");
		const scrim = root.querySelector(".ph-scrim");
		if (menuBtn) menuBtn.addEventListener("click", () => root.classList.add("is-drawer-open"));
		if (scrim) scrim.addEventListener("click", () => root.classList.remove("is-drawer-open"));
		const main = root.querySelector(".ph-main");
		if (main) main.addEventListener("scroll", () => main.classList.toggle("is-scrolled", main.scrollTop > 4), { passive: true });
		const nav = root.querySelector(".ph-side__nav");
		if (nav) {
			try {
				const y = sessionStorage.getItem("ph2SideScroll");
				if (y) nav.scrollTop = +y;
			} catch (e) {}
			window.addEventListener("pagehide", () => {
				try {
					sessionStorage.setItem("ph2SideScroll", String(nav.scrollTop));
				} catch (e) {}
			});
		}
	}
	if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initShell);
	else initShell();
})();
