/* Site Builder — client-side search over search-index.json.
   Self-contained, no dependencies. Degrades gracefully: with this script
   disabled the search box is inert and the rest of the site still works. */
(function () {
	"use strict";

	var input = document.getElementById("search-input");
	var results = document.getElementById("search-results");
	if (!input || !results) return;

	var index = [];
	var loaded = false;

	function load() {
		if (loaded) return;
		loaded = true;
		fetch("search-index.json")
			.then(function (response) {
				return response.ok ? response.json() : [];
			})
			.then(function (data) {
				index = Array.isArray(data) ? data : [];
				if (input.value) render(input.value);
			})
			.catch(function () {
				index = [];
			});
	}

	function snippet(text, terms) {
		var lower = text.toLowerCase();
		var at = -1;
		for (var i = 0; i < terms.length; i++) {
			at = lower.indexOf(terms[i]);
			if (at !== -1) break;
		}
		if (at === -1) at = 0;
		var start = Math.max(0, at - 40);
		var piece = text.slice(start, start + 160).trim();
		return (start > 0 ? "… " : "") + piece + "…";
	}

	function score(entry, terms) {
		var haystack = (entry.title + " " + entry.text).toLowerCase();
		var total = 0;
		for (var i = 0; i < terms.length; i++) {
			if (haystack.indexOf(terms[i]) === -1) return 0;
			total += entry.title.toLowerCase().indexOf(terms[i]) !== -1 ? 3 : 1;
		}
		return total;
	}

	function render(query) {
		var terms = query.toLowerCase().split(/\s+/).filter(Boolean);
		if (terms.length === 0) {
			results.hidden = true;
			results.innerHTML = "";
			return;
		}
		var matches = [];
		for (var i = 0; i < index.length; i++) {
			var value = score(index[i], terms);
			if (value > 0) matches.push({ entry: index[i], value: value });
		}
		matches.sort(function (a, b) {
			return b.value - a.value;
		});
		results.innerHTML = "";
		if (matches.length === 0) {
			var empty = document.createElement("li");
			empty.className = "no-results";
			empty.textContent = "No matches.";
			results.appendChild(empty);
			results.hidden = false;
			return;
		}
		matches.slice(0, 10).forEach(function (match) {
			var li = document.createElement("li");
			var a = document.createElement("a");
			a.href = match.entry.url;
			var title = document.createElement("span");
			title.className = "result-title";
			title.textContent = match.entry.title;
			var snip = document.createElement("span");
			snip.className = "result-snippet";
			snip.textContent = snippet(match.entry.text, terms);
			a.appendChild(title);
			a.appendChild(snip);
			li.appendChild(a);
			results.appendChild(li);
		});
		results.hidden = false;
	}

	input.addEventListener("focus", load);
	input.addEventListener("input", function () {
		render(input.value);
	});

	document.addEventListener("click", function (event) {
		if (!event.target.closest(".site-search")) {
			results.hidden = true;
		}
	});

	input.addEventListener("keydown", function (event) {
		if (event.key === "Escape") {
			results.hidden = true;
			input.blur();
		}
	});
})();
