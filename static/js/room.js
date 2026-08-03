/* Group Theater — room client */

(() => {
  const code = document.body.dataset.code;
  if (!code) return;

  const socket = io({
    // Prefer WebSocket so Engine.IO does not hold HTTP workers that video
    // Range requests need. Polling remains a fallback.
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

  let applyingRemote = false;
  let state = {
    queue: [],
    current: null,
    playing: false,
    position: 0,
    viewer_count: 1,
  };
  let endedHandled = false;
  let positionHeartbeat = null;
  let loadFailTimer = null;

  const CODEC_HINT =
    "This browser can't decode that video. Try an MP4 (H.264) export — iPhone screen recordings (.mov) are often HEVC and won't play in Chrome.";
  const NETWORK_HINT =
    "The video file didn't load from the server. Re-upload or try a smaller MP4 (H.264).";

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

  // ---- Upload / drop ----

  function openPicker() {
    fileInput.click();
  }

  dropZone.addEventListener("click", (e) => {
    if (e.target.closest("video") || e.target.closest(".btn")) return;
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
    // Accept any dropped/picked file — no extension whitelist
    const files = [...fileList].filter((f) => f && f.size > 0);
    if (!files.length) {
      alert("Please drop a video file.");
      return;
    }
    files.reduce((chain, file) => chain.then(() => uploadOne(file)), Promise.resolve());
  }

  // Railway closes request bodies after 5 minutes — send small chunks instead.
  const CHUNK_SIZE = 2 * 1024 * 1024;

  function uploadOne(file) {
    const uploadId =
      typeof crypto !== "undefined" && crypto.randomUUID
        ? crypto.randomUUID()
        : `${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`;
    const total = Math.max(1, Math.ceil(file.size / CHUNK_SIZE));

    uploadProgress.hidden = false;
    uploadFill.style.width = "0%";
    uploadLabel.textContent = `Uploading ${file.name}…`;

    let chain = Promise.resolve();
    for (let index = 0; index < total; index += 1) {
      const start = index * CHUNK_SIZE;
      const blob = file.slice(start, Math.min(start + CHUNK_SIZE, file.size));
      chain = chain.then(() =>
        postChunk({
          file,
          blob,
          uploadId,
          index,
          total,
          bytesSentBefore: start,
        })
      );
    }

    return chain
      .then((data) => {
        uploadProgress.hidden = true;
        if (data?.state) applyState(data.state);
      })
      .catch((err) => {
        uploadProgress.hidden = true;
        alert(err?.message || "Upload failed.");
        throw err;
      });
  }

  function postChunk({ file, blob, uploadId, index, total, bytesSentBefore }) {
    return new Promise((resolve, reject) => {
      const form = new FormData();
      form.append("file", blob, file.name);
      form.append("filename", file.name);
      form.append("upload_id", uploadId);
      form.append("chunk_index", String(index));
      form.append("chunk_total", String(total));

      const xhr = new XMLHttpRequest();
      xhr.open("POST", `/upload/${code}`);
      // Each chunk should finish well under Railway's 5-minute body limit
      xhr.timeout = 4 * 60 * 1000;

      xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        const sent = bytesSentBefore + e.loaded;
        const pct = Math.min(100, Math.round((sent / file.size) * 100));
        uploadFill.style.width = `${pct}%`;
        uploadLabel.textContent = `Uploading ${file.name}… ${pct}%`;
      };

      xhr.onload = () => {
        let payload = null;
        try {
          payload = JSON.parse(xhr.responseText);
        } catch {
          /* ignore */
        }
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(payload || { ok: true });
          return;
        }
        const msg =
          (payload && payload.error) ||
          `Upload failed (HTTP ${xhr.status || "error"}).`;
        reject(new Error(msg));
      };

      xhr.onerror = () => {
        reject(
          new Error(
            "Upload failed — connection dropped. Try a smaller file or check your network."
          )
        );
      };
      xhr.ontimeout = () => {
        reject(new Error("Upload timed out. Try again on a faster connection."));
      };
      xhr.send(form);
    });
  }

  // ---- Queue UI ----

  function renderQueue() {
    queueList.querySelectorAll(".queue-item").forEach((el) => el.remove());

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
      btn.innerHTML = `<span class="queue-index">${String(index + 1).padStart(2, "0")}</span><span class="queue-name"></span>`;
      btn.querySelector(".queue-name").textContent = item.name;
      btn.addEventListener("click", () => {
        socket.emit("select_video", { code, index });
      });
      li.appendChild(btn);
      queueList.appendChild(li);
    });
  }

  // ---- Seats / presence ----

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

  // ---- Player / sync ----

  function tryPlay() {
    const p = player.play();
    if (p && typeof p.catch === "function") {
      p.catch(() => {
        player.addEventListener(
          "canplay",
          () => {
            player.play().catch(() => {});
          },
          { once: true }
        );
      });
    }
  }

  function showPlayerError(show, detail) {
    if (!playerError) return;
    playerError.hidden = !show;
    if (show && playerErrorDetail && detail) {
      playerErrorDetail.textContent = detail;
    }
  }

  function clearLoadFailTimer() {
    if (loadFailTimer) {
      clearTimeout(loadFailTimer);
      clearInterval(loadFailTimer);
      loadFailTimer = null;
    }
  }

  function mediaErrorHint() {
    const err = player.error;
    const name = (state.queue[state.current]?.name || "").toLowerCase();
    if (err && err.code === MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED) {
      return CODEC_HINT;
    }
    if (err && (err.code === MediaError.MEDIA_ERR_NETWORK || err.code === MediaError.MEDIA_ERR_ABORTED)) {
      return NETWORK_HINT;
    }
    if (name.endsWith(".mov") || name.endsWith(".mkv") || name.endsWith(".avi")) {
      return CODEC_HINT;
    }
    return CODEC_HINT;
  }

  player.addEventListener("error", () => {
    clearLoadFailTimer();
    showPlayerError(true, mediaErrorHint());
  });

  player.addEventListener("playing", () => {
    clearLoadFailTimer();
    showPlayerError(false);
  });

  player.addEventListener("loadeddata", () => {
    showPlayerError(false);
  });

  function loadCurrentVideo(seekTo, shouldPlay) {
    clearLoadFailTimer();
    if (state.current === null || !state.queue[state.current]) {
      player.hidden = true;
      player.removeAttribute("src");
      player.load();
      dropPrompt.hidden = false;
      dropZone.classList.remove("has-video");
      showPlayerError(false);
      return;
    }

    const item = state.queue[state.current];
    const nextSrc = item.url;
    const needsReload = player.getAttribute("src") !== nextSrc;

    dropPrompt.hidden = true;
    player.hidden = false;
    dropZone.classList.add("has-video");
    endedHandled = false;
    showPlayerError(false);

    const afterReady = () => {
      applyingRemote = true;
      try {
        // Avoid seek(0) before buffer exists — it triggers extra Range churn.
        if (
          typeof seekTo === "number" &&
          !Number.isNaN(seekTo) &&
          seekTo > 0.5 &&
          Math.abs(player.currentTime - seekTo) > 0.4
        ) {
          player.currentTime = seekTo;
        }
        if (shouldPlay) {
          tryPlay();
        } else {
          player.pause();
        }
      } finally {
        setTimeout(() => {
          applyingRemote = false;
        }, 120);
      }
    };

    if (needsReload) {
      player.src = nextSrc;
      // Duration can appear (metadata) while media bytes still stall — wait for canplay.
      let lastBuffered = 0;
      let stallTicks = 0;
      const onCanPlay = () => {
        clearLoadFailTimer();
        afterReady();
      };
      player.addEventListener("canplay", onCanPlay, { once: true });

      // Poll buffer growth for large files instead of a single short timeout.
      loadFailTimer = setInterval(() => {
        if (player.readyState >= 3 || (!player.paused && !player.ended && player.currentTime > 0.1)) {
          clearLoadFailTimer();
          player.removeEventListener("canplay", onCanPlay);
          afterReady();
          return;
        }
        let bufferedEnd = 0;
        try {
          if (player.buffered.length) {
            bufferedEnd = player.buffered.end(player.buffered.length - 1);
          }
        } catch {
          /* ignore */
        }
        if (bufferedEnd > lastBuffered + 0.05) {
          lastBuffered = bufferedEnd;
          stallTicks = 0;
          showPlayerError(false);
          return;
        }
        stallTicks += 1;
        // ~30s with no buffer growth after metadata → surface error
        if (stallTicks >= 15 && player.readyState >= 1) {
          clearLoadFailTimer();
          player.removeEventListener("canplay", onCanPlay);
          showPlayerError(true, NETWORK_HINT);
        } else if (stallTicks >= 20 && player.readyState < 1) {
          clearLoadFailTimer();
          player.removeEventListener("canplay", onCanPlay);
          showPlayerError(true, mediaErrorHint());
        }
      }, 2000);

      player.addEventListener(
        "error",
        () => {
          clearLoadFailTimer();
          player.removeEventListener("canplay", onCanPlay);
          showPlayerError(true, mediaErrorHint());
        },
        { once: true }
      );
      player.load();
    } else {
      afterReady();
    }
  }

  function applyState(next) {
    const prevCurrent = state.current;
    const prevSrc =
      state.current !== null && state.queue[state.current]
        ? state.queue[state.current].url
        : null;

    state = { ...state, ...next };
    renderQueue();
    if (typeof next.viewer_count === "number") {
      setViewerCount(next.viewer_count);
    }

    const newSrc =
      state.current !== null && state.queue[state.current]
        ? state.queue[state.current].url
        : null;

    if (newSrc !== prevSrc || state.current !== prevCurrent) {
      loadCurrentVideo(state.position || 0, state.playing);
    } else if (newSrc) {
      // Same video — nudge play/pause/position if needed
      applyingRemote = true;
      try {
        if (Math.abs(player.currentTime - (state.position || 0)) > 0.75) {
          player.currentTime = state.position || 0;
        }
        if (state.playing && player.paused) player.play().catch(() => {});
        if (!state.playing && !player.paused) player.pause();
      } finally {
        setTimeout(() => {
          applyingRemote = false;
        }, 80);
      }
    } else {
      loadCurrentVideo(0, false);
    }
  }

  player.addEventListener("play", () => {
    if (applyingRemote) return;
    socket.emit("play", { code, position: player.currentTime });
  });

  player.addEventListener("pause", () => {
    if (applyingRemote) return;
    if (player.ended) return;
    socket.emit("pause", { code, position: player.currentTime });
  });

  player.addEventListener("seeking", () => {
    if (applyingRemote) return;
  });

  player.addEventListener("seeked", () => {
    if (applyingRemote) return;
    socket.emit("seek", { code, position: player.currentTime });
  });

  player.addEventListener("ended", () => {
    if (endedHandled) return;
    endedHandled = true;
    socket.emit("video_ended", { code, index: state.current });
  });

  // Quiet position heartbeat so late joiners land near the right frame
  positionHeartbeat = setInterval(() => {
    if (player.hidden || !player.src) return;
    socket.emit("sync_position", {
      code,
      position: player.currentTime,
      playing: !player.paused && !player.ended,
    });
  }, 4000);

  window.addEventListener("beforeunload", () => {
    if (positionHeartbeat) clearInterval(positionHeartbeat);
  });

  // ---- Socket handlers ----

  socket.on("connect", () => {
    socket.emit("join", { code });
  });

  socket.on("state_sync", (payload) => {
    applyState(payload);
  });

  socket.on("queue_updated", (payload) => {
    applyState(payload);
  });

  socket.on("video_selected", (payload) => {
    applyState(payload);
  });

  socket.on("viewer_count", (payload) => {
    setViewerCount(payload.count);
    state.viewer_count = payload.count;
  });

  socket.on("play", (payload) => {
    applyingRemote = true;
    try {
      if (typeof payload.position === "number") {
        if (Math.abs(player.currentTime - payload.position) > 0.4) {
          player.currentTime = payload.position;
        }
      }
      player.play().catch(() => {});
    } finally {
      setTimeout(() => {
        applyingRemote = false;
      }, 80);
    }
  });

  socket.on("pause", (payload) => {
    applyingRemote = true;
    try {
      if (typeof payload.position === "number") {
        if (Math.abs(player.currentTime - payload.position) > 0.4) {
          player.currentTime = payload.position;
        }
      }
      player.pause();
    } finally {
      setTimeout(() => {
        applyingRemote = false;
      }, 80);
    }
  });

  socket.on("seek", (payload) => {
    applyingRemote = true;
    try {
      if (typeof payload.position === "number") {
        player.currentTime = payload.position;
      }
      if (payload.playing) {
        player.play().catch(() => {});
      }
    } finally {
      setTimeout(() => {
        applyingRemote = false;
      }, 80);
    }
  });

  socket.on("error", (payload) => {
    console.warn(payload?.message || "Socket error");
  });

  // Initial empty queue render
  renderQueue();
  updateSeats(1);
})();
