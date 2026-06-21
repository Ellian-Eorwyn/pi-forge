/* Site Builder — progressive enhancement: theme toggle, mobile nav, scroll
   reveal, active table-of-contents highlighting, and figure zoom.
   Self-contained, no dependencies. The site is fully usable without this script. */
(function () {
	"use strict";

	var root = document.documentElement;
	root.classList.add("js");

	var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

	// Restore a previously chosen theme; otherwise follow prefers-color-scheme.
	try {
		var stored = localStorage.getItem("site-theme");
		if (stored === "light" || stored === "dark") {
			root.setAttribute("data-theme", stored);
		}
	} catch (error) {
		/* localStorage unavailable; fall back to the OS preference. */
	}

	function currentScheme() {
		var explicit = root.getAttribute("data-theme");
		if (explicit === "light" || explicit === "dark") return explicit;
		return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
	}

	document.addEventListener("click", function (event) {
		var toggle = event.target.closest(".theme-toggle");
		if (toggle) {
			var next = currentScheme() === "dark" ? "light" : "dark";
			root.setAttribute("data-theme", next);
			try {
				localStorage.setItem("site-theme", next);
			} catch (error) {
				/* ignore persistence failure */
			}
			return;
		}

		var navToggle = event.target.closest(".nav-toggle");
		if (navToggle) {
			var nav = document.getElementById("site-nav");
			if (!nav) return;
			var open = nav.classList.toggle("open");
			navToggle.setAttribute("aria-expanded", open ? "true" : "false");
		}
	});

	// Scroll reveal for section blocks.
	var revealTargets = document.querySelectorAll(".prose h2, .prose .cards, .prose figure, .prose .callout, .prose table");
	if (revealTargets.length && "IntersectionObserver" in window && !reduceMotion) {
		revealTargets.forEach(function (element) {
			element.classList.add("reveal");
		});
		var revealObserver = new IntersectionObserver(
			function (entries) {
				entries.forEach(function (entry) {
					if (entry.isIntersecting) {
						entry.target.classList.add("is-visible");
						revealObserver.unobserve(entry.target);
					}
				});
			},
			{ rootMargin: "0px 0px -10% 0px" },
		);
		revealTargets.forEach(function (element) {
			revealObserver.observe(element);
		});
	}

	// Active table-of-contents highlighting.
	var tocLinks = {};
	document.querySelectorAll(".toc a[href^='#']").forEach(function (link) {
		tocLinks[decodeURIComponent(link.getAttribute("href").slice(1))] = link;
	});
	var headings = document.querySelectorAll(".prose h2[id], .prose h3[id]");
	if (Object.keys(tocLinks).length && headings.length && "IntersectionObserver" in window) {
		var visible = new Set();
		var tocObserver = new IntersectionObserver(
			function (entries) {
				entries.forEach(function (entry) {
					if (entry.isIntersecting) visible.add(entry.target.id);
					else visible.delete(entry.target.id);
				});
				var active = null;
				headings.forEach(function (heading) {
					if (!active && visible.has(heading.id)) active = heading.id;
				});
				for (var id in tocLinks) tocLinks[id].classList.remove("is-active");
				if (active && tocLinks[active]) tocLinks[active].classList.add("is-active");
			},
			{ rootMargin: "-10% 0px -70% 0px" },
		);
		headings.forEach(function (heading) {
			tocObserver.observe(heading);
		});
	}

	// Click-to-zoom for figures.
	document.addEventListener("click", function (event) {
		var img = event.target.closest("figure img");
		if (!img) return;
		var overlay = document.createElement("div");
		overlay.className = "zoom-overlay";
		var large = document.createElement("img");
		large.src = img.currentSrc || img.src;
		large.alt = img.alt;
		overlay.appendChild(large);
		overlay.addEventListener("click", function () {
			overlay.remove();
		});
		document.body.appendChild(overlay);
	});

	document.addEventListener("keydown", function (event) {
		if (event.key === "Escape") {
			var overlay = document.querySelector(".zoom-overlay");
			if (overlay) overlay.remove();
		}
	});
})();
