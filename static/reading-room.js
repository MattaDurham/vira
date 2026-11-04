/* Reading rooms — the shared behavior for every generated queue.
 *
 * A generated room (static/reading/<slug>.html) ships two globals and
 * nothing else:
 *
 *   window.ROOM = {slug}               — which room this is
 *   window.DATA = [ {…item}, … ]       — the queue itself
 *
 * Everything below is generic. Filters that would be dead on arrival hide
 * themselves: a fresh room where every item is unseen shows no coverage
 * chips, a single-year room shows no year select, a room nobody is named
 * in shows no people select. A first room therefore looks built for its
 * subject rather than like a template with empty controls.
 *
 * Nothing is ever marked read or done (owner's call, 2026-11-04): every
 * item stays as available as the day the room was built. */

(function () {
  "use strict";

  var ROOM = window.ROOM || {};
  var DATA = window.DATA || [];

  // Items are authored server-side, but be forgiving about optional
  // fields so a sparse room never throws on render.
  DATA.forEach(function (it) {
    if (!Array.isArray(it.people)) it.people = [];
    if (!it.prio) it.prio = "P2";
    if (!it.status) it.status = "MISSING";
    if (!it.mode) it.mode = "read";
  });

  var state = {
    q: "", prio: new Set(), status: new Set(), mode: new Set(),
    person: "", year: "", sort: "prio",
  };
  var PRIO_RANK = { P1: 0, P2: 1, P3: 2 };
  // HAVE renders no badge and no chip (owner's call, 2026-07-27): everything
  // in a room is headed into the vault anyway, so "in the vault" is not a
  // distinction worth a filter. The status value survives in data.
  var STATUS_LABEL = { MISSING: "unseen", PARTIAL: "secondhand" };

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : s;
    return d.innerHTML;
  }

  /* ---------- controls that earn their place ---------- */

  function buildControls() {
    // People: everyone named in 2+ items, most-frequent first.
    var freq = {};
    DATA.forEach(function (it) {
      it.people.forEach(function (p) { freq[p] = (freq[p] || 0) + 1; });
    });
    var people = Object.keys(freq).filter(function (p) { return freq[p] >= 2; })
      .sort(function (a, b) { return freq[b] - freq[a] || a.localeCompare(b); });
    var sel = $("person");
    people.forEach(function (p) {
      var o = document.createElement("option");
      o.value = p; o.textContent = p + " (" + freq[p] + ")";
      sel.appendChild(o);
    });
    if (!people.length) sel.hidden = true;

    // Years: only worth a control when the room spans more than one.
    var years = Object.keys(DATA.reduce(function (acc, it) {
      if (it.year) acc[it.year] = 1;
      return acc;
    }, {})).sort().reverse();
    var ys = $("year");
    years.forEach(function (y) {
      var o = document.createElement("option");
      o.value = y; o.textContent = y; ys.appendChild(o);
    });
    if (years.length < 2) ys.hidden = true;

    // A chip group whose values are all identical filters nothing.
    ["status", "mode"].forEach(function (key) {
      var seen = {};
      DATA.forEach(function (it) { seen[it[key]] = 1; });
      var vals = Object.keys(seen);
      var group = $(key + "Chips");
      if (!group) return;
      group.querySelectorAll(".chip").forEach(function (c) {
        if (vals.indexOf(c.dataset.v) < 0) c.hidden = true;
      });
      if (vals.length < 2) group.hidden = true;
    });
  }

  /* ---------- filter, sort, render ---------- */

  function matches(it) {
    if (state.prio.size && !state.prio.has(it.prio)) return false;
    if (state.status.size && !state.status.has(it.status)) return false;
    if (state.mode.size && !state.mode.has(it.mode)) return false;
    if (state.person && it.people.indexOf(state.person) < 0) return false;
    if (state.year && it.year !== state.year) return false;
    if (state.q) {
      var hay = (it.title + " " + (it.note || "") + " " + (it.why || "") + " "
        + (it.venue || "") + " " + it.people.join(" ")).toLowerCase();
      if (hay.indexOf(state.q.toLowerCase()) < 0) return false;
    }
    return true;
  }

  function sorted(arr) {
    var a = arr.slice();
    if (state.sort === "new") {
      a.sort(function (x, y) { return (y.date || "0").localeCompare(x.date || "0"); });
    } else if (state.sort === "old") {
      a.sort(function (x, y) { return (x.date || "9999").localeCompare(y.date || "9999"); });
    } else {
      a.sort(function (x, y) {
        return (PRIO_RANK[x.prio] - PRIO_RANK[y.prio])
          || (x.date || "9999").localeCompare(y.date || "9999");
      });
    }
    return a;
  }

  function render() {
    var list = $("list");
    var rows = sorted(DATA.filter(matches));
    $("count").textContent = rows.length + " of " + DATA.length;
    if (!rows.length) {
      list.innerHTML = '<div class="empty">Nothing matches. Loosen a filter.</div>';
      return;
    }
    list.innerHTML = rows.map(function (it) {
      var badges = [
        '<span class="badge ' + it.prio + '">' + it.prio + "</span>",
        STATUS_LABEL[it.status]
          ? '<span class="badge ' + it.status + '">'
            + STATUS_LABEL[it.status] + "</span>" : "",
        '<span class="badge mode">' + esc(it.mode) + "</span>",
        it.pay ? '<span class="badge pay">$ paywall</span>' : "",
      ].join(" ");
      var title = it.url
        ? '<a href="' + esc(it.url) + '" target="_blank" rel="noopener">'
          + esc(it.title) + "</a>"
        : esc(it.title);
      var meta = [it.date || null, it.type || null, it.venue || null]
        .filter(Boolean).map(esc).join(" &middot; ");
      var ppl = it.people.slice(0, 6).map(function (p) {
        return '<button class="person" data-p="' + esc(p) + '">' + esc(p) + "</button>";
      }).join("");
      var note = it.note ? '<p class="note">' + esc(it.note) + "</p>" : "";
      var vault = (it.status !== "MISSING" && it.vault)
        ? '<div class="vault">vault: ' + esc(it.vault) + "</div>" : "";
      return '<div class="card" data-id="' + esc(it.id) + '">'
        + '<div class="body"><p class="title">' + title + "</p>"
        + '<div class="meta">' + badges + (meta ? " <span>" + meta + "</span>" : "") + "</div>"
        + note + (ppl ? '<div class="people">' + ppl + "</div>" : "") + vault
        + "</div></div>";
    }).join("");
  }

  /* ---------- wiring ---------- */

  $("list").addEventListener("click", function (e) {
    var person = e.target.closest(".person");
    if (person) {
      state.person = person.dataset.p;
      var sel = $("person");
      sel.hidden = false;
      var known = Array.prototype.some.call(sel.options, function (o) {
        return o.value === state.person;
      });
      if (!known) {
        var o = document.createElement("option");
        o.value = state.person; o.textContent = state.person;
        sel.appendChild(o);
      }
      sel.value = state.person;
      render();
    }
  });

  document.querySelectorAll(".chip[data-k]").forEach(function (chip) {
    chip.addEventListener("click", function () {
      var on = chip.getAttribute("aria-pressed") === "true";
      chip.setAttribute("aria-pressed", String(!on));
      var set = state[chip.dataset.k];
      if (on) set.delete(chip.dataset.v); else set.add(chip.dataset.v);
      render();
    });
    chip.setAttribute("aria-pressed", "false");
  });

  $("q").addEventListener("input", function (e) {
    state.q = e.target.value.trim(); render();
  });
  $("person").addEventListener("change", function (e) {
    state.person = e.target.value; render();
  });
  $("year").addEventListener("change", function (e) {
    state.year = e.target.value; render();
  });
  $("sort").addEventListener("change", function (e) {
    state.sort = e.target.value; render();
  });
  $("clear").addEventListener("click", function () {
    state.q = ""; state.prio.clear(); state.status.clear(); state.mode.clear();
    state.person = ""; state.year = ""; state.sort = "prio";
    $("q").value = ""; $("person").value = ""; $("year").value = "";
    $("sort").value = "prio";
    document.querySelectorAll(".chip[aria-pressed]").forEach(function (c) {
      c.setAttribute("aria-pressed", "false");
    });
    render();
  });

  buildControls();
  render();
})();
