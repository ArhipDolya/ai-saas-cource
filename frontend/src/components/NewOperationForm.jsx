import { useState } from "react";

const OPERATION_TYPE_INCOME = "income";
const OPERATION_TYPE_EXPENSE = "expense";
const ALLOWED_OPERATION_TYPES = new Set([
  OPERATION_TYPE_INCOME,
  OPERATION_TYPE_EXPENSE,
]);
const AMOUNT_DECIMAL_PATTERN = /^\d+(?:\.\d{1,2})?$/;
const DATE_PATTERN = /^(\d{4})-(\d{2})-(\d{2})$/;
const CATEGORY_MAX_LENGTH = 100;
const MINIMUM_AMOUNT = 0.01;
const MAXIMUM_AMOUNT = 10000000;
const MIN_YEAR = 2000;
const MAX_YEAR = 2100;

function getCurrentLocalDate() {
  const now = new Date();
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function isValidCalendarDate(dateText) {
  const match = DATE_PATTERN.exec(dateText);
  if (!match) {
    return false;
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (year < MIN_YEAR || year > MAX_YEAR) {
    return false;
  }
  const candidate = new Date(year, month - 1, day);
  return (
    candidate.getFullYear() === year &&
    candidate.getMonth() === month - 1 &&
    candidate.getDate() === day
  );
}

function formatTypeLabel(operationType) {
  if (operationType === OPERATION_TYPE_INCOME) {
    return "Дохід";
  }
  return "Витрата";
}

function validateAmountField({ amountText, hasBadNumberInput }) {
  const normalizedAmountText = amountText.trim().replace(",", ".");
  if (hasBadNumberInput || !normalizedAmountText) {
    return {
      error: "Введіть коректну суму від 0.01 до 10000000.00 грн.",
      value: null,
    };
  }

  const parsedAmount = Number(normalizedAmountText);
  if (!Number.isFinite(parsedAmount)) {
    return {
      error: "Сума має бути числовим значенням.",
      value: null,
    };
  }
  if (parsedAmount <= 0) {
    return {
      error: "Сума має бути більшою за 0 (від'ємні значення та нуль заборонені).",
      value: null,
    };
  }
  if (parsedAmount < MINIMUM_AMOUNT) {
    return {
      error: "Мінімальна сума операції: 0.01 грн.",
      value: null,
    };
  }
  if (parsedAmount > MAXIMUM_AMOUNT) {
    return {
      error: "Максимальна дозволена сума операції: 10000000.00 грн.",
      value: null,
    };
  }
  if (!AMOUNT_DECIMAL_PATTERN.test(normalizedAmountText)) {
    return {
      error: "Сума може містити максимум два знаки після коми (наприклад, 120.50).",
      value: null,
    };
  }

  return {
    error: null,
    value: Number(parsedAmount.toFixed(2)),
  };
}

function validateOperationInput({
  operationType,
  amountText,
  hasBadNumberInput,
  category,
  dateText,
}) {
  const fieldErrors = {};

  if (!ALLOWED_OPERATION_TYPES.has(operationType)) {
    fieldErrors.type = "Оберіть допустимий тип: «Дохід» або «Витрата».";
  }

  const amountResult = validateAmountField({
    amountText,
    hasBadNumberInput,
  });
  if (amountResult.error) {
    fieldErrors.amount = amountResult.error;
  }

  const normalizedCategory = category.trim().replace(/\s+/g, " ");
  if (!normalizedCategory) {
    fieldErrors.category =
      "Введіть категорію операції (наприклад: зарплата або сік).";
  } else if (normalizedCategory.length > CATEGORY_MAX_LENGTH) {
    fieldErrors.category = `Назва категорії не може перевищувати ${CATEGORY_MAX_LENGTH} символів.`;
  }

  const effectiveDate = dateText || getCurrentLocalDate();
  if (!isValidCalendarDate(effectiveDate)) {
    fieldErrors.date = `Оберіть коректну дату в межах ${MIN_YEAR}-${MAX_YEAR} років.`;
  }

  const hasErrors = Object.keys(fieldErrors).length > 0;
  if (hasErrors) {
    return {
      fieldErrors,
      data: null,
    };
  }

  return {
    fieldErrors: {},
    data: {
      type: operationType,
      amount: amountResult.value,
      category: normalizedCategory,
      date: effectiveDate,
    },
  };
}

const FIELD_LABELS = {
  type: "Тип",
  amount: "Сума",
  category: "Категорія",
  date: "Дата",
};

export default function NewOperationForm({
  transactionsEndpoint,
  authToken,
  onUnauthorized,
  onTransactionCreated,
}) {
  const [operationType, setOperationType] = useState(OPERATION_TYPE_EXPENSE);
  const [amount, setAmount] = useState("");
  const [category, setCategory] = useState("");
  const [operationDate, setOperationDate] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [fieldErrors, setFieldErrors] = useState({});
  const [submitError, setSubmitError] = useState(null);
  const [savedMessage, setSavedMessage] = useState(null);

  function clearFieldFeedback(fieldName) {
    if (fieldErrors[fieldName]) {
      setFieldErrors((prevErrors) => {
        const nextErrors = { ...prevErrors };
        delete nextErrors[fieldName];
        return nextErrors;
      });
    }
    if (submitError !== null) {
      setSubmitError(null);
    }
    if (savedMessage !== null) {
      setSavedMessage(null);
    }
  }

  async function handleSubmit(event) {
    event.preventDefault();
    setFieldErrors({});
    setSubmitError(null);
    setSavedMessage(null);

    const amountInput = event.currentTarget.elements.namedItem("amount");
    const hasBadNumberInput = Boolean(amountInput?.validity?.badInput);
    const validation = validateOperationInput({
      operationType,
      amountText: amount,
      hasBadNumberInput,
      category,
      dateText: operationDate,
    });

    if (!validation.data) {
      setFieldErrors(validation.fieldErrors);
      setSubmitError(
        "Не вдалося зберегти операцію. Перевірте виділені поля нижче:",
      );
      return;
    }

    const payload = validation.data;
    if (!operationDate) {
      setOperationDate(payload.date);
    }

    setIsSubmitting(true);
    try {
      const response = await fetch(transactionsEndpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${authToken}`,
        },
        body: JSON.stringify(payload),
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
            : `Помилка сервера (HTTP ${response.status})`;
        throw new Error(errorDetail);
      }

      setOperationType(OPERATION_TYPE_EXPENSE);
      setAmount("");
      setCategory("");
      setOperationDate("");
      setFieldErrors({});
      setSavedMessage(
        `Операцію успішно збережено в базу: ${formatTypeLabel(payload.type)} - ${payload.amount.toFixed(2)} грн, категорія «${payload.category}», дата ${payload.date}.`,
      );

      if (onTransactionCreated) {
        await onTransactionCreated();
      }
    } catch (error) {
      const message =
        error instanceof Error
          ? error.message
          : "Не вдалося зберегти операцію. Спробуйте ще раз.";
      setSubmitError(message);
    } finally {
      setIsSubmitting(false);
    }
  }

  const errorEntries = Object.entries(fieldErrors);
  const hasValidationErrors = errorEntries.length > 0;

  return (
    <section
      id="new-operation-section"
      aria-label="Нова операція"
      className="admin-panel"
    >
      <form className="operation-form" onSubmit={handleSubmit} noValidate>
        <h2 className="form-title">Нова операція</h2>

        <div className="form-grid">
          <div className="form-field">
            <label htmlFor="operation-type">Тип</label>
            <select
              id="operation-type"
              name="type"
              value={operationType}
              disabled={isSubmitting}
              aria-invalid={Boolean(fieldErrors.type)}
              className={fieldErrors.type ? "input-error" : ""}
              onChange={(event) => {
                clearFieldFeedback("type");
                setOperationType(event.target.value);
              }}
            >
              <option value={OPERATION_TYPE_INCOME}>Дохід</option>
              <option value={OPERATION_TYPE_EXPENSE}>Витрата</option>
            </select>
            {fieldErrors.type && (
              <span className="field-error-text">{fieldErrors.type}</span>
            )}
          </div>

          <div className="form-field">
            <label htmlFor="operation-amount">Сума</label>
            <input
              id="operation-amount"
              name="amount"
              type="number"
              inputMode="decimal"
              step="0.01"
              min={MINIMUM_AMOUNT}
              max={MAXIMUM_AMOUNT}
              placeholder="1200"
              value={amount}
              disabled={isSubmitting}
              aria-invalid={Boolean(fieldErrors.amount)}
              className={fieldErrors.amount ? "input-error" : ""}
              onChange={(event) => {
                clearFieldFeedback("amount");
                setAmount(event.target.value);
              }}
            />
            {fieldErrors.amount && (
              <span className="field-error-text">{fieldErrors.amount}</span>
            )}
          </div>

          <div className="form-field">
            <label htmlFor="operation-category">Категорія</label>
            <input
              id="operation-category"
              name="category"
              type="text"
              maxLength={CATEGORY_MAX_LENGTH}
              placeholder="Наприклад: зарплата або витрата на сік"
              value={category}
              disabled={isSubmitting}
              aria-invalid={Boolean(fieldErrors.category)}
              className={fieldErrors.category ? "input-error" : ""}
              onChange={(event) => {
                clearFieldFeedback("category");
                setCategory(event.target.value);
              }}
            />
            {fieldErrors.category && (
              <span className="field-error-text">{fieldErrors.category}</span>
            )}
          </div>

          <div className="form-field">
            <label htmlFor="operation-date">Дата</label>
            <input
              id="operation-date"
              name="date"
              type="date"
              value={operationDate}
              disabled={isSubmitting}
              aria-invalid={Boolean(fieldErrors.date)}
              className={fieldErrors.date ? "input-error" : ""}
              onChange={(event) => {
                clearFieldFeedback("date");
                setOperationDate(event.target.value);
              }}
            />
            {fieldErrors.date && (
              <span className="field-error-text">{fieldErrors.date}</span>
            )}
          </div>
        </div>

        {submitError && (
          <div className="form-feedback form-error" role="alert">
            <strong>Помилка збереження</strong>
            <p className="form-error-summary">{submitError}</p>
            {hasValidationErrors && (
              <ul className="form-error-list">
                {errorEntries.map(([fieldKey, message]) => (
                  <li key={fieldKey}>
                    <strong>{FIELD_LABELS[fieldKey]}:</strong> {message}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        {savedMessage && !submitError && (
          <p className="form-feedback form-success" role="status">
            {savedMessage}
          </p>
        )}

        <div className="form-actions">
          <button
            type="submit"
            className="submit-button"
            disabled={isSubmitting}
          >
            {isSubmitting ? "Збереження..." : "Зберегти"}
          </button>
        </div>
      </form>
    </section>
  );
}
