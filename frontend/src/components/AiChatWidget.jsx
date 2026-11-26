import { useEffect, useRef, useState } from "react";
import {
  CHAT_MAX_MESSAGE_LENGTH,
  CHAT_ROLE_ASSISTANT,
  CHAT_ROLE_USER,
  HTTP_STATUS_UNAUTHORIZED,
  appendChatMessage,
  buildChatRequestPayload,
  extractChatErrorMessage,
  extractChatReplyText,
  loadChatState,
  resetChatThread,
  saveChatState,
  validateChatDraft,
} from "../utils/aiChatState.js";

function formatMessageTime(isoTimestamp) {
  if (!isoTimestamp) {
    return "";
  }
  const parsedDate = new Date(isoTimestamp);
  if (Number.isNaN(parsedDate.getTime())) {
    return "";
  }
  return parsedDate.toLocaleTimeString("uk-UA", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatShortThreadId(threadId) {
  if (!threadId || threadId.length <= 14) {
    return threadId;
  }
  return `${threadId.slice(0, 8)}...${threadId.slice(-4)}`;
}

export default function AiChatWidget({
  chatEndpoint,
  authToken,
  onUnauthorized,
}) {
  const [chatState, setChatState] = useState(() => loadChatState());
  const [isOpen, setIsOpen] = useState(true);
  const [draftText, setDraftText] = useState("");
  const [isSending, setIsSending] = useState(false);
  const [chatError, setChatError] = useState(null);
  const [lastFailedMessage, setLastFailedMessage] = useState(null);

  const isSendingRef = useRef(false);
  const isMountedRef = useRef(true);
  const messagesEndRef = useRef(null);
  const inputRef = useRef(null);
  const abortControllerRef = useRef(null);

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
    };
  }, []);

  useEffect(() => {
    saveChatState({ state: chatState });
  }, [chatState]);

  useEffect(() => {
    if (isOpen && messagesEndRef.current) {
      messagesEndRef.current.scrollIntoView({ behavior: "smooth" });
    }
  }, [chatState.messages, isSending, isOpen]);

  async function sendChatRequest({
    messageText,
    shouldAppendUserMessage,
  }) {
    const validation = validateChatDraft({
      draftText: messageText,
      isSending: isSendingRef.current || isSending,
    });
    if (!validation.isValid) {
      setChatError(validation.error);
      return;
    }

    const normalizedMessage = validation.normalizedMessage;
    const activeThreadId = chatState.threadId;

    isSendingRef.current = true;
    setIsSending(true);
    setChatError(null);
    setLastFailedMessage(null);

    if (shouldAppendUserMessage) {
      setDraftText("");
      setChatState((prevState) =>
        appendChatMessage({
          state: prevState,
          role: CHAT_ROLE_USER,
          content: normalizedMessage,
        }),
      );
    }

    const controller = new AbortController();
    abortControllerRef.current = controller;

    try {
      const requestPayload = buildChatRequestPayload({
        threadId: activeThreadId,
        messageText: normalizedMessage,
      });

      const response = await fetch(chatEndpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${authToken}`,
        },
        body: JSON.stringify(requestPayload),
        signal: controller.signal,
      });

      if (response.status === HTTP_STATUS_UNAUTHORIZED) {
        if (onUnauthorized) {
          onUnauthorized();
        }
        return;
      }

      const responseBody = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(
          extractChatErrorMessage({
            status: response.status,
            responseBody,
          }),
        );
      }

      const replyText = extractChatReplyText(responseBody);
      const nextThreadId =
        typeof responseBody?.thread_id === "string"
          ? responseBody.thread_id
          : "";

      if (isMountedRef.current) {
        setChatState((prevState) =>
          appendChatMessage({
            state: prevState,
            role: CHAT_ROLE_ASSISTANT,
            content: replyText,
            nextThreadId,
          }),
        );
      }
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        return;
      }
      if (isMountedRef.current) {
        const message =
          error instanceof Error && error.message
            ? error.message
            : extractChatErrorMessage({});
        setChatError(message);
        setLastFailedMessage(normalizedMessage);
      }
    } finally {
      abortControllerRef.current = null;
      isSendingRef.current = false;
      if (isMountedRef.current) {
        setIsSending(false);
        setTimeout(() => {
          inputRef.current?.focus();
        }, 0);
      }
    }
  }

  function handleFormSubmit(event) {
    event.preventDefault();
    void sendChatRequest({
      messageText: draftText,
      shouldAppendUserMessage: true,
    });
  }

  function handleTextareaKeyDown(event) {
    if (event.nativeEvent?.isComposing) {
      return;
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (!isSending && draftText.trim()) {
        void sendChatRequest({
          messageText: draftText,
          shouldAppendUserMessage: true,
        });
      }
    }
  }

  function handleRetryFailedMessage() {
    if (!lastFailedMessage || isSending) {
      return;
    }
    void sendChatRequest({
      messageText: lastFailedMessage,
      shouldAppendUserMessage: false,
    });
  }

  function handleStartNewChat() {
    if (isSending) {
      return;
    }
    const nextState = resetChatThread();
    setChatState(nextState);
    setChatError(null);
    setLastFailedMessage(null);
    setDraftText("");
    inputRef.current?.focus();
  }

  const trimmedLength = draftText.trim().length;
  const canSubmit = !isSending && trimmedLength > 0;

  if (!isOpen) {
    return (
      <div className="ai-chat-dock" aria-live="polite">
        <button
          type="button"
          className="ai-chat-launcher"
          onClick={() => setIsOpen(true)}
          aria-expanded="false"
          aria-controls="ai-chat-widget"
        >
          <span className="ai-chat-launcher-dot" aria-hidden="true" />
          <span>AI-чат · Gemini</span>
          {chatState.messages.length > 0 && (
            <span className="ai-chat-launcher-count">
              {chatState.messages.length}
            </span>
          )}
        </button>
      </div>
    );
  }

  return (
    <aside
      id="ai-chat-widget"
      className="ai-chat-dock ai-chat-panel"
      aria-label="AI-чат асистент Gemini"
    >
      <header className="ai-chat-header">
        <div className="ai-chat-title-group">
          <div className="ai-chat-title-row">
            <span className="ai-chat-status-badge">Gemini</span>
            <h2 className="ai-chat-title">AI-асистент проєкту</h2>
          </div>
          <span
            className="ai-chat-thread-id"
            title={`thread_id: ${chatState.threadId}`}
          >
            thread_id: <code>{formatShortThreadId(chatState.threadId)}</code>
          </span>
        </div>

        <div className="ai-chat-header-actions">
          <button
            type="button"
            className="ai-chat-icon-button"
            onClick={handleStartNewChat}
            disabled={isSending}
            title="Почати новий чат з новим thread_id"
          >
            Новий чат
          </button>
          <button
            type="button"
            className="ai-chat-icon-button"
            onClick={() => setIsOpen(false)}
            aria-expanded="true"
            aria-label="Згорнути AI-чат"
            title="Згорнути"
          >
            Згорнути
          </button>
        </div>
      </header>

      <div
        className="ai-chat-messages"
        role="log"
        aria-live="polite"
        aria-label="Історія повідомлень чату"
      >
        {chatState.messages.length === 0 && !isSending && (
          <div className="ai-chat-empty-state">
            <p className="ai-chat-empty-title">
              Запитайте Gemini про Finance SaaS
            </p>
            <p className="ai-chat-empty-subtitle">
              Можна уточнити структуру операцій, фінансову аналітику або
              можливості проєкту. Контекст діалогу прив&apos;язаний до поточного{" "}
              <code>thread_id</code>.
            </p>
          </div>
        )}

        {chatState.messages.map((message) => {
          const isUserMessage = message.role === CHAT_ROLE_USER;
          return (
            <div
              key={message.id}
              className={`ai-chat-message ${
                isUserMessage ? "ai-chat-message-user" : "ai-chat-message-assistant"
              }`}
            >
              <div className="ai-chat-message-meta">
                <span>{isUserMessage ? "Ви" : "Gemini"}</span>
                <span>{formatMessageTime(message.createdAt)}</span>
              </div>
              <div className="ai-chat-bubble">{message.content}</div>
            </div>
          );
        })}

        {isSending && (
          <div
            className="ai-chat-message ai-chat-message-assistant"
            role="status"
          >
            <div className="ai-chat-message-meta">
              <span>Gemini</span>
              <span>обробка...</span>
            </div>
            <div className="ai-chat-bubble ai-chat-loading-bubble">
              <span className="ai-chat-spinner" aria-hidden="true" />
              <span>ШІ готує відповідь...</span>
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {chatError && (
        <div className="ai-chat-error" role="alert">
          <div className="ai-chat-error-text">
            <strong>Помилка чату</strong>
            <p>{chatError}</p>
          </div>
          <div className="ai-chat-error-actions">
            {lastFailedMessage && (
              <button
                type="button"
                className="ai-chat-retry-button"
                onClick={handleRetryFailedMessage}
                disabled={isSending}
              >
                Повторити
              </button>
            )}
            <button
              type="button"
              className="ai-chat-dismiss-button"
              onClick={() => {
                setChatError(null);
                setLastFailedMessage(null);
              }}
              aria-label="Закрити повідомлення про помилку"
            >
              Закрити
            </button>
          </div>
        </div>
      )}

      <form className="ai-chat-form" onSubmit={handleFormSubmit} noValidate>
        <div className="ai-chat-input-wrapper">
          <textarea
            ref={inputRef}
            className="ai-chat-input"
            rows={2}
            maxLength={CHAT_MAX_MESSAGE_LENGTH}
            placeholder={
              isSending
                ? "Очікуємо відповідь від Gemini..."
                : "Напишіть повідомлення про проєкт..."
            }
            aria-label="Повідомлення для AI-асистента"
            value={draftText}
            disabled={isSending}
            onKeyDown={handleTextareaKeyDown}
            onChange={(event) => {
              if (chatError) {
                setChatError(null);
              }
              setDraftText(event.target.value);
            }}
          />
          <div className="ai-chat-form-footer">
            <span className="ai-chat-hint">
              {isSending
                ? "Ввід заблоковано до отримання відповіді"
                : `Enter - надіслати (${trimmedLength}/${CHAT_MAX_MESSAGE_LENGTH})`}
            </span>
            <button
              type="submit"
              className="ai-chat-send-button"
              disabled={!canSubmit}
            >
              {isSending && (
                <span className="button-spinner" aria-hidden="true" />
              )}
              <span>{isSending ? "Надсилаємо..." : "Надіслати"}</span>
            </button>
          </div>
        </div>
      </form>
    </aside>
  );
}
