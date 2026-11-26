import assert from "node:assert/strict";
import test from "node:test";

import {
  CHAT_ENDPOINT_UNAVAILABLE_ERROR,
  CHAT_MAX_MESSAGE_LENGTH,
  CHAT_ROLE_ASSISTANT,
  CHAT_ROLE_USER,
  CHAT_STORAGE_KEY,
  DEFAULT_CHAT_ERROR,
  appendChatMessage,
  buildChatRequestPayload,
  extractChatErrorMessage,
  extractChatReplyText,
  generateThreadId,
  loadChatState,
  resetChatThread,
  saveChatState,
  validateChatDraft,
} from "./aiChatState.js";

function createMemoryStorage(initialEntries = {}) {
  const store = new Map(Object.entries(initialEntries));
  return {
    getItem(key) {
      return store.has(key) ? store.get(key) : null;
    },
    setItem(key, value) {
      store.set(key, String(value));
    },
  };
}

test("invariant 1: whitespace-only and oversized drafts are rejected", () => {
  const emptyResult = validateChatDraft({
    draftText: "   \n\t  ",
    isSending: false,
  });
  assert.equal(emptyResult.isValid, false);
  assert.equal(emptyResult.normalizedMessage, "");
  assert.ok(emptyResult.error);

  const oversizedText = "а".repeat(CHAT_MAX_MESSAGE_LENGTH + 1);
  const oversizedResult = validateChatDraft({
    draftText: oversizedText,
    isSending: false,
  });
  assert.equal(oversizedResult.isValid, false);
  assert.ok(oversizedResult.error.includes(String(CHAT_MAX_MESSAGE_LENGTH)));

  const exactLimitText = `  ${"а".repeat(CHAT_MAX_MESSAGE_LENGTH)}  `;
  const validResult = validateChatDraft({
    draftText: exactLimitText,
    isSending: false,
  });
  assert.equal(validResult.isValid, true);
  assert.equal(validResult.normalizedMessage.length, CHAT_MAX_MESSAGE_LENGTH);
  assert.equal(validResult.error, null);
});

test("invariant 2: thread_id always exists, recovers from corrupted storage, and stays in payloads", () => {
  let idCounter = 0;
  const deterministicIdFn = () => `thread-test-${++idCounter}`;

  const corruptedStorage = createMemoryStorage({
    [CHAT_STORAGE_KEY]: "{invalid-json",
  });
  const recoveredState = loadChatState({
    storage: corruptedStorage,
    generateIdFn: deterministicIdFn,
  });
  assert.equal(recoveredState.threadId, "thread-test-1");
  assert.deepEqual(recoveredState.messages, []);

  const missingThreadStorage = createMemoryStorage({
    [CHAT_STORAGE_KEY]: JSON.stringify({
      threadId: "   ",
      messages: [
        { id: "m1", role: CHAT_ROLE_USER, content: "Привіт", createdAt: "2026-11-26T10:00:00Z" },
        { id: "m2", role: "system", content: "invalid role" },
        { id: "m3", role: CHAT_ROLE_ASSISTANT, content: "   " },
      ],
    }),
  });
  const sanitizedState = loadChatState({
    storage: missingThreadStorage,
    generateIdFn: deterministicIdFn,
  });
  assert.equal(sanitizedState.threadId, "thread-test-2");
  assert.equal(sanitizedState.messages.length, 1);
  assert.equal(sanitizedState.messages[0].content, "Привіт");

  const payload = buildChatRequestPayload({
    threadId: recoveredState.threadId,
    messageText: "  Які у мене витрати?  ",
  });
  assert.deepEqual(payload, {
    thread_id: "thread-test-1",
    message: "Які у мене витрати?",
  });

  assert.throws(
    () => buildChatRequestPayload({ threadId: "   ", messageText: "Тест" }),
    /thread_id/,
  );

  const resetState = resetChatThread({
    storage: missingThreadStorage,
    generateIdFn: deterministicIdFn,
  });
  assert.equal(resetState.threadId, "thread-test-3");
  assert.deepEqual(resetState.messages, []);
});

test("invariant 3: sending state blocks concurrent message submission", () => {
  const blocked = validateChatDraft({
    draftText: "Скільки я витратив на каву?",
    isSending: true,
  });
  assert.equal(blocked.isValid, false);
  assert.equal(blocked.normalizedMessage, "");
  assert.ok(blocked.error);
});

test("invariant 4: history stays consistent across persistence and rejects empty backend replies", () => {
  let seq = 0;
  const createId = () => `id-${++seq}`;
  const storage = createMemoryStorage();

  const initialState = { threadId: "thread-abc", messages: [] };
  const afterUser = appendChatMessage({
    state: initialState,
    role: CHAT_ROLE_USER,
    content: "  Підкажи по бюджету ",
    createIdFn: createId,
    nowIsoFn: () => "2026-11-26T12:00:00.000Z",
  });
  const afterAssistant = appendChatMessage({
    state: afterUser,
    role: CHAT_ROLE_ASSISTANT,
    content: "У листопаді найбільше витрат у категорії кава.",
    nextThreadId: "thread-abc",
    createIdFn: createId,
    nowIsoFn: () => "2026-11-26T12:00:02.000Z",
  });

  assert.equal(afterAssistant.threadId, "thread-abc");
  assert.equal(afterAssistant.messages.length, 2);
  assert.equal(afterAssistant.messages[0].role, CHAT_ROLE_USER);
  assert.equal(afterAssistant.messages[0].content, "Підкажи по бюджету");
  assert.equal(afterAssistant.messages[1].role, CHAT_ROLE_ASSISTANT);

  const isSaved = saveChatState({ storage, state: afterAssistant });
  assert.equal(isSaved, true);

  const reloaded = loadChatState({ storage, generateIdFn: createId });
  assert.equal(reloaded.threadId, "thread-abc");
  assert.equal(reloaded.messages.length, 2);
  assert.equal(reloaded.messages[1].content, "У листопаді найбільше витрат у категорії кава.");

  assert.equal(extractChatReplyText({ reply: "  Відповідь Gemini  " }), "Відповідь Gemini");
  assert.throws(() => extractChatReplyText(null));
  assert.throws(() => extractChatReplyText({ reply: "   " }));

  const detailError = extractChatErrorMessage({
    status: 503,
    responseBody: { detail: "Gemini тимчасово недоступний" },
  });
  assert.equal(detailError, "Gemini тимчасово недоступний");

  const notFoundError = extractChatErrorMessage({
    status: 404,
    responseBody: null,
  });
  assert.equal(notFoundError, CHAT_ENDPOINT_UNAVAILABLE_ERROR);
});

test("generateThreadId uses provided uuid function when available and falls back safely", () => {
  assert.equal(
    generateThreadId({ randomUuidFn: () => "550e8400-e29b-41d4-a716-446655440000" }),
    "550e8400-e29b-41d4-a716-446655440000",
  );
  const nativeId = generateThreadId();
  assert.ok(typeof nativeId === "string" && nativeId.trim().length > 0);
  const fallbackId = generateThreadId({ randomUuidFn: () => "   " });
  assert.ok(fallbackId.startsWith("thread-"));
  assert.ok(fallbackId.length > "thread-".length);
});

test("storage recovery handles unavailable, empty, primitive, non-array messages, and QuotaExceeded", () => {
  let seq = 0;
  const createId = () => `id-${++seq}`;

  const noStorageState = loadChatState({
    storage: {},
    generateIdFn: createId,
  });
  assert.equal(noStorageState.threadId, "id-1");
  assert.deepEqual(noStorageState.messages, []);

  const emptyStorageState = loadChatState({
    storage: createMemoryStorage(),
    generateIdFn: createId,
  });
  assert.equal(emptyStorageState.threadId, "id-2");
  assert.deepEqual(emptyStorageState.messages, []);

  const primitiveJsonState = loadChatState({
    storage: createMemoryStorage({ [CHAT_STORAGE_KEY]: "null" }),
    generateIdFn: createId,
  });
  assert.equal(primitiveJsonState.threadId, "id-3");
  assert.deepEqual(primitiveJsonState.messages, []);

  const malformedMessagesState = loadChatState({
    storage: createMemoryStorage({
      [CHAT_STORAGE_KEY]: JSON.stringify({
        threadId: "t-ok",
        messages: "not-an-array",
      }),
    }),
    generateIdFn: createId,
  });
  assert.equal(malformedMessagesState.threadId, "t-ok");
  assert.deepEqual(malformedMessagesState.messages, []);

  const mixedEntriesState = loadChatState({
    storage: createMemoryStorage({
      [CHAT_STORAGE_KEY]: JSON.stringify({
        threadId: "t-mix",
        messages: [
          null,
          "bad-entry",
          { role: CHAT_ROLE_USER, content: "Без id і дати" },
        ],
      }),
    }),
    generateIdFn: createId,
  });
  assert.equal(mixedEntriesState.messages.length, 1);
  assert.ok(mixedEntriesState.messages[0].id.length > 0);
  assert.ok(mixedEntriesState.messages[0].createdAt.length > 0);

  assert.equal(
    saveChatState({ storage: {}, state: noStorageState }),
    false,
  );
  assert.equal(
    saveChatState({
      storage: {
        setItem() {
          throw new Error("QuotaExceededError");
        },
      },
      state: noStorageState,
    }),
    false,
  );
});

test("defensive validation rejects invalid payloads, roles, empty messages, and maps HTTP fallback errors", () => {
  assert.throws(
    () => buildChatRequestPayload({ threadId: "t-1", messageText: "   " }),
    /Введіть повідомлення/,
  );
  assert.throws(
    () => appendChatMessage({ state: { threadId: "t-1", messages: [] }, role: "admin", content: "hi" }),
    /Непідтримувана роль/,
  );
  assert.throws(
    () => appendChatMessage({ state: { threadId: "t-1", messages: [] }, role: CHAT_ROLE_USER, content: "  " }),
    /порожнім/,
  );

  const generatedFromMissingState = appendChatMessage({
    state: null,
    role: CHAT_ROLE_USER,
    content: "Тест без попереднього стану",
  });
  assert.ok(generatedFromMissingState.threadId.length > 0);
  assert.equal(generatedFromMissingState.messages.length, 1);

  assert.equal(extractChatReplyText({ answer: "Відповідь через answer" }), "Відповідь через answer");
  assert.equal(extractChatReplyText({ message: "Відповідь через message" }), "Відповідь через message");

  const serverError = extractChatErrorMessage({ status: 500, responseBody: null });
  assert.ok(serverError.includes("HTTP 500"));

  const networkError = extractChatErrorMessage({ status: 0, responseBody: null });
  assert.equal(networkError, DEFAULT_CHAT_ERROR);
});
