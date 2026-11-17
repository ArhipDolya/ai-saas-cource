import { useCallback, useEffect, useRef, useState } from "react";
import AdminLoginScreen from "./components/AdminLoginScreen.jsx";
import NewOperationForm from "./components/NewOperationForm.jsx";

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");
const AUTH_LOGIN_ENDPOINT = `${API_BASE_URL}/api/auth/login`;
const SUMMARY_ENDPOINT = `${API_BASE_URL}/api/summary`;
const TRANSACTIONS_ENDPOINT = `${API_BASE_URL}/api/transactions`;
const AUTH_TOKEN_STORAGE_KEY = "finance_admin_token";
const FILTER_ALL = "all";
const FILTER_INCOME = "income";
const FILTER_EXPENSE = "expense";
const TRANSACTION_FILTERS = [
  { value: FILTER_ALL, label: "Всі" },
  { value: FILTER_INCOME, label: "Доходи" },
  { value: FILTER_EXPENSE, label: "Витрати" },
];

function buildTransactionsEndpoint(filterValue) {
  if (filterValue === FILTER_INCOME || filterValue === FILTER_EXPENSE) {
    return `${TRANSACTIONS_ENDPOINT}?type=${encodeURIComponent(filterValue)}`;
  }
  return TRANSACTIONS_ENDPOINT;
}

function formatAmount(amount) {
  return `${Number(amount).toFixed(2)} грн`;
}

function formatDateTime(isoString) {
  if (!isoString) {
    return "-";
  }
  const parsedDate = new Date(isoString);
  const isInvalidDate = Number.isNaN(parsedDate.getTime());
  if (isInvalidDate) {
    return isoString;
  }
  return parsedDate.toLocaleString("uk-UA");
}

function formatOperationType(transactionType) {
  if (transactionType === "income") {
    return "Дохід";
  }
  return "Витрата";
}

function getOperationKey(operation) {
  return `${operation.transaction_type || "expense"}-${operation.id}`;
}

class UnauthorizedError extends Error {
  constructor(message) {
    super(message);
    this.name = "UnauthorizedError";
  }
}

async function fetchJson(url, authToken) {
  const response = await fetch(url, {
    cache: "no-store",
    headers: {
      Authorization: `Bearer ${authToken}`,
    },
  });
  if (response.status === 401) {
    throw new UnauthorizedError("Сесія завершилась. Введіть пароль ще раз.");
  }
  if (!response.ok) {
    let errorDetail = `HTTP ${response.status}`;
    try {
      const errorPayload = await response.json();
      if (errorPayload && errorPayload.detail) {
        errorDetail = errorPayload.detail;
      }
    } catch {
      // Keep fallback HTTP status text when response body is not JSON.
    }
    throw new Error(`Помилка запиту ${url}: ${errorDetail}`);
  }
  return response.json();
}

export default function App() {
  const [authToken, setAuthToken] = useState(
    () => sessionStorage.getItem(AUTH_TOKEN_STORAGE_KEY) || "",
  );
  const [authNotice, setAuthNotice] = useState(null);
  const [summary, setSummary] = useState(null);
  const [transactions, setTransactions] = useState([]);
  const [activeFilter, setActiveFilter] = useState(FILTER_ALL);
  const [isLoading, setIsLoading] = useState(Boolean(authToken));
  const [isFilterLoading, setIsFilterLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState(null);
  const [pendingDeleteKey, setPendingDeleteKey] = useState(null);
  const [deletingKey, setDeletingKey] = useState(null);
  const [tableError, setTableError] = useState(null);
  const activeFilterRef = useRef(FILTER_ALL);
  const requestCounterRef = useRef(0);

  const handleUnauthorized = useCallback((noticeMessage) => {
    sessionStorage.removeItem(AUTH_TOKEN_STORAGE_KEY);
    activeFilterRef.current = FILTER_ALL;
    setAuthToken("");
    setSummary(null);
    setTransactions([]);
    setActiveFilter(FILTER_ALL);
    setPendingDeleteKey(null);
    setDeletingKey(null);
    setErrorMessage(null);
    setTableError(null);
    setAuthNotice(
      noticeMessage || "Сесія завершилась. Введіть пароль ще раз.",
    );
  }, []);

  const handleLoginSuccess = useCallback((token) => {
    sessionStorage.setItem(AUTH_TOKEN_STORAGE_KEY, token);
    activeFilterRef.current = FILTER_ALL;
    setAuthNotice(null);
    setErrorMessage(null);
    setTableError(null);
    setActiveFilter(FILTER_ALL);
    setIsLoading(true);
    setAuthToken(token);
  }, []);

  const handleLogout = useCallback(() => {
    sessionStorage.removeItem(AUTH_TOKEN_STORAGE_KEY);
    activeFilterRef.current = FILTER_ALL;
    setAuthToken("");
    setSummary(null);
    setTransactions([]);
    setActiveFilter(FILTER_ALL);
    setPendingDeleteKey(null);
    setDeletingKey(null);
    setErrorMessage(null);
    setTableError(null);
    setAuthNotice(null);
  }, []);

  const fetchDashboardState = useCallback(
    async (filterValue, token) => {
      const requestId = ++requestCounterRef.current;
      const [summaryData, transactionsData] = await Promise.all([
        fetchJson(SUMMARY_ENDPOINT, token),
        fetchJson(buildTransactionsEndpoint(filterValue), token),
      ]);
      setSummary(summaryData);
      if (requestId === requestCounterRef.current) {
        setTransactions(Array.isArray(transactionsData) ? transactionsData : []);
      }
    },
    [],
  );

  const loadDashboardData = useCallback(async () => {
    if (!authToken) {
      setIsLoading(false);
      return;
    }

    setIsLoading(true);
    setErrorMessage(null);
    setTableError(null);

    try {
      await fetchDashboardState(activeFilterRef.current, authToken);
    } catch (error) {
      if (error instanceof UnauthorizedError) {
        handleUnauthorized(error.message);
        return;
      }
      const message =
        error instanceof Error
          ? error.message
          : "Не вдалося завантажити дані з сервера.";
      setErrorMessage(message);
    } finally {
      setIsLoading(false);
    }
  }, [authToken, fetchDashboardState, handleUnauthorized]);

  const refreshDashboardData = useCallback(async () => {
    if (!authToken) {
      return;
    }

    try {
      await fetchDashboardState(activeFilterRef.current, authToken);
      setErrorMessage(null);
      setTableError(null);
    } catch (error) {
      if (error instanceof UnauthorizedError) {
        handleUnauthorized(error.message);
        return;
      }
      const message =
        error instanceof Error
          ? error.message
          : "Не вдалося оновити дані з сервера.";
      setErrorMessage(message);
    }
  }, [authToken, fetchDashboardState, handleUnauthorized]);

  useEffect(() => {
    loadDashboardData();
  }, [loadDashboardData]);

  async function handleSelectFilter(filterValue) {
    activeFilterRef.current = filterValue;
    setActiveFilter(filterValue);
    setPendingDeleteKey(null);
    setTableError(null);
    setIsFilterLoading(true);

    const requestId = ++requestCounterRef.current;
    try {
      const transactionsData = await fetchJson(
        buildTransactionsEndpoint(filterValue),
        authToken,
      );
      if (requestId === requestCounterRef.current) {
        setTransactions(Array.isArray(transactionsData) ? transactionsData : []);
      }
    } catch (error) {
      if (error instanceof UnauthorizedError) {
        handleUnauthorized(error.message);
        return;
      }
      if (requestId === requestCounterRef.current) {
        const message =
          error instanceof Error
            ? error.message
            : "Не вдалося застосувати фільтр транзакцій.";
        setTableError(message);
      }
    } finally {
      if (requestId === requestCounterRef.current) {
        setIsFilterLoading(false);
      }
    }
  }

  function handleRequestDelete(operationKey) {
    setTableError(null);
    setPendingDeleteKey((currentKey) =>
      currentKey === operationKey ? null : operationKey,
    );
  }

  async function handleConfirmDelete(operation) {
    const operationKey = getOperationKey(operation);
    setTableError(null);

    const transactionId = Number(operation.id);
    const isValidId = Number.isInteger(transactionId) && transactionId > 0;
    if (!isValidId) {
      setTableError("Некоректний ID транзакції для видалення.");
      return;
    }

    setDeletingKey(operationKey);
    try {
      const operationType = operation.transaction_type || "expense";
      const deleteUrl = `${TRANSACTIONS_ENDPOINT}/${transactionId}?type=${encodeURIComponent(operationType)}`;
      const response = await fetch(deleteUrl, {
        method: "DELETE",
        headers: {
          Authorization: `Bearer ${authToken}`,
        },
      });
      if (response.status === 401) {
        handleUnauthorized();
        return;
      }
      const responseBody = await response.json().catch(() => null);
      if (!response.ok) {
        const errorDetail =
          responseBody && responseBody.detail
            ? responseBody.detail
            : `Помилка видалення (HTTP ${response.status})`;
        throw new Error(errorDetail);
      }

      setPendingDeleteKey(null);
      setTransactions((prevTransactions) =>
        prevTransactions.filter(
          (item) => getOperationKey(item) !== operationKey,
        ),
      );
      await refreshDashboardData();
    } catch (error) {
      const message =
        error instanceof Error
          ? error.message
          : "Не вдалося видалити транзакцію.";
      setTableError(message);
    } finally {
      setDeletingKey(null);
    }
  }

  if (!authToken) {
    return (
      <AdminLoginScreen
        loginEndpoint={AUTH_LOGIN_ENDPOINT}
        initialNotice={authNotice}
        onLoginSuccess={handleLoginSuccess}
      />
    );
  }

  return (
    <div className="admin-layout">
      <aside className="admin-sidebar">
        <div className="sidebar-brand">
          <span className="brand-badge">ADMIN</span>
          <span className="brand-title">Finance SaaS</span>
        </div>

        <nav className="sidebar-nav" aria-label="Навігація адмін-панелі">
          <a href="#summary-section" className="nav-item active">
            Карточки зведення
          </a>
        </nav>

        <div className="sidebar-footer">
          <div className="status-indicator">
            <span
              className={`status-dot ${
                errorMessage ? "dot-error" : isLoading ? "dot-loading" : "dot-ok"
              }`}
            />
            <span>
              {errorMessage
                ? "Помилка API"
                : isLoading
                  ? "Синхронізація..."
                  : "Neon DB підключено"}
            </span>
          </div>
        </div>
      </aside>

      <div className="admin-workspace">
        <header className="admin-topbar">
          <div>
            <span className="topbar-eyebrow">Admin Dashboard</span>
            <h1>Панель керування фінансами</h1>
          </div>
          <div className="topbar-actions">
            <button
              type="button"
              className="refresh-button"
              onClick={loadDashboardData}
              disabled={isLoading}
            >
              {isLoading ? "Завантаження..." : "Оновити дані"}
            </button>
            <button
              type="button"
              className="logout-button"
              onClick={handleLogout}
            >
              Вийти
            </button>
          </div>
        </header>

        <main className="admin-content">
          {isLoading && (
            <div className="state-banner loading-state" role="status">
              <div className="spinner" aria-hidden="true" />
              <span>Завантаження операцій та зведення з бази Neon...</span>
            </div>
          )}

          {!isLoading && errorMessage && (
            <div className="state-banner error-state" role="alert">
              <div>
                <strong>Помилка завантаження даних</strong>
                <p>{errorMessage}</p>
              </div>
              <button
                type="button"
                className="retry-button"
                onClick={loadDashboardData}
              >
                Спробувати знову
              </button>
            </div>
          )}

          {!isLoading && !errorMessage && (
            <>
              {summary && (
                <section
                  id="summary-section"
                  aria-label="Карточки операцій"
                  className="summary-top-section"
                >
                  <div className="summary-grid">
                    <article className="summary-card income-card">
                      <div className="card-header-row">
                        <span className="summary-label">Карточка доходу</span>
                        <code className="field-tag">total_income</code>
                      </div>
                      <strong className="summary-value">
                        {formatAmount(summary.total_income)}
                      </strong>
                      <span className="card-caption">
                        Загальна сума всіх доходів (GET /api/summary)
                      </span>
                    </article>

                    <article className="summary-card expense-card">
                      <div className="card-header-row">
                        <span className="summary-label">Карточка витрат</span>
                        <code className="field-tag">total_expense</code>
                      </div>
                      <strong className="summary-value">
                        {formatAmount(summary.total_expense)}
                      </strong>
                      <span className="card-caption">
                        Загальна сума всіх витрат (GET /api/summary)
                      </span>
                    </article>

                    <article className="summary-card balance-card">
                      <div className="card-header-row">
                        <span className="summary-label">Карточка балансу</span>
                        <code className="field-tag">balance</code>
                      </div>
                      <strong
                        className={`summary-value ${
                          Number(summary.balance) < 0 ? "negative-balance" : ""
                        }`}
                      >
                        {formatAmount(summary.balance)}
                      </strong>
                      <span className="card-caption">
                        Поточний баланс за доходами і витратами
                      </span>
                    </article>
                  </div>
                </section>
              )}

              <NewOperationForm
                transactionsEndpoint={TRANSACTIONS_ENDPOINT}
                authToken={authToken}
                onUnauthorized={handleUnauthorized}
                onTransactionCreated={refreshDashboardData}
              />

              <section
                id="operations-section"
                aria-label="Таблиця операцій"
                className="admin-panel"
              >
                <div className="panel-header">
                  <div>
                    <h2>Таблиця операцій</h2>
                    <p className="panel-subtitle">
                      Усі фінансові операції користувачів (дані з{" "}
                      <code>GET /api/transactions</code>)
                    </p>
                  </div>
                  <span className="badge">
                    Всього операцій: {transactions.length}
                  </span>
                </div>

                <div
                  className="filter-bar"
                  role="group"
                  aria-label="Фільтр транзакцій за типом"
                >
                  {TRANSACTION_FILTERS.map((filterOption) => {
                    const isActive = activeFilter === filterOption.value;
                    return (
                      <button
                        key={filterOption.value}
                        type="button"
                        aria-pressed={isActive}
                        className={`filter-button ${isActive ? "active" : ""}`}
                        onClick={() => handleSelectFilter(filterOption.value)}
                      >
                        {filterOption.label}
                      </button>
                    );
                  })}
                  {isFilterLoading && (
                    <span className="filter-status">Завантаження...</span>
                  )}
                </div>

                {tableError && (
                  <p
                    className="form-feedback form-error delete-error-banner"
                    role="alert"
                  >
                    {tableError}
                  </p>
                )}

                {transactions.length === 0 ? (
                  <p className="empty-state">
                    {activeFilter === FILTER_ALL
                      ? "Операцій поки немає."
                      : "Операцій за обраним фільтром поки немає."}
                  </p>
                ) : (
                  <div className="table-wrapper">
                    <table className="admin-table operations-table">
                      <thead>
                        <tr>
                          <th>Дата</th>
                          <th>Тип операції</th>
                          <th>Сума</th>
                          <th>Категорія</th>
                          <th>Опис</th>
                          <th>Дії</th>
                        </tr>
                      </thead>
                      <tbody>
                        {transactions.map((operation) => {
                          const isIncome =
                            operation.transaction_type === "income";
                          const operationKey = getOperationKey(operation);
                          const isPendingConfirm =
                            pendingDeleteKey === operationKey;
                          const isDeletingThisRow =
                            deletingKey === operationKey;
                          const isDeletingAny = deletingKey !== null;
                          return (
                            <tr key={operationKey}>
                              <td className="date-cell">
                                {formatDateTime(operation.created_at)}
                              </td>
                              <td>
                                <span
                                  className={`type-badge ${
                                    isIncome ? "type-income" : "type-expense"
                                  }`}
                                >
                                  {formatOperationType(
                                    operation.transaction_type,
                                  )}
                                </span>
                              </td>
                              <td
                                className={`amount-cell ${
                                  isIncome ? "amount-income" : "amount-expense"
                                }`}
                              >
                                {formatAmount(operation.amount)}
                              </td>
                              <td>
                                <span className="category-pill">
                                  {operation.category_name}
                                </span>
                              </td>
                              <td className="description-cell">
                                {operation.description || "Без опису"}
                              </td>
                              <td className="actions-cell">
                                <div className="row-actions">
                                  <button
                                    type="button"
                                    className="delete-button"
                                    disabled={isDeletingAny}
                                    onClick={() =>
                                      handleRequestDelete(operationKey)
                                    }
                                  >
                                    Видалити
                                  </button>
                                  {isPendingConfirm && (
                                    <button
                                      type="button"
                                      className="confirm-delete-button"
                                      disabled={isDeletingAny}
                                      onClick={() =>
                                        handleConfirmDelete(operation)
                                      }
                                    >
                                      {isDeletingThisRow
                                        ? "Видалення..."
                                        : "Підтвердження"}
                                    </button>
                                  )}
                                </div>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            </>
          )}
        </main>
      </div>
    </div>
  );
}
