export const CHAT_STORAGE_KEY = "finance_ai_chat_state";
export const CHAT_ROLE_USER = "user";
export const CHAT_ROLE_ASSISTANT = "assistant";
export const CHAT_ALLOWED_ROLES = new Set([
  CHAT_ROLE_USER,
  CHAT_ROLE_ASSISTANT,
]);
export const CHAT_MAX_MESSAGE_LENGTH = 2000;  // Keep single user prompt bounded before backend limits exist.
export const CHAT_THREAD_PREFIX = "thread-";
export const HTTP_STATUS_UNAUTHORIZED = 401;
export const HTTP_STATUS_NOT_FOUND = 404;
export const EMPTY_MESSAGE_ERROR = "Введіть повідомлення перед відправкою.";
export const SENDING_IN_PROGRESS_ERROR =
  "Зачекайте на відповідь ШІ перед наступним повідомленням.";
export const INVALID_CHAT_RESPONSE_ERROR =
  "Сервер повернув некоректну або порожню відповідь чату.";
export const DEFAULT_CHAT_ERROR =
  "Не вдалося отримати відповідь від AI-асистента. Спробуйте ще раз.";
export const CHAT_ENDPOINT_UNAVAILABLE_ERROR =
  "Ендпоінт AI-чату ще не налаштований на сервері (HTTP 404).";

/**
 * Generate a unique thread identifier on the frontend for preserving Gemini chat context.
 */
export function generateThreadId({ randomUuidFn = null } = {}) {
  const uuidGenerator =
    typeof randomUuidFn === "function"
      ? randomUuidFn
      : typeof globalThis.crypto?.randomUUID === "function"
        ? () => globalThis.crypto.randomUUID()
        : null;

  if (uuidGenerator) {
    const generatedUuid = String(uuidGenerator() || "").trim();
    if (generatedUuid) {
      return generatedUuid;
    }
  }

  const timestampPart = Date.now().toString(36);
  const randomPart = Math.random().toString(36).slice(2, 10);
  return `${CHAT_THREAD_PREFIX}${timestampPart}-${randomPart}`;
}

/**
 * Validate raw stored message records and discard malformed entries safely.
 */
function normalizeStoredMessages(rawMessages) {
  if (!Array.isArray(rawMessages)) {
    return [];
  }

  const normalizedMessages = [];
  for (const rawMessage of rawMessages) {
    if (!rawMessage || typeof rawMessage !== "object") {
      continue;
    }
    const hasValidRole = CHAT_ALLOWED_ROLES.has(rawMessage.role);
    const hasValidContent =
      typeof rawMessage.content === "string" &&
      rawMessage.content.trim().length > 0;
    if (!hasValidRole || !hasValidContent) {
      continue;
    }

    const messageId =
      typeof rawMessage.id === "string" && rawMessage.id.trim().length > 0
        ? rawMessage.id.trim()
        : generateThreadId();
    const createdAt =
      typeof rawMessage.createdAt === "string" &&
      rawMessage.createdAt.trim().length > 0
        ? rawMessage.createdAt
        : new Date().toISOString();

    normalizedMessages.push({
      id: messageId,
      role: rawMessage.role,
      content: rawMessage.content,
      createdAt,
    });
  }

  return normalizedMessages;
}

/**
 * Load persisted chat thread_id and message history, always guaranteeing a valid thread_id.
 */
export function loadChatState({
  storage = null,
  storageKey = CHAT_STORAGE_KEY,
  generateIdFn = generateThreadId,
} = {}) {
  const targetStorage = storage ?? globalThis.localStorage ?? null;
  if (!targetStorage || typeof targetStorage.getItem !== "function") {
    return {
      threadId: generateIdFn(),
      messages: [],
    };
  }

  try {
    const rawState = targetStorage.getItem(storageKey);
    if (!rawState) {
      return {
        threadId: generateIdFn(),
        messages: [],
      };
    }

    const parsedState = JSON.parse(rawState);
    if (!parsedState || typeof parsedState !== "object") {
      return {
        threadId: generateIdFn(),
        messages: [],
      };
    }

    const rawThreadId =
      typeof parsedState.threadId === "string" ? parsedState.threadId.trim() : "";
    const threadId = rawThreadId || generateIdFn();
    const messages = normalizeStoredMessages(parsedState.messages);

    return {
      threadId,
      messages,
    };
  } catch {
    return {
      threadId: generateIdFn(),
      messages: [],
    };
  }
}

/**
 * Persist chat thread_id and message history in storage without breaking UI on storage errors.
 */
export function saveChatState({
  storage = null,
  storageKey = CHAT_STORAGE_KEY,
  state,
} = {}) {
  const targetStorage = storage ?? globalThis.localStorage ?? null;
  if (!targetStorage || typeof targetStorage.setItem !== "function" || !state) {
    return false;
  }

  try {
    const serialized = JSON.stringify({
      threadId: state.threadId,
      messages: state.messages,
    });
    targetStorage.setItem(storageKey, serialized);
    return true;
  } catch {
    return false;
  }
}

/**
 * Validate user draft and enforce loading-lock, non-empty, and length invariants.
 */
export function validateChatDraft({
  draftText,
  isSending,
  maxLength = CHAT_MAX_MESSAGE_LENGTH,
} = {}) {
  if (isSending) {
    return {
      isValid: false,
      normalizedMessage: "",
      error: SENDING_IN_PROGRESS_ERROR,
    };
  }

  const normalizedMessage =
    typeof draftText === "string" ? draftText.trim() : "";
  if (!normalizedMessage) {
    return {
      isValid: false,
      normalizedMessage: "",
      error: EMPTY_MESSAGE_ERROR,
    };
  }

  if (normalizedMessage.length > maxLength) {
    return {
      isValid: false,
      normalizedMessage: "",
      error: `Повідомлення занадто довге (максимум ${maxLength} символів).`,
    };
  }

  return {
    isValid: true,
    normalizedMessage,
    error: null,
  };
}

/**
 * Build request JSON body expected by the future Gemini chat backend endpoint.
 */
export function buildChatRequestPayload({
  threadId,
  messageText,
} = {}) {
  const normalizedThreadId =
    typeof threadId === "string" ? threadId.trim() : "";
  const normalizedMessage =
    typeof messageText === "string" ? messageText.trim() : "";

  if (!normalizedThreadId) {
    throw new Error("Відсутній thread_id для відправки повідомлення.");
  }
  if (!normalizedMessage) {
    throw new Error(EMPTY_MESSAGE_ERROR);
  }

  return {
    thread_id: normalizedThreadId,
    message: normalizedMessage,
  };
}

/**
 * Append a message immutably while preserving chronological order and a valid thread_id.
 */
export function appendChatMessage({
  state,
  role,
  content,
  nextThreadId = "",
  createIdFn = generateThreadId,
  nowIsoFn = () => new Date().toISOString(),
} = {}) {
  if (!CHAT_ALLOWED_ROLES.has(role)) {
    throw new Error(`Непідтримувана роль повідомлення: ${role}`);
  }
  const normalizedContent = typeof content === "string" ? content.trim() : "";
  if (!normalizedContent) {
    throw new Error("Повідомлення не може бути порожнім.");
  }

  const resolvedThreadId =
    (typeof nextThreadId === "string" && nextThreadId.trim()) ||
    (typeof state?.threadId === "string" && state.threadId.trim()) ||
    createIdFn();

  const existingMessages = Array.isArray(state?.messages) ? state.messages : [];
  const nextMessage = {
    id: createIdFn(),
    role,
    content: normalizedContent,
    createdAt: nowIsoFn(),
  };

  return {
    threadId: resolvedThreadId,
    messages: [...existingMessages, nextMessage],
  };
}

/**
 * Extract and validate non-empty assistant reply text from backend response JSON.
 */
export function extractChatReplyText(responseBody) {
  if (!responseBody || typeof responseBody !== "object") {
    throw new Error(INVALID_CHAT_RESPONSE_ERROR);
  }

  const candidateFields = [
    responseBody.reply,
    responseBody.answer,
    responseBody.message,
  ];
  for (const candidate of candidateFields) {
    if (typeof candidate === "string" && candidate.trim().length > 0) {
      return candidate.trim();
    }
  }

  throw new Error(INVALID_CHAT_RESPONSE_ERROR);
}

/**
 * Convert HTTP error response status/body into a clear Ukrainian message for the user.
 */
export function extractChatErrorMessage({
  status,
  responseBody = null,
} = {}) {
  if (
    responseBody &&
    typeof responseBody === "object" &&
    typeof responseBody.detail === "string" &&
    responseBody.detail.trim().length > 0
  ) {
    return responseBody.detail.trim();
  }

  if (status === HTTP_STATUS_NOT_FOUND) {
    return CHAT_ENDPOINT_UNAVAILABLE_ERROR;
  }

  if (typeof status === "number" && status > 0) {
    return `Помилка чату (HTTP ${status}). Спробуйте ще раз.`;
  }

  return DEFAULT_CHAT_ERROR;
}

/**
 * Clear chat history and generate a fresh thread_id for starting a new conversation.
 */
export function resetChatThread({
  storage = null,
  storageKey = CHAT_STORAGE_KEY,
  generateIdFn = generateThreadId,
} = {}) {
  const nextState = {
    threadId: generateIdFn(),
    messages: [],
  };
  saveChatState({
    storage,
    storageKey,
    state: nextState,
  });
  return nextState;
}
