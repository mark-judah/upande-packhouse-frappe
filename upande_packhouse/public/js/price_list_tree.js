// Shared by www/price-lists.html and www/new-customer-price-list.html --
// renders the Line -> Category -> Item price grid and handles inline
// click-to-edit on any length cell. One implementation, not duplicated
// across both pages.
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

	// Renders into `container` (a DOM element). `state` = {lines, lengths,
	// currency, priced_cells, total_cells}. `onSetPrice(item_code, length, rawValue)`
	// must return a Promise resolving to {success, error?, cleared?, rate?}.
	function render(container, state, onSetPrice) {
		var lengths = state.lengths || [];
		var currency = state.currency || "";

		var html = "";
		html += '<div class="plt-summary">' +
			'<span class="plt-summary-n">' + (state.priced_cells || 0) + '</span> / ' +
			'<span class="plt-summary-n">' + (state.total_cells || 0) + '</span> variety&times;length combinations priced' +
			(currency ? ' &middot; <span class="plt-cur">' + esc(currency) + '</span>' : '') +
			'</div>';

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
		wireCells(container, onSetPrice);
	}

	function wireCells(container, onSetPrice) {
		container.querySelectorAll(".plt-cell").forEach(function (cell) {
			cell.addEventListener("click", function () {
				if (cell.querySelector("input")) return; // already editing
				var code = cell.getAttribute("data-code");
				var length = cell.getAttribute("data-length");
				var current = cell.classList.contains("plt-missing") ? "" :
					cell.textContent.replace(/[^0-9.]/g, "");
				var input = document.createElement("input");
				input.type = "number";
				input.step = "0.01";
				input.min = "0";
				input.className = "plt-edit-input";
				input.value = current;
				cell.textContent = "";
				cell.appendChild(input);
				input.focus();
				input.select();

				var commit = function () {
					var val = input.value;
					cell.classList.add("plt-saving");
					onSetPrice(code, length, val).then(function (res) {
						cell.classList.remove("plt-saving");
						if (!res || !res.success) {
							cell.classList.add("plt-error");
							setTimeout(function () { cell.classList.remove("plt-error"); }, 1500);
						}
						if (res && res.cleared) {
							cell.className = "plt-cell plt-missing";
							cell.setAttribute("data-code", code);
							cell.setAttribute("data-length", length);
							cell.innerHTML = "&mdash;";
						} else if (res && res.success) {
							cell.className = "plt-cell plt-priced";
							cell.setAttribute("data-code", code);
							cell.setAttribute("data-length", length);
							cell.textContent = fmtRate(res.rate, cell.closest(".plt-tree") ?
								cell.closest(".plt-tree").getAttribute("data-currency") : "");
						} else {
							// revert to whatever it was
							cell.textContent = current ? current : "—";
						}
					});
				};
				input.addEventListener("keydown", function (e) {
					if (e.key === "Enter") { input.blur(); }
					if (e.key === "Escape") {
						input.removeEventListener("blur", commit);
						cell.textContent = current ? current : "—";
					}
				});
				input.addEventListener("blur", commit);
			});
		});
	}

	return { render: render };
})();
