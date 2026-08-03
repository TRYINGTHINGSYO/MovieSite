/* Group Theater — room client */

(() => {
  const code = document.body.dataset.code;
  if (!code) return;

  const socket = io({
    // Werkzeug/dev server can't upgrade WebSockets — polling avoids infinite hang
    transports: ["polling"],
    upgrade: false,
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

  function uploadOne(file) {
    return new Promise((resolve, reject) => {
      const form = new FormData();
      form.append("file", file);

      uploadProgress.hidden = false;
      uploadFill.style.width = "0%";
      uploadLabel.textContent = `Uploading ${file.name}…`;

      const xhr = new XMLHttpRequest();
      xhr.open("POST", `/upload/${code}`);
      xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        const pct = Math.round((e.loaded / e.total) * 100);
        uploadFill.style.width = `${pct}%`;
        uploadLabel.textContent = `Uploading ${file.name}… ${pct}%`;
      };
      xhr.onload = () => {
        uploadProgress.hidden = true;
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            const data = JSON.parse(xhr.responseText);
            if (data.state) applyState(data.state);
          } catch {
            /* queue_updated socket event will catch up */
          }
          resolve();
        } else {
          let msg = "Upload failed";
          try {
            msg = JSON.parse(xhr.responseText).error || msg;
          } catch {
            /* ignore */
          }
          alert(msg);
          reject(new Error(msg));
        }
      };
      xhr.onerror = () => {
        uploadProgress.hidden = true;
        alert("Upload failed — network error.");
        reject(new Error("network"));
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

  function showPlayerError(show) {
    if (!playerError) return;
    playerError.hidden = !show;
  }

  player.addEventListener("error", () => {
    showPlayerError(true);
  });

  player.addEventListener("loadeddata", () => {
    showPlayerError(false);
  });

  function loadCurrentVideo(seekTo, shouldPlay) {
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
        if (typeof seekTo === "number" && !Number.isNaN(seekTo)) {
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
      // If metadata never arrives (bad codec), surface an error instead of spinning forever
      const failTimer = setTimeout(() => {
        if (player.readyState < 1) {
          showPlayerError(true);
        }
      }, 8000);
      player.addEventListener(
        "loadedmetadata",
        () => {
          clearTimeout(failTimer);
          afterReady();
        },
        { once: true }
      );
      player.addEventListener(
        "error",
        () => {
          clearTimeout(failTimer);
          showPlayerError(true);
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
