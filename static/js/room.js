/* Group Theater — room client (local preview + Mux HLS for the house) */

(() => {
  const code = document.body.dataset.code;
  if (!code) return;

  const socket = io({
    transports: ["websocket", "polling"],
    reconnection: true,
    reconnectionAttempts: 10,
  });

  const dropZone = document.getElementById("drop-zone");
  const dropPrompt = document.getElementById("drop-prompt");
  const player = document.getElementById("player");
  const fileInput = document.getElementById("file-input");
  const queueList = document.getElementById("queue-list");
  const queueEmpty = document.getElementById("queue-empty");
  const viewerCountEl = document.getElementById("viewer-count");
  const seats = [...document.querySelectorAll(".seat")];
  const copyBtn = document.getElementById("copy-link");
  const copyFeedback = document.getElementById("copy-feedback");
  const addMoreBtn = document.getElementById("add-more");
  const uploadProgress = document.getElementById("upload-progress");
  const uploadFill = document.getElementById("upload-fill");
  const uploadLabel = document.getElementById("upload-label");
  const playerError = document.getElementById("player-error");
  const playerErrorDetail = document.getElementById("player-error-detail");
  const downloadBtn = document.getElementById("download-original");
  const prepareBanner = document.getElementById("prepare-banner");
  const muxPlayer = document.getElementById("mux-player");

  let applyingRemote = false;
  let state = {
    queue: [],
    current: null,
    playing: false,
    position: 0,
    viewer_count: 1,
    mux: true,
  };
  let endedHandled = false;
  let positionHeartbeat = null;
  let activeSrc = null;
  let activePlaybackId = null;
  let pollTimers = {};
  let mediaEl = player; // whichever is currently driving playback

  // Local originals for the uploader (instant play + re-download)
  const localFiles = new Map(); // video_id -> { file, blobUrl }

  function showPlayerError(show, detail) {
    if (!playerError) return;
    playerError.hidden = !show;
    if (show && playerErrorDetail && detail) {
      playerErrorDetail.textContent = detail;
    }
  }

  function setPrepareBanner(text) {
    if (!prepareBanner) return;
    if (!text) {
      prepareBanner.hidden = true;
      prepareBanner.textContent = "";
      return;
    }
    prepareBanner.hidden = false;
    prepareBanner.textContent = text;
  }

  function updateDownloadButton() {
    if (!downloadBtn) return;
    const item = state.current != null ? state.queue[state.current] : null;
    const local = item ? localFiles.get(item.id) : null;
    downloadBtn.hidden = !local;
  }

  // ---- Invite link ----

  copyBtn.addEventListener("click", async () => {
    const url = `${window.location.origin}/session/${code}`;
    try {
      await navigator.clipboard.writeText(url);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = url;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    copyFeedback.hidden = false;
    setTimeout(() => {
      copyFeedback.hidden = true;
    }, 1600);
  });

  if (downloadBtn) {
    downloadBtn.addEventListener("click", () => {
      const item = state.current != null ? state.queue[state.current] : null;
      const local = item ? localFiles.get(item.id) : null;
      if (!local) return;
      const a = document.createElement("a");
      a.href = local.blobUrl;
      a.download = local.file.name || "video";
      document.body.appendChild(a);
      a.click();
      a.remove();
    });
  }

  // ---- Upload / drop ----

  function openPicker() {
    fileInput.click();
  }

  dropZone.addEventListener("click", (e) => {
    if (e.target.closest("video") || e.target.closest(".btn") || e.target.closest("a")) return;
    if (!player.hidden) return;
    openPicker();
  });

  addMoreBtn.addEventListener("click", openPicker);

  fileInput.addEventListener("change", () => {
    if (fileInput.files?.length) uploadFiles(fileInput.files);
    fileInput.value = "";
  });

  ["dragenter", "dragover"].forEach((evt) => {
    dropZone.addEventListener(evt, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropZone.classList.add("dragover");
    });
  });

  ["dragleave", "drop"].forEach((evt) => {
    dropZone.addEventListener(evt, (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (evt === "dragleave" && dropZone.contains(e.relatedTarget)) return;
      dropZone.classList.remove("dragover");
    });
  });

  dropZone.addEventListener("drop", (e) => {
    const files = e.dataTransfer?.files;
    if (files?.length) uploadFiles(files);
  });

  function uploadFiles(fileList) {
    const files = [...fileList].filter((f) => f && f.size > 0);
    if (!files.length) {
      alert("Please drop a video file.");
      return;
    }
    files.reduce((chain, file) => chain.then(() => uploadOne(file)), Promise.resolve());
  }

  async function uploadOne(file) {
    uploadProgress.hidden = false;
    uploadFill.style.width = "0%";
    uploadLabel.textContent = `Preparing ${file.name}…`;

    let created;
    try {
      const res = await fetch(`/api/mux/create-upload/${code}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: file.name, size: file.size }),
      });
      created = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(
          created.error ||
            created.detail ||
            `Could not start upload (${res.status})`
        );
      }
    } catch (err) {
      uploadProgress.hidden = true;
      alert(err.message || "Upload failed");
      throw err;
    }

    const videoId = created.video_id;
    const blobUrl = URL.createObjectURL(file);
    localFiles.set(videoId, { file, blobUrl });

    if (created.state) applyState(created.state, { preferLocalId: videoId });

    // Instant local preview for the uploader
    const idx = state.queue.findIndex((q) => q.id === videoId);
    if (idx >= 0 && (state.current === idx || state.current === null)) {
      playLocalPreview(blobUrl, true);
      setPrepareBanner("Playing your copy now — preparing a shared stream for everyone…");
    }

    uploadLabel.textContent = `Uploading ${file.name}…`;

    try {
      await putToMux(created.upload_url, file);
      uploadFill.style.width = "100%";
      uploadLabel.textContent = "Processing for the room…";

      await fetch(`/api/mux/uploaded/${code}/${videoId}`, { method: "POST" });
      startStatusPoll(videoId);
    } catch (err) {
      uploadProgress.hidden = true;
      setPrepareBanner("");
      alert(err.message || "Upload to Mux failed");
      throw err;
    }
  }

  function putToMux(uploadUrl, file) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("PUT", uploadUrl);
      xhr.timeout = 60 * 60 * 1000;
      if (file.type) xhr.setRequestHeader("Content-Type", file.type);

      xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        const pct = Math.round((e.loaded / e.total) * 100);
        uploadFill.style.width = `${pct}%`;
        uploadLabel.textContent = `Uploading ${file.name}… ${pct}%`;
      };

      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) resolve();
        else reject(new Error(`Mux upload failed (HTTP ${xhr.status})`));
      };
      xhr.onerror = () => reject(new Error("Network error while uploading to Mux"));
      xhr.ontimeout = () => reject(new Error("Mux upload timed out"));
      xhr.send(file);
    });
  }

  function startStatusPoll(videoId) {
    if (pollTimers[videoId]) clearInterval(pollTimers[videoId]);
    let ticks = 0;
    pollTimers[videoId] = setInterval(async () => {
      ticks += 1;
      try {
        const res = await fetch(`/api/mux/status/${code}/${videoId}`);
        const data = await res.json();
        if (data.state) applyState(data.state);
        const item = (data.state?.queue || state.queue).find((q) => q.id === videoId);
        if (!item) {
          clearInterval(pollTimers[videoId]);
          delete pollTimers[videoId];
          return;
        }
        if (item.status === "ready") {
          clearInterval(pollTimers[videoId]);
          delete pollTimers[videoId];
          uploadProgress.hidden = true;
          setPrepareBanner("");
        } else if (item.status === "error") {
          clearInterval(pollTimers[videoId]);
          delete pollTimers[videoId];
          uploadProgress.hidden = true;
          setPrepareBanner("");
          showPlayerError(true, item.error || "Mux could not process this video.");
        } else if (ticks > 180) {
          clearInterval(pollTimers[videoId]);
          delete pollTimers[videoId];
          uploadProgress.hidden = true;
          setPrepareBanner("Still processing — the room will update when ready.");
        }
      } catch {
        /* keep polling */
      }
    }, 2000);
  }

  // ---- Queue UI ----

  function statusLabel(item) {
    if (item.status === "ready") return "";
    if (item.status === "uploading") return " · uploading";
    if (item.status === "processing") return " · processing";
    if (item.status === "error") return " · error";
    return item.status ? ` · ${item.status}` : "";
  }

  function renderQueue() {
    // Remove whole list rows (not just the button) — leaving empty <li>s
    // was pushing the queue further down on every upload/status update.
    queueList.querySelectorAll("li:not(#queue-empty)").forEach((el) => el.remove());

    if (!state.queue.length) {
      queueEmpty.hidden = false;
      return;
    }
    queueEmpty.hidden = true;

    state.queue.forEach((item, index) => {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "queue-item" + (state.current === index ? " active" : "");
      btn.innerHTML =
        `<span class="queue-index">${String(index + 1).padStart(2, "0")}</span>` +
        `<span class="queue-name"></span>`;
      btn.querySelector(".queue-name").textContent = `${item.name}${statusLabel(item)}`;
      btn.addEventListener("click", () => {
        socket.emit("select_video", { code, index });
      });
      li.appendChild(btn);
      queueList.appendChild(li);
    });
  }

  function updateSeats(count) {
    const n = Math.max(0, Math.min(seats.length, count | 0));
    seats.forEach((seat, i) => {
      seat.classList.toggle("occupied", i < n);
    });
  }

  function setViewerCount(count) {
    viewerCountEl.textContent = String(count);
    updateSeats(count);
  }

  // ---- Player ----

  function bindMediaEvents(el) {
    if (!el || el._theaterBound) return;
    el._theaterBound = true;
    el.addEventListener("play", () => {
      if (applyingRemote) return;
      socket.emit("play", { code, position: el.currentTime || 0 });
    });
    el.addEventListener("pause", () => {
      if (applyingRemote) return;
      if (el.ended) return;
      socket.emit("pause", { code, position: el.currentTime || 0 });
    });
    el.addEventListener("seeked", () => {
      if (applyingRemote) return;
      socket.emit("seek", { code, position: el.currentTime || 0 });
    });
    el.addEventListener("ended", () => {
      if (endedHandled) return;
      endedHandled = true;
      const item = state.current != null ? state.queue[state.current] : null;
      // Keep local original available for download until room advances
      socket.emit("video_ended", { code, index: state.current });
    });
  }

  bindMediaEvents(player);
  if (muxPlayer) bindMediaEvents(muxPlayer);

  function showLocalPlayer() {
    player.hidden = false;
    if (muxPlayer) muxPlayer.hidden = true;
    mediaEl = player;
  }

  function showMuxPlayer() {
    player.hidden = true;
    player.removeAttribute("src");
    player.load();
    if (muxPlayer) muxPlayer.hidden = false;
    mediaEl = muxPlayer || player;
  }

  function tryPlay(el = mediaEl) {
    if (!el) return;
    const p = el.play();
    if (p && typeof p.catch === "function") {
      p.catch(() => {
        el.addEventListener(
          "canplay",
          () => {
            el.play().catch(() => {});
          },
          { once: true }
        );
      });
    }
  }

  function playLocalPreview(blobUrl, shouldPlay) {
    showLocalPlayer();
    dropPrompt.hidden = true;
    dropZone.classList.add("has-video");
    showPlayerError(false);
    activeSrc = blobUrl;
    activePlaybackId = null;
    player.src = blobUrl;
    player.load();
    if (shouldPlay) tryPlay(player);
    updateDownloadButton();
  }

  function attachMuxStream(playbackId, seekTo, shouldPlay) {
    if (!muxPlayer || !playbackId) {
      showPlayerError(true, "Shared stream unavailable.");
      return;
    }

    dropPrompt.hidden = true;
    dropZone.classList.add("has-video");
    showPlayerError(false);
    endedHandled = false;
    showMuxPlayer();

    const same = activePlaybackId === playbackId;
    activePlaybackId = playbackId;
    activeSrc = playbackId;
    muxPlayer.playbackId = playbackId;
    muxPlayer.setAttribute("playback-id", playbackId);

    const afterReady = () => {
      applyingRemote = true;
      try {
        if (typeof seekTo === "number" && seekTo > 0.35) {
          muxPlayer.currentTime = seekTo;
        }
        if (shouldPlay) tryPlay(muxPlayer);
        else muxPlayer.pause();
      } finally {
        setTimeout(() => {
          applyingRemote = false;
        }, 200);
      }
    };

    if (same && muxPlayer.readyState >= 2) {
      afterReady();
      return;
    }

    const onError = () => {
      // Host keeps their saved original if Mux player fails
      const item = state.current != null ? state.queue[state.current] : null;
      const local = item ? localFiles.get(item.id) : null;
      if (local) {
        setPrepareBanner("Shared stream hiccup — playing your saved original.");
        playLocalPreview(local.blobUrl, shouldPlay);
        return;
      }
      showPlayerError(
        true,
        "Could not play the shared stream. Guests: refresh. Host: use Download original / re-add."
      );
    };

    muxPlayer.addEventListener("loadedmetadata", afterReady, { once: true });
    muxPlayer.addEventListener("error", onError, { once: true });
    // Mux Player sometimes needs a tick after setting playback-id
    setTimeout(() => {
      if (shouldPlay) tryPlay(muxPlayer);
    }, 50);
  }

  function loadCurrentVideo(seekTo, shouldPlay, opts = {}) {
    const item = state.current != null ? state.queue[state.current] : null;
    if (!item) {
      player.hidden = true;
      player.removeAttribute("src");
      player.load();
      if (muxPlayer) {
        muxPlayer.hidden = true;
        muxPlayer.removeAttribute("playback-id");
      }
      dropPrompt.hidden = false;
      dropZone.classList.remove("has-video");
      showPlayerError(false);
      setPrepareBanner("");
      activeSrc = null;
      activePlaybackId = null;
      updateDownloadButton();
      return;
    }

    updateDownloadButton();
    const local = localFiles.get(item.id);

    // Shared Mux stream for the whole room
    if (item.status === "ready" && item.playback_id) {
      setPrepareBanner("");
      uploadProgress.hidden = true;
      if (activePlaybackId === item.playback_id && mediaEl === muxPlayer) {
        applyingRemote = true;
        try {
          if (typeof seekTo === "number" && Math.abs((muxPlayer.currentTime || 0) - seekTo) > 0.75) {
            muxPlayer.currentTime = seekTo;
          }
          if (shouldPlay && muxPlayer.paused) tryPlay(muxPlayer);
          if (!shouldPlay && !muxPlayer.paused) muxPlayer.pause();
        } finally {
          setTimeout(() => {
            applyingRemote = false;
          }, 80);
        }
        return;
      }
      attachMuxStream(item.playback_id, seekTo || 0, shouldPlay);
      return;
    }

    if (item.status === "error") {
      if (local) {
        setPrepareBanner("Processing failed — playing your saved original.");
        playLocalPreview(local.blobUrl, shouldPlay);
        return;
      }
      showPlayerError(true, item.error || "This video failed to process.");
      setPrepareBanner("");
      return;
    }

    // Host local preview while uploading/processing — original stays saved in memory
    if (local && (opts.preferLocalId === item.id || item.status !== "ready")) {
      setPrepareBanner(
        item.status === "processing"
          ? "Playing your saved copy — shared stream is processing on Mux…"
          : "Playing your saved copy — uploading to Mux for the room…"
      );
      if (activeSrc !== local.blobUrl) {
        playLocalPreview(local.blobUrl, shouldPlay);
      } else if (shouldPlay) {
        tryPlay(player);
      }
      return;
    }

    // Guests waiting on Mux
    dropPrompt.hidden = true;
    player.hidden = false;
    dropZone.classList.add("has-video");
    if (muxPlayer) muxPlayer.hidden = true;
    setPrepareBanner(`“${item.name}” is saving & processing for the room…`);
  }

  function applyState(next, opts = {}) {
    const prevId =
      state.current != null && state.queue[state.current]
        ? state.queue[state.current].id
        : null;

    state = { ...state, ...next };
    renderQueue();
    if (typeof next.viewer_count === "number") setViewerCount(next.viewer_count);

    const newId =
      state.current != null && state.queue[state.current]
        ? state.queue[state.current].id
        : null;

    const item = state.current != null ? state.queue[state.current] : null;
    const srcKey =
      item?.status === "ready" && item.playback_id ? item.playback_id : newId;

    if (newId !== prevId || srcKey !== activeSrc || opts.preferLocalId) {
      loadCurrentVideo(state.position || 0, state.playing, opts);
    } else if (item) {
      loadCurrentVideo(state.position || 0, state.playing, opts);
    } else {
      loadCurrentVideo(0, false);
    }
  }

  // ---- Socket handlers ----

  socket.on("connect", () => {
    socket.emit("join", { code });
  });

  socket.on("state_sync", (payload) => applyState(payload));
  socket.on("queue_updated", (payload) => applyState(payload));
  socket.on("video_selected", (payload) => {
    endedHandled = false;
    applyState(payload);
  });

  socket.on("viewer_count", (payload) => {
    setViewerCount(payload.count);
    state.viewer_count = payload.count;
  });

  socket.on("play", (payload) => {
    applyingRemote = true;
    try {
      const el = mediaEl || player;
      if (typeof payload.position === "number" && Math.abs((el.currentTime || 0) - payload.position) > 0.4) {
        el.currentTime = payload.position;
      }
      el.play?.().catch?.(() => {});
    } finally {
      setTimeout(() => {
        applyingRemote = false;
      }, 80);
    }
  });

  socket.on("pause", (payload) => {
    applyingRemote = true;
    try {
      const el = mediaEl || player;
      if (typeof payload.position === "number" && Math.abs((el.currentTime || 0) - payload.position) > 0.4) {
        el.currentTime = payload.position;
      }
      el.pause?.();
    } finally {
      setTimeout(() => {
        applyingRemote = false;
      }, 80);
    }
  });

  socket.on("seek", (payload) => {
    applyingRemote = true;
    try {
      const el = mediaEl || player;
      if (typeof payload.position === "number") el.currentTime = payload.position;
      if (payload.playing) el.play?.().catch?.(() => {});
    } finally {
      setTimeout(() => {
        applyingRemote = false;
      }, 80);
    }
  });

  socket.on("error", (payload) => {
    console.warn(payload?.message || "Socket error");
  });

  positionHeartbeat = setInterval(() => {
    const el = mediaEl;
    if (!el || el.hidden) return;
    socket.emit("sync_position", {
      code,
      position: el.currentTime || 0,
      playing: !el.paused && !el.ended,
    });
  }, 4000);

  window.addEventListener("beforeunload", () => {
    if (positionHeartbeat) clearInterval(positionHeartbeat);
    Object.values(pollTimers).forEach(clearInterval);
  });

  renderQueue();
  updateSeats(1);
  updateDownloadButton();
})();
