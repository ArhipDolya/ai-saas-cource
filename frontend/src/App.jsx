import { useCallback, useEffect, useState } from "react";

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");
const SUMMARY_ENDPOINT = `${API_BASE_URL}/api/summary`;
const TRANSACTIONS_ENDPOINT = `${API_BASE_URL}/api/transactions`;

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

async function fetchJson(url) {
  const response = await fetch(url);
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
  const [summary, setSummary] = useState(null);
  const [transactions, setTransactions] = useState([]);
  const [isLoading, setIsLoading] = useState(true);
  const [errorMessage, setErrorMessage] = useState(null);

  const loadDashboardData = useCallback(async () => {
    setIsLoading(true);
    setErrorMessage(null);

    try {
      const [summaryData, transactionsData] = await Promise.all([
        fetchJson(SUMMARY_ENDPOINT),
        fetchJson(TRANSACTIONS_ENDPOINT),
      ]);
      setSummary(summaryData);
      setTransactions(Array.isArray(transactionsData) ? transactionsData : []);
    } catch (error) {
      const message =
        error instanceof Error
          ? error.message
          : "Не вдалося завантажити дані з сервера.";
      setErrorMessage(message);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    loadDashboardData();
  }, [loadDashboardData]);

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

                {transactions.length === 0 ? (
                  <p className="empty-state">Операцій поки немає.</p>
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
                        </tr>
                      </thead>
                      <tbody>
                        {transactions.map((operation) => {
                          const isIncome =
                            operation.transaction_type === "income";
                          return (
                            <tr
                              key={`${operation.transaction_type || "expense"}-${operation.id}`}
                            >
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
