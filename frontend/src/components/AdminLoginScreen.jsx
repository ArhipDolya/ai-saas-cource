import { useState } from "react";

export default function AdminLoginScreen({
  loginEndpoint,
  initialNotice,
  onLoginSuccess,
}) {
  const [password, setPassword] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState(initialNotice || null);

  async function handleSubmit(event) {
    event.preventDefault();
    setErrorMessage(null);

    if (!password) {
      setErrorMessage("Введіть пароль адміністратора.");
      return;
    }

    setIsSubmitting(true);
    try {
      const response = await fetch(loginEndpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ password }),
      });
      const responseBody = await response.json().catch(() => null);
      if (!response.ok) {
        const errorDetail =
          responseBody && responseBody.detail
            ? responseBody.detail
            : `Помилка входу (HTTP ${response.status})`;
        throw new Error(errorDetail);
      }

      if (!responseBody || !responseBody.token) {
        throw new Error("Сервер не повернув токен авторизації.");
      }

      setPassword("");
      onLoginSuccess(responseBody.token);
    } catch (error) {
      const message =
        error instanceof Error
          ? error.message
          : "Не вдалося виконати вхід. Спробуйте ще раз.";
      setErrorMessage(message);
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <div className="login-brand">
          <span className="brand-badge">ADMIN</span>
          <span className="login-brand-title">Finance SaaS</span>
        </div>

        <h1 className="login-title">Вхід в адмін-панель</h1>
        <p className="login-subtitle">
          Введіть пароль з налаштувань адміністратора, щоб переглядати та
          керувати транзакціями.
        </p>

        <form className="login-form" onSubmit={handleSubmit} noValidate>
          <div className="form-field">
            <label htmlFor="admin-password">Пароль</label>
            <input
              id="admin-password"
              name="password"
              type="password"
              autoComplete="current-password"
              placeholder="Введіть пароль адміністратора"
              value={password}
              disabled={isSubmitting}
              aria-invalid={Boolean(errorMessage)}
              className={errorMessage ? "input-error" : ""}
              autoFocus
              onChange={(event) => {
                if (errorMessage) {
                  setErrorMessage(null);
                }
                setPassword(event.target.value);
              }}
            />
          </div>

          {errorMessage && (
            <p className="form-feedback form-error" role="alert">
              {errorMessage}
            </p>
          )}

          <button
            type="submit"
            className="submit-button login-submit-button"
            disabled={isSubmitting}
          >
            {isSubmitting ? "Перевірка..." : "Увійти"}
          </button>
        </form>
      </div>
    </div>
  );
}
