import assert from "node:assert/strict";
import test from "node:test";

import {
  LocalMediaError,
  OpfsLocalMediaStore,
  createStorageKey,
  getOrCreateBrowserToken,
  inspectStorageCapacity,
  pendingRegistrationBelongsTo,
  probeLocalVideo,
  validateStorageKey,
} from "../../static/js/local_media.js";


const KEY = "c".repeat(64);


class MemoryMetadataStore {
  constructor() {
    this.records = new Map();
  }

  async get(key) {
    return this.records.get(key);
  }

  async put(record) {
    this.records.set(record.storage_key, { ...record });
    return record;
  }

  async delete(key) {
    this.records.delete(key);
  }

  async list() {
    return [...this.records.values()].map((record) => ({ ...record }));
  }
}


function notFound() {
  return new DOMException("Not found", "NotFoundError");
}


class MemoryDirectory {
  constructor() {
    this.files = new Map();
  }

  async getFileHandle(key, { create }) {
    if (!this.files.has(key) && !create) throw notFound();
    if (!this.files.has(key)) this.files.set(key, new Uint8Array());
    return {
      createWritable: async () => new WritableStream({
        write: (chunk) => this.files.set(key, new Uint8Array(chunk)),
      }),
      getFile: async () => ({
        size: this.files.get(key).byteLength,
        type: "video/mp4",
        stream: () => new Blob([this.files.get(key)]).stream(),
      }),
    };
  }

  async removeEntry(key) {
    if (!this.files.delete(key)) throw notFound();
  }
}


function memoryStorageManager({ quota = 10_000, usage = 0, persistent = true } = {}) {
  const mediaDirectory = new MemoryDirectory();
  const root = {
    getDirectoryHandle: async (_name, { create }) => {
      if (!create && !mediaDirectory) throw notFound();
      return mediaDirectory;
    },
  };
  return {
    mediaDirectory,
    estimate: async () => ({ quota, usage }),
    persist: async () => persistent,
    getDirectory: async () => root,
  };
}


function fakeFile(bytes = [1, 2, 3, 4]) {
  const data = new Uint8Array(bytes);
  return {
    name: "movie.mp4",
    type: "video/mp4",
    size: data.byteLength,
    stream: () => new Blob([data]).stream(),
  };
}


test("durable browser token is random, opaque, and reused", () => {
  const values = new Map();
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  };
  const cryptoImpl = {
    getRandomValues: (bytes) => {
      bytes.fill(171);
      return bytes;
    },
  };
  const first = getOrCreateBrowserToken(storage, cryptoImpl);
  const second = getOrCreateBrowserToken(storage, cryptoImpl);
  assert.equal(first, "ab".repeat(32));
  assert.equal(second, first);
  assert.equal(createStorageKey(cryptoImpl), "ab".repeat(32));
  assert.equal(validateStorageKey(first), first);
  assert.throws(() => validateStorageKey("../../movie"), LocalMediaError);
});


test("quota is checked before persistence is requested", async () => {
  let persistCalled = false;
  const storageManager = {
    getDirectory: async () => ({}),
    estimate: async () => ({ quota: 100, usage: 90 }),
    persist: async () => {
      persistCalled = true;
      return true;
    },
  };
  await assert.rejects(
    inspectStorageCapacity(20, storageManager),
    (error) => error.code === "insufficient_quota",
  );
  assert.equal(persistCalled, false);
});


test("OPFS persistence restores the file and records metadata", async () => {
  const storageManager = memoryStorageManager({ persistent: false });
  const metadataStore = new MemoryMetadataStore();
  const store = new OpfsLocalMediaStore({ storageManager, metadataStore });
  const file = fakeFile();

  const persisted = await store.persist(file, {
    storage_key: KEY,
    browser_client_id: "browser-a",
    room_code: "ROOM0001",
    original_filename: "movie.mp4",
    mime_type: "video/mp4",
  });
  assert.equal(persisted.capacity.persistent, false);
  assert.equal(persisted.record.registration_status, "pending");
  assert.equal(persisted.record.byte_size, 4);
  assert.equal(
    pendingRegistrationBelongsTo(persisted.record, "ROOM0001", "browser-a"),
    true,
  );
  assert.equal(
    pendingRegistrationBelongsTo(persisted.record, "ROOM0001", "browser-b"),
    false,
  );

  const restored = await store.restore(KEY);
  assert.equal(restored.available, true);
  assert.equal(restored.file.size, 4);

  const registered = await store.markRegistered(KEY, {
    media_asset_id: "asset-1",
    room_media_id: "saved-1",
    source_id: "source-1",
  });
  assert.equal(registered.registration_status, "registered");
  assert.equal(registered.media_asset_id, "asset-1");
});


test("missing OPFS data is reported without deleting IndexedDB metadata", async () => {
  const storageManager = memoryStorageManager();
  const metadataStore = new MemoryMetadataStore();
  const store = new OpfsLocalMediaStore({ storageManager, metadataStore });
  await store.persist(fakeFile(), {
    storage_key: KEY,
    room_code: "ROOM0001",
    original_filename: "movie.mp4",
  });
  storageManager.mediaDirectory.files.delete(KEY);

  const restored = await store.restore(KEY);
  assert.equal(restored.available, false);
  assert.equal(restored.reason, "missing");
  assert.equal((await metadataStore.get(KEY)).original_filename, "movie.mp4");
});


test("explicit local cleanup removes OPFS data and IndexedDB metadata", async () => {
  const storageManager = memoryStorageManager();
  const metadataStore = new MemoryMetadataStore();
  const store = new OpfsLocalMediaStore({ storageManager, metadataStore });
  await store.persist(fakeFile(), {
    storage_key: KEY,
    room_code: "ROOM0001",
    original_filename: "movie.mp4",
  });
  await store.remove(KEY);
  assert.equal(storageManager.mediaDirectory.files.has(KEY), false);
  assert.equal(await metadataStore.get(KEY), undefined);
});


test("a reused storage key cannot overwrite an existing local file", async () => {
  const storageManager = memoryStorageManager();
  const metadataStore = new MemoryMetadataStore();
  const store = new OpfsLocalMediaStore({ storageManager, metadataStore });
  await store.persist(fakeFile([1, 2, 3, 4]), {
    storage_key: KEY,
    room_code: "ROOM0001",
    original_filename: "first.mp4",
  });

  await assert.rejects(
    store.persist(fakeFile([9, 9]), {
      storage_key: KEY,
      room_code: "ROOM0001",
      original_filename: "second.mp4",
    }),
    (error) => error.code === "storage_key_in_use",
  );
  const restored = await store.restore(KEY);
  assert.equal(restored.available, true);
  assert.equal(restored.file.size, 4);
  assert.equal(restored.metadata.original_filename, "first.mp4");
});


test("a failed metadata commit cleans up the orphaned OPFS write", async () => {
  const storageManager = memoryStorageManager();
  const metadataStore = new MemoryMetadataStore();
  metadataStore.put = async () => {
    throw new Error("IndexedDB failed");
  };
  const store = new OpfsLocalMediaStore({ storageManager, metadataStore });

  await assert.rejects(
    store.persist(fakeFile(), {
      storage_key: KEY,
      room_code: "ROOM0001",
      original_filename: "movie.mp4",
    }),
    /IndexedDB failed/,
  );
  assert.equal(storageManager.mediaDirectory.files.has(KEY), false);
  assert.equal(await metadataStore.get(KEY), undefined);
});


test("a truncated OPFS file is reported missing without deleting metadata", async () => {
  const storageManager = memoryStorageManager();
  const metadataStore = new MemoryMetadataStore();
  const store = new OpfsLocalMediaStore({ storageManager, metadataStore });
  await store.persist(fakeFile(), {
    storage_key: KEY,
    room_code: "ROOM0001",
    original_filename: "movie.mp4",
  });
  storageManager.mediaDirectory.files.set(KEY, new Uint8Array([1]));

  const restored = await store.restore(KEY);
  assert.equal(restored.available, false);
  assert.equal(restored.reason, "size_mismatch");
  assert.equal((await metadataStore.get(KEY)).byte_size, 4);
});


test("local video probing always revokes its temporary object URL", async () => {
  const listeners = new Map();
  let triggered = false;
  let revoked = null;
  const video = {
    duration: 42,
    src: "",
    addEventListener: (event, listener) => listeners.set(event, listener),
    removeAttribute: () => { video.src = ""; },
    load: () => {
      if (video.src && !triggered) {
        triggered = true;
        queueMicrotask(() => listeners.get("loadedmetadata")());
      }
    },
  };
  const result = await probeLocalVideo(fakeFile(), {
    documentObject: { createElement: () => video },
    urlApi: {
      createObjectURL: () => "blob:local-probe",
      revokeObjectURL: (url) => { revoked = url; },
    },
  });
  assert.equal(result.duration, 42);
  assert.equal(revoked, "blob:local-probe");
  assert.equal(video.src, "");
});
