(function () {
  const databaseName = "cv-optimiser-report-draft";
  const storeName = "drafts";
  const lifetime = 60 * 60 * 1000;

  async function useStore(callback, mode = "readonly") {
    let database;
    try {
      database = await new Promise(function(resolve, reject) {
        const request = indexedDB.open(databaseName, 1);
        request.onupgradeneeded = function() {
          request.result.createObjectStore(storeName);
        };
        request.onsuccess = function() { resolve(request.result); };
        request.onerror = function() { reject(request.error); };
        request.onblocked = function() { reject(new Error("Report draft storage is blocked.")); };
      });
      return await new Promise(function(resolve, reject) {
        const transaction = database.transaction(storeName, mode);
        const request = callback(transaction.objectStore(storeName));
        transaction.oncomplete = function() { resolve(request.result); };
        transaction.onabort = function() { reject(transaction.error); };
        transaction.onerror = function() { reject(transaction.error); };
      });
    } catch (error) {
      return null;
    } finally {
      if (database) database.close();
    }
  }

  async function remove(key) {
    if (key) await useStore(function(store) { return store.delete(key); }, "readwrite");
  }

  async function save(key, draft) {
    const record = { ...draft, expiresAt: Date.now() + lifetime };
    const storedKey = await useStore(function(store) { return store.put(record, key); }, "readwrite");
    if (storedKey) {
      window.setTimeout(function() { removeExpiredDrafts(); }, lifetime);
    }
    return storedKey === key;
  }

  async function read(key) {
    if (!key) return null;
    const draft = await useStore(function(store) { return store.get(key); });
    if (draft && draft.expiresAt > Date.now()) return draft;
    if (draft) await remove(key);
    return null;
  }

  function removeExpiredDrafts() {
    return useStore(function(store) {
      const request = store.openCursor();
      request.onsuccess = function() {
        const cursor = request.result;
        if (!cursor) return;
        if (cursor.value.expiresAt <= Date.now()) cursor.delete();
        cursor.continue();
      };
      return request;
    }, "readwrite");
  }

  window.cvReportDraft = { save, read, remove, removeExpiredDrafts };
})();
