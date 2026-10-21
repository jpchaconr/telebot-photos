// Web gallery: grid with infinite scroll, filters and a full-screen viewer.
// User texts (names, captions) are only ever written with textContent.
"use strict";

(() => {
  const data = JSON.parse(document.getElementById("gallery-data").textContent);
  const $ = (id) => document.getElementById(id);

  const grid = $("grid");
  const more = $("more");
  const statusBox = $("status");
  const statusText = $("status-text");
  const retryButton = $("retry");
  const senderSelect = $("sender");
  const monthSelect = $("month");
  const viewer = $("viewer");
  const stage = $("stage");
  const prevButton = $("prev");
  const nextButton = $("next");
  const closeButton = $("close");
  const downloadLink = $("download");
  const infoSender = $("info-sender");
  const infoDate = $("info-date");
  const infoCaption = $("info-caption");

  const timeZone = data.timezone;
  const dateFormat = new Intl.DateTimeFormat("es", {
    dateStyle: "long",
    timeStyle: "short",
    timeZone,
  });
  const monthFormat = new Intl.DateTimeFormat("es", { month: "long", year: "numeric", timeZone });
  const monthKeyFormat = new Intl.DateTimeFormat("en", {
    year: "numeric",
    month: "2-digit",
    timeZone,
  });
  const TYPE_NAMES = { image: "Foto", video: "Vídeo", gif: "GIF" };

  const state = {
    items: [],
    tiles: [],
    next: null,
    done: false,
    loading: false,
    generation: 0,
    lastMonth: null,
    current: -1,
  };

  // --- helpers --------------------------------------------------------------

  const capitalize = (text) => text.charAt(0).toUpperCase() + text.slice(1);

  function count(n, one, many) {
    return `${n.toLocaleString("es")} ${n === 1 ? one : many}`;
  }

  // "2026-10" for a timestamp, in the group's timezone.
  function monthKey(seconds) {
    const parts = {};
    for (const part of monthKeyFormat.formatToParts(new Date(seconds * 1000))) {
      parts[part.type] = part.value;
    }
    return `${parts.year}-${parts.month}`;
  }

  function monthLabel(key) {
    const [year, month] = key.split("-").map(Number);
    return capitalize(monthFormat.format(new Date(Date.UTC(year, month - 1, 15))));
  }

  function fileUrl(item) {
    return `${data.base}/file/${item.id}`;
  }

  function setStatus(text, canRetry = false) {
    statusBox.hidden = !text;
    statusText.textContent = text;
    retryButton.hidden = !canRetry;
  }

  const filtersActive = () => Boolean(senderSelect.value || monthSelect.value);

  // --- filters --------------------------------------------------------------

  for (const sender of data.senders) {
    senderSelect.add(new Option(`${sender.name} (${sender.count})`, String(sender.id)));
  }
  for (const month of data.months) {
    monthSelect.add(new Option(`${monthLabel(month.value)} (${month.count})`, month.value));
  }

  const params = new URLSearchParams(window.location.search);
  senderSelect.value = params.get("persona") || "";
  monthSelect.value = params.get("mes") || "";
  for (const select of [senderSelect, monthSelect]) {
    if (select.selectedIndex < 0) select.value = "";
  }

  $("summary").textContent = data.total ? count(data.total, "foto o vídeo", "fotos y vídeos") : "";
  if (!data.total) document.querySelector(".filters").hidden = true;

  function onFilterChange() {
    const query = new URLSearchParams();
    if (senderSelect.value) query.set("persona", senderSelect.value);
    if (monthSelect.value) query.set("mes", monthSelect.value);
    const search = query.toString();
    window.history.replaceState(null, "", search ? `?${search}` : window.location.pathname);
    reset();
    load();
  }
  senderSelect.addEventListener("change", onFilterChange);
  monthSelect.addEventListener("change", onFilterChange);

  // --- grid -----------------------------------------------------------------

  function reset() {
    state.generation += 1;
    state.items = [];
    state.tiles = [];
    state.next = null;
    state.done = false;
    state.loading = false;
    state.lastMonth = null;
    grid.replaceChildren();
    setStatus("");
    window.scrollTo(0, 0);
  }

  function tile(item, index) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "tile";
    button.dataset.placeholder = item.type === "image" ? "Sin vista previa" : "▶";
    const date = dateFormat.format(new Date(item.sent_at * 1000));
    button.setAttribute("aria-label", `${TYPE_NAMES[item.type]} de ${item.sender}, ${date}`);
    if (item.thumb) {
      const img = document.createElement("img");
      img.alt = "";
      img.loading = "lazy";
      img.decoding = "async";
      img.addEventListener("error", () => button.classList.add("missing"), { once: true });
      img.src = `${data.base}/thumb/${item.id}`;
      button.append(img);
    } else {
      button.classList.add("missing");
    }
    if (item.type !== "image") {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = item.type === "gif" ? "GIF" : "▶";
      button.append(badge);
    }
    button.addEventListener("click", () => openViewer(index));
    return button;
  }

  function append(items) {
    const fragment = document.createDocumentFragment();
    for (const item of items) {
      const key = monthKey(item.sent_at);
      if (key !== state.lastMonth) {
        state.lastMonth = key;
        const heading = document.createElement("h2");
        heading.className = "month";
        heading.textContent = monthLabel(key);
        fragment.append(heading);
      }
      const index = state.items.push(item) - 1;
      const element = tile(item, index);
      state.tiles.push(element);
      fragment.append(element);
    }
    grid.append(fragment);
    if (!viewer.hidden) updateNav();
  }

  async function load() {
    if (state.loading || state.done) return;
    state.loading = true;
    const generation = state.generation;
    const query = new URLSearchParams();
    if (senderSelect.value) query.set("sender", senderSelect.value);
    if (monthSelect.value) query.set("month", monthSelect.value);
    if (state.next) query.set("cursor", state.next);
    try {
      const response = await fetch(`${data.base}/api/items?${query}`, {
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const page = await response.json();
      if (generation !== state.generation) return;
      append(page.items);
      state.next = page.next;
      state.done = !page.next;
      if (state.items.length) {
        setStatus("");
      } else {
        setStatus(
          filtersActive()
            ? "No hay fotos ni vídeos con estos filtros."
            : "Todavía no hay fotos ni vídeos guardados.",
        );
      }
    } catch (error) {
      if (generation === state.generation) {
        setStatus("No se pudieron cargar las fotos.", true);
      }
      return;
    } finally {
      if (generation === state.generation) state.loading = false;
    }
    // The observer only fires on changes: keep loading while the end is still on screen.
    if (!state.done && more.getBoundingClientRect().top < window.innerHeight + 800) load();
  }

  new IntersectionObserver(
    (entries) => {
      if (entries.some((entry) => entry.isIntersecting)) load();
    },
    { rootMargin: "800px 0px" },
  ).observe(more);

  retryButton.addEventListener("click", () => {
    setStatus("");
    load();
  });

  // --- viewer ---------------------------------------------------------------

  function errorMessage() {
    const p = document.createElement("p");
    p.className = "error";
    p.textContent = "No se pudo cargar este archivo.";
    return p;
  }

  function show(index) {
    const item = state.items[index];
    if (!item) return;
    state.current = index;
    let media;
    if (item.type === "image") {
      media = document.createElement("img");
      media.alt = item.caption || `Foto de ${item.sender}`;
    } else {
      media = document.createElement("video");
      media.playsInline = true;
      media.setAttribute("playsinline", "");
      media.preload = "metadata";
      if (item.type === "gif") {
        media.muted = true;
        media.defaultMuted = true;
        media.loop = true;
        media.autoplay = true;
      } else {
        media.controls = true;
      }
    }
    media.className = "media";
    media.addEventListener("error", () => stage.replaceChildren(errorMessage()), { once: true });
    media.src = fileUrl(item);
    stage.replaceChildren(media);
    if (item.type === "gif") media.play().catch(() => {});

    const date = new Date(item.sent_at * 1000);
    infoSender.textContent = item.sender;
    infoDate.textContent = dateFormat.format(date);
    infoDate.dateTime = date.toISOString();
    infoCaption.textContent = item.caption || "";
    infoCaption.hidden = !item.caption;
    downloadLink.href = `${fileUrl(item)}?download=1`;

    updateNav();
    if (index >= state.items.length - 3) load();
    const next = state.items[index + 1];
    if (next && next.type === "image") new Image().src = fileUrl(next);
  }

  function updateNav() {
    prevButton.disabled = state.current <= 0;
    nextButton.disabled = state.current >= state.items.length - 1;
  }

  function step(delta) {
    const target = state.current + delta;
    if (target >= 0 && target < state.items.length) show(target);
  }

  function openViewer(index) {
    if (viewer.hidden) {
      viewer.hidden = false;
      document.body.classList.add("noscroll");
      // The phone's back button closes the viewer instead of leaving the page.
      window.history.pushState({ viewer: true }, "");
    }
    show(index);
    closeButton.focus();
  }

  function hideViewer() {
    viewer.hidden = true;
    stage.replaceChildren();
    document.body.classList.remove("noscroll");
    const element = state.tiles[state.current];
    if (element) {
      element.focus({ preventScroll: true });
      element.scrollIntoView({ block: "nearest" });
    }
  }

  function closeViewer() {
    if (window.history.state && window.history.state.viewer) window.history.back();
    else hideViewer();
  }

  window.addEventListener("popstate", () => {
    if (!viewer.hidden) hideViewer();
  });
  closeButton.addEventListener("click", closeViewer);
  prevButton.addEventListener("click", () => step(-1));
  nextButton.addEventListener("click", () => step(1));
  stage.addEventListener("click", (event) => {
    if (event.target === stage) closeViewer();
  });

  document.addEventListener("keydown", (event) => {
    if (viewer.hidden) return;
    if (event.key === "Escape") closeViewer();
    else if (event.key === "ArrowLeft") step(-1);
    else if (event.key === "ArrowRight") step(1);
    else return;
    event.preventDefault();
  });

  let touch = null;
  stage.addEventListener(
    "touchstart",
    (event) => {
      const start = event.touches[0];
      touch = event.touches.length === 1 ? { x: start.clientX, y: start.clientY } : null;
      // Leave the bottom of a video to its own controls (the progress bar).
      const target = event.target;
      if (touch && target.tagName === "VIDEO" && target.controls) {
        if (start.clientY > target.getBoundingClientRect().bottom - 72) touch = null;
      }
    },
    { passive: true },
  );
  stage.addEventListener(
    "touchend",
    (event) => {
      if (!touch || event.touches.length) return;
      const end = event.changedTouches[0];
      const dx = end.clientX - touch.x;
      const dy = end.clientY - touch.y;
      touch = null;
      if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5) step(dx < 0 ? 1 : -1);
      else if (dy > 90 && dy > Math.abs(dx) * 1.5) closeViewer();
    },
    { passive: true },
  );

  load();
})();
