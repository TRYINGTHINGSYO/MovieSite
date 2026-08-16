/* Group Theater Stage 2C-A room client. */

import {
  createDefaultLocalMediaStore,
  createStorageKey,
  getOrCreateBrowserToken,
  inspectStorageCapacity,
  pendingRegistrationBelongsTo,
  probeLocalVideo,
} from "./local_media.js";

(() => {
  "use strict";

  const code = document.body.dataset.code;
  if (!code) return;
  const authenticated = document.body.dataset.authenticated === "true";
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const $ = (id) => document.getElementById(id);
  const player = $("player");
  const muxPlayer = $("mux-player");
  const dropPrompt = $("drop-prompt");
  const dropZone = $("drop-zone");
  const notice = $("room-notice");
  const localFiles = new Map();
  const pollTimers = new Map();
  const localRestoreTasks = new Map();
  const reportedAvailability = new Map();
  let hlsPlayer = null;
  const permissions = [
    "CONTROL_PLAYBACK",
    "ADD_MEDIA",
    "MANAGE_QUEUE",
    "MANAGE_MEDIA",
    "MANAGE_MEMBERS",
    "MANAGE_ROOM",
    "REVIEW_REQUESTS",
  ];

  let applyingRemote = false;
  let activeSourceKey = null;
  let mediaElement = player;
  let stateReceivedAt = performance.now();
  let clockOffsetMs = 0;
  let lastHostSyncAt = 0;
  let pendingReviewMediaId = null;
  let advancingQueue = false;
  let browserToken = null;
  let browserClientId = null;
  let localMediaStore = null;
  let localMediaReady = false;
  let state = {
    queue: [],
    library: [],
    people: [],
    presence: [],
    requests: [],
    capabilities: [],
    identity: { kind: "guest", is_owner: false },
    current_id: null,
    playing: false,
    position: 0,
    playback_base: 0,
    playback_updated_at: null,
    server_now: null,
    queue_version: 0,
    playback_version: 0,
    viewer_count: 1,
  };

  const socket = io({
    transports: ["websocket", "polling"],
    reconnection: true,
    reconnectionAttempts: 10,
    autoConnect: false,
  });

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }

  function button(text, className, action) {
    const element = node("button", className || "btn btn-sm btn-ghost", text);
    element.type = "button";
    element.addEventListener("click", action);
    return element;
  }

  function can(permission) {
    return state.capabilities.includes(permission);
  }

  function randomId() {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID().replaceAll("-", "");
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    return [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
  }

  function inform(message, kind = "info") {
    notice.textContent = message || "";
    notice.dataset.kind = kind;
    notice.hidden = !message;
    if (message) window.setTimeout(() => { notice.hidden = true; }, 4500);
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body !== undefined) headers.set("Content-Type", "application/json");
    if (options.method && options.method !== "GET") headers.set("X-CSRFToken", csrfToken);
    if (browserToken) headers.set("X-Browser-Client-Token", browserToken);
    const response = await fetch(path, { ...options, headers });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (payload.state) applyState(payload.state);
      const error = new Error(payload.error || `Request failed (${response.status})`);
      error.status = response.status;
      throw error;
    }
    if (payload.state) applyState(payload.state);
    return payload;
  }

  async function submitRequest(requestType, payload = {}) {
    const result = await api(`/api/rooms/${code}/requests`, {
      method: "POST",
      body: JSON.stringify({
        request_type: requestType,
        payload,
        client_request_id: randomId(),
      }),
    });
    inform(result.created ? "Request sent to the room controllers." : "That request was already sent.");
  }

  function playbackAction(action, payload = {}) {
    if (action === "defer") {
      deferQueue(payload.queue_entry_id || state.current_id);
      return;
    }
    if (!can("CONTROL_PLAYBACK")) {
      const requestTypes = { play: "PLAY", pause: "PAUSE", seek: "SEEK", next: "NEXT", select: "SELECT_MEDIA" };
      const requestPayload = action === "seek"
        ? { position: payload.position }
        : action === "select"
          ? { queue_entry_id: payload.queue_entry_id }
          : {};
      submitRequest(requestTypes[action], requestPayload).catch((error) => inform(error.message, "error"));
      return;
    }
    socket.emit("playback:command", {
      code,
      action,
      ...payload,
      expected_playback_version: state.playback_version,
      expected_queue_version: state.queue_version,
      client_action_id: randomId(),
    });
  }

  function deferQueue(entryId) {
    if (!entryId) return;
    const operation = can("MANAGE_QUEUE")
      ? api(`/api/rooms/${code}/queue/${entryId}/defer`, {
          method: "POST",
          body: JSON.stringify({ expected_queue_version: state.queue_version }),
        })
      : submitRequest("MOVE_TO_END", { queue_entry_id: entryId });
    operation.catch((error) => inform(error.message, "error"));
  }

  function sourceFor(item) {
    if (!item) return null;
    if (item.source_type) return item;
    return (item.sources || []).find((source) => source.status === "ready") || item.sources?.[0] || null;
  }

  function localSourceFor(item) {
    return (item?.sources || []).find((source) => (
      source.source_type === "browser_local" && source.storage_key
    )) || null;
  }

  function sourceStatusLabel(source) {
    if (!source) return "SOURCE • UNAVAILABLE";
    if (source.source_type !== "browser_local") {
      return `${source.source_type || "source"} • ${source.status || "unknown"}`.toUpperCase();
    }
    if (source.availability === "AVAILABLE_THIS_BROWSER") return "LOCAL • READY";
    if (source.availability === "LOCAL_DATA_MISSING") return "LOCAL • MISSING";
    if (source.availability === "ERROR") return "LOCAL • ERROR";
    return "LOCAL";
  }

  function rememberLocalFile(roomMediaId, file, storageKey) {
    const previous = localFiles.get(roomMediaId);
    if (previous?.url) URL.revokeObjectURL(previous.url);
    localFiles.set(roomMediaId, {
      file,
      storageKey,
      url: URL.createObjectURL(file),
    });
  }

  async function reportLocalAvailability(source, available) {
    if (!source?.source_id || !browserToken) return;
    const reportKey = `${source.source_id}:${available}`;
    if (reportedAvailability.get(source.source_id) === reportKey) return;
    reportedAvailability.set(source.source_id, reportKey);
    try {
      await api(`/api/rooms/${code}/media/browser-local/${source.source_id}/availability`, {
        method: "POST",
        body: JSON.stringify({ available }),
      });
    } catch (error) {
      reportedAvailability.delete(source.source_id);
      throw error;
    }
  }

  async function restoreLocalLibrary(items = state.library) {
    if (!localMediaStore) return;
    for (const item of items || []) {
      const source = localSourceFor(item);
      if (!source || localFiles.has(item.id) || localRestoreTasks.has(source.storage_key)) continue;
      const task = (async () => {
        const restored = await localMediaStore.restore(source.storage_key);
        if (!restored.available) {
          await reportLocalAvailability(source, false);
          return;
        }
        rememberLocalFile(item.id, restored.file, source.storage_key);
        if (source.availability === "LOCAL_DATA_MISSING" || source.status !== "ready") {
          await reportLocalAvailability(source, true);
        }
        loadCurrent();
      })().catch((error) => inform(error.message, "error")).finally(() => {
        localRestoreTasks.delete(source.storage_key);
      });
      localRestoreTasks.set(source.storage_key, task);
    }
  }

  function currentItem() {
    return state.queue.find((item) => item.id === state.current_id) || null;
  }

  function updateClock(next) {
    const serverMs = Date.parse(next?.server_now || "");
    if (Number.isFinite(serverMs)) clockOffsetMs = serverMs - Date.now();
  }

  function authoritativePosition() {
    const base = Number.isFinite(Number(state.playback_base))
      ? Number(state.playback_base)
      : (Number.isFinite(Number(state.position)) ? Number(state.position) : 0);
    if (!state.playing) return Math.max(0, base);
    const updatedMs = Date.parse(state.playback_updated_at || "");
    if (Number.isFinite(updatedMs)) {
      return Math.max(0, base + Math.max(0, (Date.now() + clockOffsetMs - updatedMs) / 1000));
    }
    const elapsed = Math.max(0, performance.now() - stateReceivedAt) / 1000;
    return Math.max(0, base + elapsed);
  }

  function applyAuthoritativeClock(element) {
    if (!element) return;
    const targetPosition = authoritativePosition();
    if (Math.abs((element.currentTime || 0) - targetPosition) > 0.25) {
      try { element.currentTime = targetPosition; } catch { /* source is not seekable yet */ }
    }
    if (state.playing) tryPlay(element); else element.pause();
  }

  function whenMediaReady(element, onReady) {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      onReady();
    };
    element.addEventListener("canplay", finish, { once: true });
    element.addEventListener("loadedmetadata", finish, { once: true });
    window.setTimeout(finish, 2500);
  }

  function setPlayerMessage(message, isError = false) {
    $("prepare-banner").hidden = !message || isError;
    $("prepare-banner").textContent = !isError ? message || "" : "";
    $("player-error").hidden = !message || !isError;
    $("player-error-detail").textContent = isError ? message || "" : "";
  }

  function tryPlay(element) {
    const result = element?.play?.();
    if (result?.catch) result.catch(() => { $("tap-to-play").hidden = false; });
  }

  function looksLikeDirectMediaFile(url) {
    try {
      return /\.(mp4|m4v|webm|ogv|ogg|mov|mkv|m3u8|mpd)$/i.test(new URL(url).pathname);
    } catch {
      return false;
    }
  }

  function isHlsUrl(url) {
    try {
      return /\.m3u8$/i.test(new URL(url, window.location.origin).pathname);
    } catch {
      return false;
    }
  }

  function detachHls() {
    if (!hlsPlayer) return;
    hlsPlayer.destroy();
    hlsPlayer = null;
  }

  function showNoMedia() {
    activeSourceKey = null;
    detachHls();
    player.hidden = true;
    player.removeAttribute("src");
    player.load();
    muxPlayer.hidden = true;
    muxPlayer.removeAttribute("playback-id");
    dropPrompt.hidden = false;
    dropZone.classList.remove("has-video");
    setPlayerMessage("");
  }

  function loadCurrent() {
    const item = currentItem();
    if (!item) {
      showNoMedia();
      return;
    }
    const source = sourceFor(item);
    const local = localFiles.get(item.room_media_id);
    dropPrompt.hidden = true;
    dropZone.classList.add("has-video");
    $("tap-to-play").hidden = true;

    if (local && source?.source_type === "browser_local") {
      setPlayerMessage("Playing from persistent storage on this browser.");
      attachHtmlVideo(local.url, `local:${item.id}`);
      return;
    }
    if (local && source?.status !== "ready") {
      setPlayerMessage("Playing your local copy while the shared source prepares.");
      attachHtmlVideo(local.url, `local:${item.id}`);
      return;
    }
    if (source?.source_type === "mux_upload" && source.playback_id) {
      setPlayerMessage("");
      attachMux(source.playback_id);
      return;
    }
    if (source?.source_type === "direct_url" && source.url) {
      setPlayerMessage(source.probe_result === "playable_no_seek" ? "This source may not support seeking." : "");
      attachHtmlVideo(source.url, `url:${source.source_id}`);
      return;
    }
    if (source?.source_type === "browser_local") {
      const message = source.availability === "LOCAL_DATA_MISSING"
        ? "The owning browser can no longer find this local video."
        : "This video is stored on its source owner's browser.";
      setPlayerMessage(message, source.availability === "LOCAL_DATA_MISSING");
      return;
    }
    if (source?.status === "error" || source?.status === "unavailable") {
      setPlayerMessage(source.error || "This media source is unavailable.", true);
      return;
    }
    setPlayerMessage("Media is still preparing for shared playback.");
    if (source?.source_type === "mux_upload") pollMux(item.id);
  }

  function attachHtmlVideo(url, key) {
    const changed = activeSourceKey !== key;
    activeSourceKey = key;
    muxPlayer.hidden = true;
    player.hidden = false;
    mediaElement = player;
    applyingRemote = true;
    const finishApply = () => {
      applyAuthoritativeClock(player);
      window.setTimeout(() => { applyingRemote = false; }, 250);
    };
    if (changed) {
      detachHls();
      if (isHlsUrl(url) && window.Hls?.isSupported()) {
        player.removeAttribute("src");
        hlsPlayer = new window.Hls();
        hlsPlayer.loadSource(url);
        hlsPlayer.attachMedia(player);
      } else {
        player.src = url;
        player.load();
      }
      whenMediaReady(player, finishApply);
      return;
    }
    finishApply();
  }

  function attachMux(playbackId) {
    const key = `mux:${playbackId}`;
    const changed = activeSourceKey !== key;
    activeSourceKey = key;
    detachHls();
    player.hidden = true;
    muxPlayer.hidden = false;
    mediaElement = muxPlayer;
    applyingRemote = true;
    const finishApply = () => {
      applyAuthoritativeClock(muxPlayer);
      window.setTimeout(() => { applyingRemote = false; }, 250);
    };
    if (changed) {
      muxPlayer.playbackId = playbackId;
      muxPlayer.setAttribute("playback-id", playbackId);
      muxPlayer.setAttribute("poster", `https://image.mux.com/${playbackId}/thumbnail.webp?time=1`);
      whenMediaReady(muxPlayer, finishApply);
      return;
    }
    finishApply();
  }

  function saveReview(roomMediaId, rating, comment) {
    return api(`/api/rooms/${code}/media/${roomMediaId}/reviews`, {
      method: "POST",
      body: JSON.stringify({ rating, comment: comment || "" }),
    }).then(() => {
      if (pendingReviewMediaId === roomMediaId) pendingReviewMediaId = null;
      inform("Review saved for the group.");
    }).catch((error) => inform(error.message, "error"));
  }

  function renderReview(item) {
    const reviews = item.reviews || { average: null, count: 0, mine: null, latest: [] };
    const mine = reviews.mine;
    const block = node("div", "review-block");
    const comment = document.createElement("input");
    comment.type = "text";
    comment.maxLength = 280;
    comment.placeholder = "Optional comment";
    comment.value = mine?.comment || "";
    const stars = node("div", "review-stars");
    stars.setAttribute("role", "group");
    stars.setAttribute("aria-label", `Rate ${item.name}`);
    for (let rating = 1; rating <= 5; rating += 1) {
      const selected = (mine?.rating || 0) >= rating;
      const star = button("★", `star-button${selected ? " is-on" : ""}`, () => {
        saveReview(item.id, rating, comment.value.trim());
      });
      star.setAttribute("aria-label", `${rating} star${rating === 1 ? "" : "s"}`);
      stars.append(star);
    }
    const average = node(
      "span",
      "review-average",
      reviews.count ? `${reviews.average} average · ${reviews.count} review${reviews.count === 1 ? "" : "s"}` : "No ratings yet",
    );
    const commentRow = node("div", "review-comment");
    commentRow.append(comment);
    commentRow.append(button("Save review", "btn btn-sm btn-ghost", () => {
      const rating = mine?.rating;
      if (!rating) {
        inform("Choose a star rating first.", "error");
        return;
      }
      saveReview(item.id, rating, comment.value.trim());
    }));
    block.append(stars, average, commentRow);
    if ((reviews.latest || []).length) {
      const latest = node("ul", "review-comment-list");
      for (const review of reviews.latest) {
        const line = review.comment
          ? `${review.label}: ${review.rating}/5 — ${review.comment}`
          : `${review.label}: ${review.rating}/5`;
        latest.append(node("li", "", line));
      }
      block.append(latest);
    }
    return block;
  }

  function renderLibrary() {
    const list = $("library-list");
    list.replaceChildren();
    $("library-empty").hidden = state.library.length > 0;
    for (const item of state.library) {
      const row = node("li", `resource-row library-row${item.id === pendingReviewMediaId ? " needs-review" : ""}`);
      const summary = node("div", "resource-summary");
      summary.append(node("strong", "resource-title", item.name));
      const source = sourceFor(item);
      summary.append(node("span", "resource-meta", sourceStatusLabel(source)));
      row.append(summary);
      const actions = node("div", "resource-actions");
      const label = can("MANAGE_QUEUE") ? "Add to queue" : "Request queue";
      actions.append(button(label, "btn btn-sm btn-ghost", () => {
        const operation = can("MANAGE_QUEUE")
          ? api(`/api/rooms/${code}/queue`, { method: "POST", body: JSON.stringify({ room_media_id: item.id, expected_queue_version: state.queue_version }) })
          : submitRequest("ADD_SAVED_MEDIA", { room_media_id: item.id });
        operation.catch((error) => inform(error.message, "error"));
      }));
      row.append(actions);
      row.append(renderReview(item));
      list.append(row);
    }
  }

  function reorder(entryId, offset) {
    const ids = state.queue.map((item) => item.id);
    const index = ids.indexOf(entryId);
    const target = index + offset;
    if (index < 0 || target < 0 || target >= ids.length) return;
    [ids[index], ids[target]] = [ids[target], ids[index]];
    api(`/api/rooms/${code}/queue/order`, {
      method: "PUT",
      body: JSON.stringify({ queue_entry_ids: ids, expected_queue_version: state.queue_version }),
    }).catch((error) => inform(error.message, "error"));
  }

  function renderQueue() {
    const list = $("queue-list");
    list.replaceChildren();
    $("queue-empty").hidden = state.queue.length > 0;
    state.queue.forEach((item, index) => {
      const row = node("li", `resource-row queue-row${item.id === state.current_id ? " current" : ""}`);
      const summary = node("button", "queue-select");
      summary.type = "button";
      summary.append(node("span", "queue-index", String(index + 1).padStart(2, "0")));
      summary.append(node("strong", "resource-title", item.name));
      summary.addEventListener("click", () => playbackAction("select", { queue_entry_id: item.id }));
      row.append(summary);
      const actions = node("div", "resource-actions");
      if (can("MANAGE_QUEUE")) {
        actions.append(button("↑", "icon-button", () => reorder(item.id, -1)));
        actions.append(button("↓", "icon-button", () => reorder(item.id, 1)));
      }
      actions.append(button(
        can("MANAGE_QUEUE") ? "Watch later" : "Request later",
        "btn btn-sm btn-ghost",
        () => deferQueue(item.id),
      ));
      actions.append(button(can("MANAGE_QUEUE") ? "Remove" : "Request removal", "btn btn-sm btn-ghost", () => {
        const operation = can("MANAGE_QUEUE")
          ? api(`/api/rooms/${code}/queue/${item.id}`, { method: "DELETE", body: JSON.stringify({ expected_queue_version: state.queue_version }) })
          : submitRequest("REMOVE_QUEUE_ENTRY", { queue_entry_id: item.id });
        operation.catch((error) => inform(error.message, "error"));
      }));
      row.append(actions);
      list.append(row);
    });
    $("clear-upcoming").hidden = !can("MANAGE_QUEUE");
  }

  function renderPeople() {
    const list = $("people-list");
    list.replaceChildren();
    for (const person of state.people) {
      const row = node("li", "person-row");
      row.append(node("strong", "resource-title", `${person.label}${person.is_owner ? " · owner" : ""}`));
      if (state.identity.is_owner && !person.is_owner) {
        const grants = node("div", "permission-grid");
        for (const permission of permissions) {
          const label = node("label", "permission-toggle");
          const checkbox = document.createElement("input");
          checkbox.type = "checkbox";
          checkbox.checked = (person.permissions || []).includes(permission);
          checkbox.addEventListener("change", () => {
            api(`/api/rooms/${code}/permissions`, {
              method: "POST",
              body: JSON.stringify({ user_id: person.user_id, permission, enabled: checkbox.checked }),
            }).catch((error) => { checkbox.checked = !checkbox.checked; inform(error.message, "error"); });
          });
          label.append(checkbox, node("span", "", permission.replaceAll("_", " ").toLowerCase()));
          grants.append(label);
        }
        row.append(grants);
      }
      list.append(row);
    }

    const presence = $("presence-list");
    presence.replaceChildren();
    for (const person of state.presence) {
      presence.append(node("li", "presence-chip", `${person.label} · ${person.kind}`));
    }
  }

  function describeRequest(item) {
    const payload = item.payload || {};
    if (item.request_type === "SEEK") return `seek to ${payload.position}s`;
    if (item.request_type === "ADD_DIRECT_URL") return `add URL: ${payload.title}`;
    if (item.request_type === "MOVE_TO_END") return "move this to the end";
    return item.request_type.replaceAll("_", " ").toLowerCase();
  }

  function renderRequests() {
    const list = $("requests-list");
    list.replaceChildren();
    $("requests-empty").hidden = state.requests.length > 0;
    const labels = { approved: "Approve", denied: "Deny", dismissed: "Dismiss" };
    for (const item of state.requests) {
      const row = node("li", `request-row request-${item.status || "pending"}`);
      const summary = node("div", "resource-summary");
      summary.append(node("strong", "resource-title", item.requester_label));
      const meta = node("span", "resource-meta");
      meta.append(node("span", "", describeRequest(item)));
      meta.append(document.createTextNode(" · "));
      meta.append(node("span", `request-status status-${item.status}`, item.status));
      summary.append(meta);
      row.append(summary);
      if (can("REVIEW_REQUESTS") && item.status === "pending") {
        const actions = node("div", "resource-actions");
        for (const decision of ["approved", "denied", "dismissed"]) {
          actions.append(button(labels[decision], "btn btn-sm btn-ghost", () => {
            api(`/api/rooms/${code}/requests/${item.id}/resolve`, {
              method: "POST",
              body: JSON.stringify({ resolution: decision }),
            }).catch((error) => inform(error.message, "error"));
          }));
        }
        row.append(actions);
      }
      list.append(row);
    }
  }

  function snapshot(value) {
    return JSON.stringify(value);
  }

  function applyState(next) {
    if (!next || typeof next !== "object") return;
    const previous = state;
    updateClock(next);
    state = { ...state, ...next };
    stateReceivedAt = performance.now();
    const controlsPlayback = can("CONTROL_PLAYBACK");
    player.controls = controlsPlayback;
    muxPlayer.controls = controlsPlayback;
    muxPlayer.toggleAttribute("controls", controlsPlayback);
    $("viewer-count").textContent = String(state.viewer_count || 0);
    $("playback-mode").textContent = controlsPlayback
      ? "Host controls start everyone together"
      : "The host controls playback · your actions send requests";
    $("defer-action").hidden = !state.current_id;
    $("add-local-media").hidden = !can("ADD_MEDIA") || !authenticated || !localMediaReady;
    $("direct-submit").textContent = can("ADD_MEDIA")
      ? (can("MANAGE_QUEUE") ? "Play link" : "Add link")
      : "Request link";
    if (snapshot(previous.library) !== snapshot(state.library) || snapshot(previous.capabilities) !== snapshot(state.capabilities)) {
      renderLibrary();
    }
    if (
      snapshot(previous.queue) !== snapshot(state.queue)
      || previous.current_id !== state.current_id
      || snapshot(previous.capabilities) !== snapshot(state.capabilities)
    ) {
      renderQueue();
    }
    if (
      snapshot(previous.people) !== snapshot(state.people)
      || snapshot(previous.presence) !== snapshot(state.presence)
      || snapshot(previous.capabilities) !== snapshot(state.capabilities)
    ) {
      renderPeople();
    }
    if (snapshot(previous.requests) !== snapshot(state.requests) || snapshot(previous.capabilities) !== snapshot(state.capabilities)) {
      renderRequests();
    }
    loadCurrent();
    ensureMuxPolling();
    void restoreLocalLibrary(state.library);
  }

  async function probeDirectUrl(url) {
    return new Promise((resolve) => {
      const probe = document.createElement("video");
      let settled = false;
      const finish = (result) => {
        if (settled) return;
        settled = true;
        probe.removeAttribute("src");
        probe.load();
        resolve(result);
      };
      const timer = window.setTimeout(() => finish("network_or_cors_failure"), 8000);
      probe.preload = "metadata";
      probe.addEventListener("loadedmetadata", () => {
        window.clearTimeout(timer);
        finish(probe.seekable?.length ? "playable" : "playable_no_seek");
      }, { once: true });
      probe.addEventListener("error", () => {
        window.clearTimeout(timer);
        const errorCode = probe.error?.code;
        if (errorCode === MediaError.MEDIA_ERR_NETWORK) finish("network_or_cors_failure");
        else if ([MediaError.MEDIA_ERR_DECODE, MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED].includes(errorCode)) finish("unsupported_format");
        else finish("unavailable");
      }, { once: true });
      probe.src = url;
      probe.load();
    });
  }

  $("direct-url-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const title = $("direct-title").value.trim();
    const url = $("direct-url").value.trim();
    const enqueue = can("MANAGE_QUEUE");
    try {
      let probeResult = "not_probed";
      if (looksLikeDirectMediaFile(url)) {
        $("probe-result").textContent = "Checking this browser…";
        probeResult = await probeDirectUrl(url);
        $("probe-result").textContent = probeResult.replaceAll("_", " ");
      } else {
        $("probe-result").textContent = "Extracting a playable clip…";
      }
      const payload = { url, probe_result: probeResult, enqueue };
      if (title) payload.title = title;
      if (can("ADD_MEDIA")) {
        await api(`/api/rooms/${code}/media/direct-url`, {
          method: "POST",
          body: JSON.stringify(payload),
        });
        inform(enqueue ? "Playing that link for the room." : "Link saved. Add it to the queue when ready.");
      } else {
        await submitRequest("ADD_DIRECT_URL", {
          title: title || "Clip",
          url,
          probe_result: probeResult,
        });
      }
      $("direct-url-form").reset();
      $("probe-result").textContent = "";
    } catch (error) {
      inform(error.message, "error");
    }
  });

  $("add-local-media").addEventListener("click", () => $("file-input").click());
  $("file-input").addEventListener("change", () => {
    const files = [...($("file-input").files || [])];
    $("file-input").value = "";
    files.reduce((chain, file) => chain.then(() => addLocalFile(file)), Promise.resolve())
      .catch((error) => inform(error.message, "error"));
  });

  async function registerPersistedLocal(record) {
    const result = await api(`/api/rooms/${code}/media/browser-local`, {
      method: "POST",
      body: JSON.stringify({
        storage_key: record.storage_key,
        original_filename: record.original_filename,
        mime_type: record.mime_type,
        byte_size: record.byte_size,
        duration: record.duration,
      }),
    });
    await localMediaStore.markRegistered(record.storage_key, {
      browser_client_id: result.browser_client_id,
      media_asset_id: result.media_asset_id,
      room_media_id: result.room_media_id,
      source_id: result.source_id,
    });
    return result;
  }

  async function addLocalFile(file) {
    if (!localMediaStore || !browserToken || !browserClientId) {
      throw new Error("Persistent local media is not ready in this browser.");
    }
    $("upload-progress").hidden = false;
    $("upload-fill").style.width = "0%";
    try {
      $("upload-label").textContent = `Checking storage for ${file.name}...`;
      await inspectStorageCapacity(file.size, localMediaStore.storageManager);
      $("upload-label").textContent = `Inspecting ${file.name}...`;
      const probe = await probeLocalVideo(file);
      const storageKey = createStorageKey();
      $("upload-label").textContent = `Saving ${file.name} on this browser...`;
      const persisted = await localMediaStore.persist(file, {
        storage_key: storageKey,
        browser_client_id: browserClientId,
        room_code: code,
        original_filename: file.name,
        mime_type: file.type || "application/octet-stream",
        duration: probe.duration,
      });
      let result;
      $("upload-label").textContent = `Registering ${file.name}...`;
      try {
        result = await registerPersistedLocal(persisted.record);
      } catch (error) {
        if ([400, 413, 422].includes(error.status)) {
          await localMediaStore.remove(storageKey);
        } else {
          inform("The video is saved locally; registration will retry after reconnect.", "error");
        }
        throw error;
      }
      rememberLocalFile(result.room_media_id, file, storageKey);
      if (!persisted.capacity.persistent) {
        inform("Video saved locally. This browser did not guarantee persistent storage.");
      } else {
        inform("Video saved on this browser. Add it to the queue when ready.");
      }
      if (result.state) applyState(result.state);
    } finally {
      $("upload-progress").hidden = true;
    }
  }

  function pollMux(entryId) {
    if (pollTimers.has(entryId)) return;
    const timer = window.setInterval(async () => {
      try {
        const result = await api(`/api/mux/status/${code}/${entryId}`);
        const nextState = result.state || state;
        const item = nextState.queue?.find((entry) => entry.id === entryId)
          || nextState.library?.find((media) => media.id === entryId);
        const source = sourceFor(item);
        if (!item || ["ready", "error", "unavailable"].includes(source?.status)) {
          window.clearInterval(timer);
          pollTimers.delete(entryId);
        }
      } catch (error) {
        if ([403, 404].includes(error.status)) {
          window.clearInterval(timer);
          pollTimers.delete(entryId);
        }
      }
    }, 2500);
    pollTimers.set(entryId, timer);
  }

  function ensureMuxPolling() {
    if (!can("ADD_MEDIA")) {
      for (const timer of pollTimers.values()) window.clearInterval(timer);
      pollTimers.clear();
      return;
    }
    for (const item of state.queue) {
      if (item.source_type === "mux_upload" && ["uploading", "processing"].includes(item.status)) pollMux(item.id);
    }
    for (const item of state.library) {
      const source = sourceFor(item);
      if (source?.source_type === "mux_upload" && ["uploading", "processing"].includes(source.status)) pollMux(item.id);
    }
  }

  async function retryPendingLocalRegistrations() {
    if (!localMediaStore) return;
    const records = await localMediaStore.list();
    for (const record of records) {
      if (!pendingRegistrationBelongsTo(record, code, browserClientId)) continue;
      const restored = await localMediaStore.restore(record.storage_key);
      if (!restored.available) {
        await localMediaStore.remove(record.storage_key);
        continue;
      }
      try {
        const result = await registerPersistedLocal(record);
        rememberLocalFile(result.room_media_id, restored.file, record.storage_key);
      } catch (error) {
        if ([400, 413, 422].includes(error.status)) {
          await localMediaStore.remove(record.storage_key);
        }
      }
    }
  }

  async function initializeBrowserLocalMedia() {
    if (!authenticated) {
      socket.connect();
      return;
    }
    try {
      browserToken = getOrCreateBrowserToken();
      localMediaStore = createDefaultLocalMediaStore();
      const registration = await api("/api/browser-clients/register", {
        method: "POST",
        body: JSON.stringify({}),
      });
      browserClientId = registration.browser_client_id;
      localMediaReady = true;
      socket.auth = { browser_client_token: browserToken };
      socket.connect();
      void retryPendingLocalRegistrations().catch((error) => inform(error.message, "error"));
      return;
    } catch (error) {
      browserClientId = null;
      localMediaStore = null;
      localMediaReady = false;
      inform(`Local video storage is unavailable: ${error.message}`, "error");
    }
    socket.connect();
  }

  $("clear-upcoming").addEventListener("click", () => {
    api(`/api/rooms/${code}/queue/upcoming`, {
      method: "DELETE",
      body: JSON.stringify({ expected_queue_version: state.queue_version }),
    }).catch((error) => inform(error.message, "error"));
  });
  $("play-action").addEventListener("click", () => playbackAction("play", { position: mediaElement.currentTime || state.position || 0 }));
  $("pause-action").addEventListener("click", () => playbackAction("pause", { position: mediaElement.currentTime || state.position || 0 }));
  $("seek-action").addEventListener("click", () => playbackAction("seek", { position: Number($("seek-position").value) }));
  $("next-action").addEventListener("click", () => playbackAction("next"));
  $("defer-action").addEventListener("click", () => playbackAction("defer"));
  $("tap-to-play").addEventListener("click", () => { $("tap-to-play").hidden = true; tryPlay(mediaElement); });

  function restoreAuthoritativePlayback(element) {
    if (applyingRemote || can("CONTROL_PLAYBACK") || element !== mediaElement || !currentItem()) return;
    applyingRemote = true;
    try {
      applyAuthoritativeClock(element);
    } finally {
      window.setTimeout(() => { applyingRemote = false; }, 150);
    }
  }

  function maybeAdvanceFinished(element) {
    if (!can("CONTROL_PLAYBACK") || applyingRemote || advancingQueue || element !== mediaElement) return;
    const item = currentItem();
    if (!item) return;
    const duration = Number(element.duration);
    const reachedEnd = element.ended || (
      Number.isFinite(duration) && duration > 0 && authoritativePosition() >= duration - 0.4
    );
    if (!reachedEnd) return;
    advancingQueue = true;
    pendingReviewMediaId = item.room_media_id;
    renderLibrary();
    inform("Rate this title for the group.");
    playbackAction("next");
    window.setTimeout(() => { advancingQueue = false; }, 2000);
  }

  function maybeHostSync(element) {
    if (!can("CONTROL_PLAYBACK") || applyingRemote || element !== mediaElement || !currentItem()) return;
    const now = performance.now();
    if (now - lastHostSyncAt < 4000) return;
    lastHostSyncAt = now;
    const position = Number(element.currentTime);
    if (!Number.isFinite(position)) return;
    socket.emit("sync_position", {
      code,
      position,
      playing: !element.paused,
    });
  }

  for (const element of [player, muxPlayer]) {
    element.addEventListener("play", () => {
      if (element !== mediaElement || applyingRemote) return;
      if (can("CONTROL_PLAYBACK")) {
        playbackAction("play", { position: element.currentTime || 0 });
      } else {
        restoreAuthoritativePlayback(element);
      }
    });
    element.addEventListener("pause", () => {
      if (element !== mediaElement || applyingRemote) return;
      if (can("CONTROL_PLAYBACK")) {
        playbackAction("pause", { position: element.currentTime || 0 });
      } else {
        restoreAuthoritativePlayback(element);
      }
    });
    element.addEventListener("seeking", () => {
      if (element !== mediaElement || applyingRemote) return;
      if (!can("CONTROL_PLAYBACK")) restoreAuthoritativePlayback(element);
    });
    element.addEventListener("seeked", () => {
      if (element !== mediaElement || applyingRemote) return;
      if (can("CONTROL_PLAYBACK")) {
        playbackAction("seek", { position: element.currentTime || 0 });
      }
    });
    element.addEventListener("ended", () => maybeAdvanceFinished(element));
    element.addEventListener("timeupdate", () => {
      maybeAdvanceFinished(element);
      maybeHostSync(element);
    });
  }

  $("copy-link").addEventListener("click", async () => {
    const url = `${window.location.origin}/session/${code}`;
    try { await navigator.clipboard.writeText(url); } catch { /* clipboard may be unavailable */ }
    $("copy-feedback").hidden = false;
    window.setTimeout(() => { $("copy-feedback").hidden = true; }, 1500);
  });

  socket.on("connect", () => socket.emit("room:join", { code }));
  for (const event of [
    "room:state", "state_sync", "queue:updated", "queue_updated", "library:updated",
    "presence:updated", "permissions:updated", "requests:updated", "video_selected",
  ]) socket.on(event, applyState);
  socket.on("playback:updated", (payload) => {
    if (payload?.queue && payload?.capabilities) applyState(payload);
    else api(`/api/rooms/${code}/state`).catch(() => {});
  });
  socket.on("play", () => api(`/api/rooms/${code}/state`).catch(() => {}));
  socket.on("pause", () => api(`/api/rooms/${code}/state`).catch(() => {}));
  socket.on("seek", () => api(`/api/rooms/${code}/state`).catch(() => {}));
  socket.on("viewer_count", (payload) => { $("viewer-count").textContent = String(payload.count || 0); });
  socket.on("error", (payload) => inform(payload?.message || "Room action failed", "error"));

  window.addEventListener("beforeunload", () => {
    for (const timer of pollTimers.values()) window.clearInterval(timer);
    for (const local of localFiles.values()) URL.revokeObjectURL(local.url);
  });

  void initializeBrowserLocalMedia();
})();
