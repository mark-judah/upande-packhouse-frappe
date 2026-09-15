// Shared by www/price-lists.html and www/new-customer-price-list.html --
// renders the Line -> Category -> Item price grid and handles inline
// click-to-edit on any length cell. One implementation, not duplicated
// across both pages.
//
// Editing model: clicking a cell opens an input, same as before, but the
// typed value is now STAGED (not saved immediately on blur) into a pending
// set shown in a "Save N changes" bar. Saving one cell or many goes through
// the exact same path -- the bar's Save button just fires onSetPrice for
// every staged cell (in parallel) and reconciles each cell against its own
// result, so "save one" and "bulk save several" are the same code path,
// never two. `onSetPrice(item_code, length, rawValue)` (unchanged contract,
// both pages already pass this) is called once per staged cell on Save.
//
// The save bar itself renders into `saveBarMount` (5th arg to render()) when
// given -- the page's own sticky top bar, so Save/Discard stay on screen no
// matter how far down the (potentially very long) price grid the user has
// scrolled. Falls back to inline-above-the-grid if no mount is passed.
window.PriceListTree = (function () {
	function esc(s) {
		return (s == null ? "" : String(s)).replace(/[&<>"']/g, function (c) {
			return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
		});
	}

	function fmtRate(rate, currency) {
		if (rate === null || rate === undefined) return "";
		var n = Number(rate);
		return (currency ? currency + " " : "") + n.toFixed(2);
	}

	function cellKey(code, length) { return code + "\u241F" + length; }

	var SAVEBAR_HTML =
		'<div class="plt-savebar" id="plt-savebar">' +
		'<span class="plt-savebar-n">No unsaved changes</span>' +
		'<button type="button" class="plt-btn plt-btn-ghost" data-act="plt-discard" disabled>Discard</button>' +
		'<button type="button" class="plt-btn plt-btn-key" data-act="plt-save" disabled>Save changes</button>' +
		'</div>';

	// Renders into `container` (a DOM element). `state` = {lines, lengths,
	// currency, priced_cells, total_cells}. `onSetPrice(item_code, length, rawValue)`
	// must return a Promise resolving to {success, error?, cleared?, rate?}.
	// `saveBarMount` (optional): a DOM element (typically in the page's own
	// sticky topbar) to render the Save/Discard bar into instead of inline.
	function render(container, state, onSetPrice, saveBarMount) {
		var lengths = state.lengths || [];
		var currency = state.currency || "";
		// Pending (typed but not yet saved) edits for THIS render — cleared
		// whenever the tree reloads (switching price list, etc).
		var pending = {}; // key -> {code, length, rawValue, original}
		var barHost = saveBarMount || container;

		var html = "";
		html += '<div class="plt-summary">' +
			'<span class="plt-summary-n">' + (state.priced_cells || 0) + '</span> / ' +
			'<span class="plt-summary-n">' + (state.total_cells || 0) + '</span> variety&times;length combinations priced' +
			(currency ? ' &middot; <span class="plt-cur">' + esc(currency) + '</span>' : '') +
			'</div>';

		if (saveBarMount) {
			saveBarMount.innerHTML = SAVEBAR_HTML;
		} else {
			html += SAVEBAR_HTML;
		}

		if (!state.lines || !state.lines.length) {
			html += '<div class="plt-empty">No varieties found under Cut Flowers.</div>';
			container.innerHTML = html;
			return;
		}

		html += '<div class="plt-head-row">' +
			'<div class="plt-head-name">Variety</div>' +
			lengths.map(function (l) { return '<div class="plt-head-len">' + esc(l) + '</div>'; }).join("") +
			'</div>';

		state.lines.forEach(function (line) {
			html += '<div class="plt-line">' + esc(line.name) + '</div>';
			line.cats.forEach(function (cat) {
				if (cat.name && cat.name !== line.name) {
					html += '<div class="plt-cat">' + esc(cat.name) + '</div>';
				}
				cat.items.forEach(function (it) {
					html += '<div class="plt-row" data-code="' + esc(it.code) + '">';
					html += '<div class="plt-item-name' + (it.s === "Inactive" ? " plt-inactive" : "") + '">' + esc(it.n) + "</div>";
					lengths.forEach(function (len) {
						var rate = it.lengths[len];
						var missing = rate === null || rate === undefined;
						html += '<div class="plt-cell' + (missing ? " plt-missing" : " plt-priced") +
							'" data-code="' + esc(it.code) + '" data-length="' + esc(len) + '">' +
							(missing ? "&mdash;" : fmtRate(rate, currency)) + "</div>";
					});
					html += "</div>";
				});
			});
		});

		container.innerHTML = html;
		wireCells(container, barHost, onSetPrice, currency, pending);
	}

	function nextEditableCell(container, code, length, lengths) {
		// Same row, next length to the right that isn't already mid-edit.
		var idx = lengths.indexOf(length);
		if (idx < 0 || idx === lengths.length - 1) return null;
		var nextLen = lengths[idx + 1];
		return container.querySelector('.plt-cell[data-code="' + cssEsc(code) + '"][data-length="' + cssEsc(nextLen) + '"]');
	}
	function cssEsc(s) { return String(s).replace(/["\\]/g, "\\$&"); }

	function updateSaveBar(barHost, pending) {
		var bar = barHost.querySelector("#plt-savebar");
		if (!bar) return;
		var n = Object.keys(pending).length;
		var label = bar.querySelector(".plt-savebar-n");
		if (label) label.textContent = n ? (n + " unsaved change" + (n === 1 ? "" : "s")) : "No unsaved changes";
		bar.classList.toggle("plt-has-pending", n > 0);
		var discardBtn = bar.querySelector('[data-act="plt-discard"]');
		var saveBtn = bar.querySelector('[data-act="plt-save"]');
		if (discardBtn) discardBtn.disabled = n === 0;
		if (saveBtn) saveBtn.disabled = n === 0;
	}

	function wireCells(container, barHost, onSetPrice, currency, pending) {
		var lengths = Array.prototype.slice.call(container.querySelectorAll(".plt-head-len")).map(function (h) { return h.textContent; });

		function openEditor(cell, focusAfterOpen) {
			if (cell.querySelector("input")) return; // already editing
			var code = cell.getAttribute("data-code");
			var length = cell.getAttribute("data-length");
			var key = cellKey(code, length);
			var staged = pending[key];
			var current = staged ? staged.rawValue :
				(cell.classList.contains("plt-missing") ? "" : cell.textContent.replace(/[^0-9.]/g, ""));
			var input = document.createElement("input");
			input.type = "number";
			input.step = "0.01";
			input.min = "0";
			input.className = "plt-edit-input";
			input.value = current;
			cell.textContent = "";
			cell.appendChild(input);
			if (focusAfterOpen !== false) { input.focus(); input.select(); }

			var stageAndClose = function (moveNext) {
				var val = input.value;
				var original = staged ? staged.original : (cell.classList.contains("plt-missing") ? "" : current);
				var unchanged = (val || "") === (original || "");
				if (unchanged) {
					delete pending[key];
					renderCellIdle(cell, cell.classList.contains("plt-missing") ? null : Number(original), currency, false);
				} else {
					pending[key] = { code: code, length: length, rawValue: val, original: original };
					renderCellIdle(cell, val, currency, true, true);
				}
				updateSaveBar(barHost, pending);
				if (moveNext) {
					var next = nextEditableCell(container, code, length, lengths);
					if (next) openEditor(next, true);
				}
			};
			input.addEventListener("keydown", function (e) {
				if (e.key === "Enter") { e.preventDefault(); input.removeEventListener("blur", onBlur); stageAndClose(true); }
				if (e.key === "Escape") {
					e.preventDefault();
					input.removeEventListener("blur", onBlur);
					if (staged) renderCellIdle(cell, staged.rawValue, currency, true, true);
					else renderCellIdle(cell, cell.classList.contains("plt-missing") ? null : Number(current), currency, false);
				}
			});
			var onBlur = function () { stageAndClose(false); };
			input.addEventListener("blur", onBlur);
		}

		container.querySelectorAll(".plt-cell").forEach(function (cell) {
			cell.addEventListener("click", function () { openEditor(cell, true); });
		});

		// `container`/`barHost` are persistent elements re-populated on every
		// render (switching price lists never replaces them, only their
		// innerHTML) -- a plain addEventListener here would stack a new
		// delegated handler, closing over that render's own stale `pending`
		// object, on top of every earlier one. Replace any previous handler
		// instead of adding another.
		if (barHost._pltClickHandler) {
			barHost.removeEventListener("click", barHost._pltClickHandler);
		}
		var clickHandler = function (e) {
			var btn = e.target.closest("[data-act]");
			if (!btn) return;
			if (btn.getAttribute("data-act") === "plt-discard") {
				Object.keys(pending).forEach(function (key) {
					var p = pending[key];
					var cell = container.querySelector('.plt-cell[data-code="' + cssEsc(p.code) + '"][data-length="' + cssEsc(p.length) + '"]');
					if (cell) renderCellIdle(cell, p.original ? Number(p.original) : null, currency, false);
				});
				pending = {};
				updateSaveBar(barHost, pending);
			} else if (btn.getAttribute("data-act") === "plt-save") {
				saveAllPending(container, barHost, onSetPrice, currency, pending);
			}
		};
		barHost._pltClickHandler = clickHandler;
		barHost.addEventListener("click", clickHandler);
	}

	function renderCellIdle(cell, value, currency, dirty, isRaw) {
		cell.innerHTML = "";
		var missing = value === null || value === undefined || value === "";
		cell.className = "plt-cell" + (missing ? " plt-missing" : " plt-priced") + (dirty ? " plt-dirty" : "");
		cell.textContent = missing ? "\u2014" : (isRaw ? String(value) : fmtRate(value, currency));
	}

	function saveAllPending(container, barHost, onSetPrice, currency, pending) {
		var keys = Object.keys(pending);
		if (!keys.length) return;
		var bar = barHost.querySelector("#plt-savebar");
		var saveBtn = bar && bar.querySelector('[data-act="plt-save"]');
		if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = "Saving\u2026"; }

		Promise.all(keys.map(function (key) {
			var p = pending[key];
			var cell = container.querySelector('.plt-cell[data-code="' + cssEsc(p.code) + '"][data-length="' + cssEsc(p.length) + '"]');
			if (cell) cell.classList.add("plt-saving");
			return onSetPrice(p.code, p.length, p.rawValue).then(function (res) {
				return { key: key, p: p, cell: cell, res: res };
			});
		})).then(function (results) {
			var failed = 0;
			results.forEach(function (r) {
				if (!r.cell) return;
				r.cell.classList.remove("plt-saving");
				if (r.res && r.res.success) {
					delete pending[r.key];
					if (r.res.cleared) renderCellIdle(r.cell, null, currency, false);
					else renderCellIdle(r.cell, r.res.rate != null ? r.res.rate : Number(r.p.rawValue), currency, false);
				} else {
					failed++;
					r.cell.classList.add("plt-error");
					setTimeout(function () { r.cell.classList.remove("plt-error"); }, 1800);
					// left in `pending` so the bar still shows it and Save can be retried
				}
			});
			if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = "Save changes"; }
			updateSaveBar(barHost, pending);
			if (bar) {
				var label = bar.querySelector(".plt-savebar-n");
				if (label && failed) label.textContent += " \u2014 " + failed + " failed, click Save to retry";
			}
		});
	}

	return { render: render };
})();
