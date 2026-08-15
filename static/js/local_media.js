const TOKEN_KEY = "group-theater.browser-client-token.v1";
const DATABASE_NAME = "group-theater-local-media";
const DATABASE_VERSION = 1;
const METADATA_STORE = "local_media";
const OPFS_DIRECTORY = "group-theater-media";
const OPAQUE_KEY_PATTERN = /^[a-f0-9]{64}$/;

export class LocalMediaError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "LocalMediaError";
    this.code = code;
  }
}

export function randomHex(byteLength = 32, cryptoImpl = globalThis.crypto) {
  if (!cryptoImpl?.getRandomValues || !Number.isInteger(byteLength) || byteLength < 16) {
    throw new LocalMediaError("crypto_unavailable", "Secure browser randomness is unavailable.");
  }
  const bytes = new Uint8Array(byteLength);
  cryptoImpl.getRandomValues(bytes);
  return [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
}

export function getOrCreateBrowserToken(storage = globalThis.localStorage, cryptoImpl = globalThis.crypto) {
  if (!storage) throw new LocalMediaError("storage_unavailable", "Durable browser identity storage is unavailable.");
  let token = storage.getItem(TOKEN_KEY);
  if (!OPAQUE_KEY_PATTERN.test(token || "")) {
    token = randomHex(32, cryptoImpl);
    storage.setItem(TOKEN_KEY, token);
  }
  return token;
}

export function createStorageKey(cryptoImpl = globalThis.crypto) {
  return randomHex(32, cryptoImpl);
}

export function validateStorageKey(value) {
  if (!OPAQUE_KEY_PATTERN.test(value || "")) {
    throw new LocalMediaError("invalid_storage_key", "Invalid browser-local storage key.");
  }
  return value;
}

export function pendingRegistrationBelongsTo(record, roomCode, browserClientId) {
  return Boolean(
    record
    && record.registration_status === "pending"
    && record.room_code === roomCode
    && record.browser_client_id === browserClientId,
  );
}

export async function inspectStorageCapacity(byteSize, storageManager = globalThis.navigator?.storage) {
  if (!Number.isSafeInteger(byteSize) || byteSize <= 0) {
    throw new LocalMediaError("invalid_size", "The selected file has an invalid size.");
  }
  if (!storageManager?.getDirectory || !storageManager?.estimate) {
    throw new LocalMediaError("opfs_unavailable", "Persistent browser media storage is not supported by this browser.");
  }
  const estimate = await storageManager.estimate();
  const quota = Number(estimate?.quota);
  const usage = Number(estimate?.usage || 0);
  const available = Number.isFinite(quota) ? Math.max(0, quota - usage) : null;
  if (available !== null && available < byteSize) {
    throw new LocalMediaError(
      "insufficient_quota",
      `This browser has ${available} bytes available, but the video needs ${byteSize} bytes.`,
    );
  }
  let persistent = false;
  if (typeof storageManager.persist === "function") {
    persistent = Boolean(await storageManager.persist());
  }
  return { quota: Number.isFinite(quota) ? quota : null, usage, available, persistent };
}

function requestResult(request) {
  return new Promise((resolve, reject) => {
    request.addEventListener("success", () => resolve(request.result), { once: true });
    request.addEventListener("error", () => reject(request.error || new Error("IndexedDB request failed")), { once: true });
  });
}

function transactionComplete(transaction) {
  return new Promise((resolve, reject) => {
    transaction.addEventListener("complete", resolve, { once: true });
    transaction.addEventListener("abort", () => reject(transaction.error || new Error("IndexedDB transaction aborted")), { once: true });
    transaction.addEventListener("error", () => reject(transaction.error || new Error("IndexedDB transaction failed")), { once: true });
  });
}

export class IndexedDbMetadataStore {
  constructor(indexedDb = globalThis.indexedDB) {
    if (!indexedDb) throw new LocalMediaError("indexeddb_unavailable", "IndexedDB is unavailable.");
    this.indexedDb = indexedDb;
    this.databasePromise = null;
  }

  async database() {
    if (!this.databasePromise) {
      this.databasePromise = new Promise((resolve, reject) => {
        const request = this.indexedDb.open(DATABASE_NAME, DATABASE_VERSION);
        request.addEventListener("upgradeneeded", () => {
          if (!request.result.objectStoreNames.contains(METADATA_STORE)) {
            request.result.createObjectStore(METADATA_STORE, { keyPath: "storage_key" });
          }
        });
        request.addEventListener("success", () => resolve(request.result), { once: true });
        request.addEventListener("error", () => reject(request.error || new Error("Could not open IndexedDB")), { once: true });
      });
    }
    return this.databasePromise;
  }

  async get(storageKey) {
    const db = await this.database();
    const transaction = db.transaction(METADATA_STORE, "readonly");
    return requestResult(transaction.objectStore(METADATA_STORE).get(storageKey));
  }

  async put(record) {
    const db = await this.database();
    const transaction = db.transaction(METADATA_STORE, "readwrite");
    const completion = transactionComplete(transaction);
    transaction.objectStore(METADATA_STORE).put(record);
    await completion;
    return record;
  }

  async delete(storageKey) {
    const db = await this.database();
    const transaction = db.transaction(METADATA_STORE, "readwrite");
    const completion = transactionComplete(transaction);
    transaction.objectStore(METADATA_STORE).delete(storageKey);
    await completion;
  }

  async list() {
    const db = await this.database();
    const transaction = db.transaction(METADATA_STORE, "readonly");
    return requestResult(transaction.objectStore(METADATA_STORE).getAll());
  }
}

async function copyFileToWritable(file, writable) {
  const stream = file.stream();
  if (typeof stream.pipeTo === "function") {
    await stream.pipeTo(writable);
    return;
  }
  const reader = stream.getReader();
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      await writable.write(value);
    }
    await writable.close();
  } catch (error) {
    await writable.abort?.(error);
    throw error;
  } finally {
    reader.releaseLock?.();
  }
}

export class OpfsLocalMediaStore {
  constructor({
    storageManager = globalThis.navigator?.storage,
    metadataStore = new IndexedDbMetadataStore(),
  } = {}) {
    this.storageManager = storageManager;
    this.metadataStore = metadataStore;
  }

  async mediaDirectory(create = false) {
    if (!this.storageManager?.getDirectory) {
      throw new LocalMediaError("opfs_unavailable", "Persistent browser media storage is unavailable.");
    }
    const root = await this.storageManager.getDirectory();
    return root.getDirectoryHandle(OPFS_DIRECTORY, { create });
  }

  async persist(file, metadata) {
    const storageKey = validateStorageKey(metadata?.storage_key);
    const capacity = await inspectStorageCapacity(file.size, this.storageManager);
    const directory = await this.mediaDirectory(true);
    const existingMetadata = await this.metadataStore.get(storageKey);
    if (existingMetadata) {
      throw new LocalMediaError("storage_key_in_use", "That local storage key is already in use.");
    }
    try {
      await directory.getFileHandle(storageKey, { create: false });
      throw new LocalMediaError("storage_key_in_use", "That local storage key is already in use.");
    } catch (error) {
      if (error instanceof LocalMediaError || error?.name !== "NotFoundError") throw error;
    }
    let fileCreated = false;
    try {
      const fileHandle = await directory.getFileHandle(storageKey, { create: true });
      fileCreated = true;
      const writable = await fileHandle.createWritable();
      await copyFileToWritable(file, writable);
      const record = {
        ...metadata,
        storage_key: storageKey,
        byte_size: file.size,
        mime_type: file.type || metadata.mime_type || "application/octet-stream",
        registration_status: "pending",
        updated_at: new Date().toISOString(),
      };
      await this.metadataStore.put(record);
      return { record, capacity };
    } catch (error) {
      if (fileCreated) await directory.removeEntry(storageKey).catch(() => {});
      await this.metadataStore.delete(storageKey).catch(() => {});
      throw error;
    }
  }

  async restore(storageKey) {
    validateStorageKey(storageKey);
    const metadata = await this.metadataStore.get(storageKey);
    try {
      const directory = await this.mediaDirectory(false);
      const fileHandle = await directory.getFileHandle(storageKey, { create: false });
      const file = await fileHandle.getFile();
      if (metadata?.byte_size && file.size !== metadata.byte_size) {
        return { available: false, metadata, reason: "size_mismatch" };
      }
      return { available: true, metadata, file };
    } catch (error) {
      if (error?.name !== "NotFoundError") throw error;
      return { available: false, metadata, reason: "missing" };
    }
  }

  async markRegistered(storageKey, identifiers) {
    const record = await this.metadataStore.get(validateStorageKey(storageKey));
    if (!record) throw new LocalMediaError("metadata_missing", "Local media metadata is missing.");
    const updated = {
      ...record,
      ...identifiers,
      registration_status: "registered",
      updated_at: new Date().toISOString(),
    };
    await this.metadataStore.put(updated);
    return updated;
  }

  async list() {
    return this.metadataStore.list();
  }

  async remove(storageKey) {
    validateStorageKey(storageKey);
    try {
      const directory = await this.mediaDirectory(false);
      await directory.removeEntry(storageKey);
    } catch (error) {
      if (error?.name !== "NotFoundError") throw error;
    }
    await this.metadataStore.delete(storageKey);
  }
}

export function createDefaultLocalMediaStore() {
  if (!globalThis.navigator?.storage?.getDirectory) {
    throw new LocalMediaError("opfs_unavailable", "Persistent browser media storage is unavailable.");
  }
  return new OpfsLocalMediaStore();
}

export async function probeLocalVideo(file, {
  documentObject = globalThis.document,
  urlApi = globalThis.URL,
  timeoutMs = 10000,
} = {}) {
  if (!documentObject?.createElement || !urlApi?.createObjectURL) {
    throw new LocalMediaError("media_probe_unavailable", "Local video probing is unavailable.");
  }
  const video = documentObject.createElement("video");
  const objectUrl = urlApi.createObjectURL(file);
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      globalThis.clearTimeout(timer);
      video.removeAttribute("src");
      video.load?.();
      urlApi.revokeObjectURL(objectUrl);
      callback(value);
    };
    const timer = globalThis.setTimeout(
      () => finish(reject, new LocalMediaError("media_probe_timeout", "The browser could not inspect this video.")),
      timeoutMs,
    );
    video.preload = "metadata";
    video.addEventListener("loadedmetadata", () => {
      const duration = Number(video.duration);
      finish(resolve, { duration: Number.isFinite(duration) && duration > 0 ? duration : null });
    }, { once: true });
    video.addEventListener("error", () => {
      finish(reject, new LocalMediaError("unsupported_media", "This browser cannot play the selected video file."));
    }, { once: true });
    video.src = objectUrl;
    video.load();
  });
}
