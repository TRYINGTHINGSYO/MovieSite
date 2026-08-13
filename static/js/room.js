/* Group Theater Stage 2B room client. */

(() => {
  "use strict";

  const code = document.body.dataset.code;
  if (!code) return;
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const $ = (id) => document.getElementById(id);
  const player = $("player");
  const muxPlayer = $("mux-player");
  const dropPrompt = $("drop-prompt");
  const dropZone = $("drop-zone");
  const notice = $("room-notice");
  const localFiles = new Map();
  const pollTimers = new Map();
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
    queue_version: 0,
    playback_version: 0,
    viewer_count: 1,
  };

  const socket = io({
    transports: ["websocket", "polling"],
    reconnection: true,
    reconnectionAttempts: 10,
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
      client_action_id: randomId(),
    });
  }

  function sourceFor(item) {
    if (!item) return null;
    if (item.source_type) return item;
    return (item.sources || []).find((source) => source.status === "ready") || item.sources?.[0] || null;
  }

  function currentItem() {
    return state.queue.find((item) => item.id === state.current_id) || null;
  }

  function authoritativePosition() {
    const base = Number.isFinite(Number(state.position)) ? Number(state.position) : 0;
    const elapsed = state.playing ? Math.max(0, performance.now() - stateReceivedAt) / 1000 : 0;
    return Math.max(0, base + elapsed);
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

  function showNoMedia() {
    activeSourceKey = null;
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

    if (local && source?.status !== "ready") {
      setPlayerMessage("Playing your local upload while Mux prepares the shared stream.");
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
    if (changed) {
      player.src = url;
      player.load();
    }
    const targetPosition = authoritativePosition();
    if (Math.abs((player.currentTime || 0) - targetPosition) > 0.75) {
      try { player.currentTime = targetPosition; } catch { /* source is not seekable yet */ }
    }
    if (state.playing) tryPlay(player); else player.pause();
    window.setTimeout(() => { applyingRemote = false; }, 150);
  }

  function attachMux(playbackId) {
    const key = `mux:${playbackId}`;
    const changed = activeSourceKey !== key;
    activeSourceKey = key;
    player.hidden = true;
    muxPlayer.hidden = false;
    mediaElement = muxPlayer;
    applyingRemote = true;
    if (changed) {
      muxPlayer.playbackId = playbackId;
      muxPlayer.setAttribute("playback-id", playbackId);
      muxPlayer.setAttribute("poster", `https://image.mux.com/${playbackId}/thumbnail.webp?time=1`);
    }
    const targetPosition = authoritativePosition();
    try { if (Math.abs((muxPlayer.currentTime || 0) - targetPosition) > 0.75) muxPlayer.currentTime = targetPosition; } catch { /* wait for metadata */ }
    if (state.playing) tryPlay(muxPlayer); else muxPlayer.pause();
    window.setTimeout(() => { applyingRemote = false; }, 180);
  }

  function renderLibrary() {
    const list = $("library-list");
    list.replaceChildren();
    $("library-empty").hidden = state.library.length > 0;
    for (const item of state.library) {
      const row = node("li", "resource-row");
      const summary = node("div", "resource-summary");
      summary.append(node("strong", "resource-title", item.name));
      const source = sourceFor(item);
      summary.append(node("span", "resource-meta", `${source?.source_type || "source"} · ${source?.status || "unknown"}`));
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
    return item.request_type.replaceAll("_", " ").toLowerCase();
  }

  function renderRequests() {
    const list = $("requests-list");
    list.replaceChildren();
    $("requests-empty").hidden = state.requests.length > 0;
    for (const item of state.requests) {
      const row = node("li", "request-row");
      const summary = node("div", "resource-summary");
      summary.append(node("strong", "resource-title", item.requester_label));
      summary.append(node("span", "resource-meta", `${describeRequest(item)} · ${item.status}`));
      row.append(summary);
      if (can("REVIEW_REQUESTS") && item.status === "pending") {
        const actions = node("div", "resource-actions");
        for (const decision of ["approved", "denied", "dismissed"]) {
          actions.append(button(decision, "btn btn-sm btn-ghost", () => {
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

  function applyState(next) {
    if (!next || typeof next !== "object") return;
    state = { ...state, ...next };
    stateReceivedAt = performance.now();
    const controlsPlayback = can("CONTROL_PLAYBACK");
    player.controls = controlsPlayback;
    muxPlayer.controls = controlsPlayback;
    muxPlayer.toggleAttribute("controls", controlsPlayback);
    $("viewer-count").textContent = String(state.viewer_count || 0);
    $("playback-mode").textContent = controlsPlayback ? "You control playback" : "Actions send requests";
    $("upload-media").hidden = !can("ADD_MEDIA");
    $("direct-submit").textContent = can("ADD_MEDIA") ? "Probe & save" : "Probe & request";
    renderLibrary();
    renderQueue();
    renderPeople();
    renderRequests();
    loadCurrent();
    ensureMuxPolling();
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
    const title = $("direct-title").value.trim() || "Direct media";
    const url = $("direct-url").value.trim();
    $("probe-result").textContent = "Probing in this browser…";
    try {
      const probeResult = await probeDirectUrl(url);
      $("probe-result").textContent = probeResult.replaceAll("_", " ");
      if (!["playable", "playable_no_seek"].includes(probeResult)) {
        throw new Error("This URL did not pass the browser playback probe and was not saved.");
      }
      if (can("ADD_MEDIA")) {
        await api(`/api/rooms/${code}/media/direct-url`, {
          method: "POST",
          body: JSON.stringify({ title, url, probe_result: probeResult }),
        });
        inform("Direct media saved. Add it to the queue when ready.");
      } else {
        await submitRequest("ADD_DIRECT_URL", { title, url, probe_result: probeResult });
      }
      $("direct-url-form").reset();
    } catch (error) {
      inform(error.message, "error");
    }
  });

  $("upload-media").addEventListener("click", () => $("file-input").click());
  $("file-input").addEventListener("change", () => {
    const files = [...($("file-input").files || [])];
    $("file-input").value = "";
    files.reduce((chain, file) => chain.then(() => uploadMux(file)), Promise.resolve())
      .catch((error) => inform(error.message, "error"));
  });

  async function uploadMux(file) {
    $("upload-progress").hidden = false;
    $("upload-label").textContent = `Preparing ${file.name}…`;
    const created = await api(`/api/mux/create-upload/${code}`, {
      method: "POST",
      body: JSON.stringify({ filename: file.name, size: file.size, save_only: true }),
    });
    const localUrl = URL.createObjectURL(file);
    localFiles.set(created.room_media_id, { file, url: localUrl });
    await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("PUT", created.upload_url);
      xhr.upload.onprogress = (event) => {
        if (!event.lengthComputable) return;
        const percentage = Math.round((event.loaded / event.total) * 100);
        $("upload-fill").style.width = `${percentage}%`;
        $("upload-label").textContent = `Uploading ${file.name}… ${percentage}%`;
      };
      xhr.onload = () => xhr.status >= 200 && xhr.status < 300 ? resolve() : reject(new Error(`Mux upload failed (${xhr.status})`));
      xhr.onerror = () => reject(new Error("Network error while uploading to Mux"));
      xhr.send(file);
    });
    await api(`/api/mux/uploaded/${code}/${created.video_id}`, { method: "POST" });
    $("upload-progress").hidden = true;
    pollMux(created.video_id);
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
  $("tap-to-play").addEventListener("click", () => { $("tap-to-play").hidden = true; tryPlay(mediaElement); });

  for (const element of [player, muxPlayer]) {
    const restoreAuthoritativePlayback = () => {
      if (
        applyingRemote
        || can("CONTROL_PLAYBACK")
        || element !== mediaElement
        || !currentItem()
      ) return;
      applyingRemote = true;
      try {
        const targetPosition = authoritativePosition();
        if (Math.abs((element.currentTime || 0) - targetPosition) > 0.25) {
          element.currentTime = targetPosition;
        }
        if (state.playing) tryPlay(element); else element.pause();
      } finally {
        window.setTimeout(() => { applyingRemote = false; }, 150);
      }
    };
    element.addEventListener("play", restoreAuthoritativePlayback);
    element.addEventListener("pause", restoreAuthoritativePlayback);
    element.addEventListener("seeking", restoreAuthoritativePlayback);
    element.addEventListener("ended", () => {
      if (!applyingRemote && can("CONTROL_PLAYBACK")) playbackAction("next");
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
})();
