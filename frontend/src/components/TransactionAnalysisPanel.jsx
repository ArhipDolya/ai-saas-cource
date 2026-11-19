import { useState } from "react";

export default function TransactionAnalysisPanel({
  analyzeEndpoint,
  authToken,
  onUnauthorized,
}) {
  const [analysis, setAnalysis] = useState(null);
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [analysisError, setAnalysisError] = useState(null);

  async function handleAnalyze() {
    setAnalysisError(null);
    setIsAnalyzing(true);

    try {
      const response = await fetch(analyzeEndpoint, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${authToken}`,
        },
      });

      if (response.status === 401) {
        if (onUnauthorized) {
          onUnauthorized();
        }
        return;
      }

      const responseBody = await response.json().catch(() => null);
      if (!response.ok) {
        const errorDetail =
          responseBody && responseBody.detail
            ? responseBody.detail
            : `Помилка аналізу (HTTP ${response.status})`;
        throw new Error(errorDetail);
      }

      if (!responseBody || typeof responseBody !== "object") {
        throw new Error("Сервер повернув некоректну відповідь аналізу.");
      }

      setAnalysis({
        summary: responseBody.summary || "",
        topExpenseCategories: Array.isArray(responseBody.top_expense_categories)
          ? responseBody.top_expense_categories
          : [],
        risks: Array.isArray(responseBody.risks) ? responseBody.risks : [],
        advice: Array.isArray(responseBody.advice) ? responseBody.advice : [],
      });
    } catch (error) {
      const message =
        error instanceof Error
          ? error.message
          : "Не вдалося виконати аналіз операцій. Спробуйте пізніше.";
      setAnalysisError(message);
    } finally {
      setIsAnalyzing(false);
    }
  }

  return (
    <section
      id="ai-analysis-section"
      aria-label="Аналіз операцій"
      className="admin-panel"
    >
      <div className="panel-header ai-panel-header">
        <div>
          <h2>Фінансовий AI-аналіз</h2>
          <p className="panel-subtitle">
            Отримайте автоматичний висновок, ключові категорії витрат, ризики та
            персональні поради за вашими операціями
          </p>
        </div>
        <button
          type="button"
          className="analyze-button"
          disabled={isAnalyzing}
          onClick={handleAnalyze}
        >
          {isAnalyzing && (
            <span className="button-spinner" aria-hidden="true" />
          )}
          <span>
            {isAnalyzing ? "Аналізуємо операції..." : "Проаналізувати операції"}
          </span>
        </button>
      </div>

      {analysisError && (
        <div className="form-feedback form-error ai-error-banner" role="alert">
          <strong>Не вдалося виконати аналіз</strong>
          <p className="form-error-summary">{analysisError}</p>
        </div>
      )}

      {analysis && (
        <div className="ai-cards-grid">
          <article className="ai-card ai-summary-card">
            <h3 className="ai-card-title">Загальний висновок</h3>
            <p className="ai-card-text">
              {analysis.summary || "Висновок відсутній."}
            </p>
          </article>

          <article className="ai-card ai-categories-card">
            <h3 className="ai-card-title">Топ категорій витрат</h3>
            {analysis.topExpenseCategories.length === 0 ? (
              <p className="ai-card-empty">
                Категорії витрат поки не зафіксовані.
              </p>
            ) : (
              <ul className="ai-tags-list">
                {analysis.topExpenseCategories.map((category, index) => (
                  <li key={`${category}-${index}`} className="ai-category-tag">
                    {category}
                  </li>
                ))}
              </ul>
            )}
          </article>

          <article className="ai-card ai-risks-card">
            <h3 className="ai-card-title">Ризики</h3>
            {analysis.risks.length === 0 ? (
              <p className="ai-card-empty">Суттєвих ризиків не виявлено.</p>
            ) : (
              <ul className="ai-bullet-list">
                {analysis.risks.map((risk, index) => (
                  <li key={`risk-${index}`}>{risk}</li>
                ))}
              </ul>
            )}
          </article>

          <article className="ai-card ai-advice-card">
            <h3 className="ai-card-title">Поради</h3>
            {analysis.advice.length === 0 ? (
              <p className="ai-card-empty">Поради поки відсутні.</p>
            ) : (
              <ol className="ai-numbered-list">
                {analysis.advice.map((tip, index) => (
                  <li key={`advice-${index}`}>{tip}</li>
                ))}
              </ol>
            )}
          </article>
        </div>
      )}
    </section>
  );
}
