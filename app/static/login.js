const form = document.getElementById("login-form");
const username = document.getElementById("username");
const password = document.getElementById("password");
const confirmation = document.getElementById("confirm-password");
const button = document.getElementById("login-button");
const toggle = document.getElementById("password-toggle");
const errorMessage = document.getElementById("login-error");
let setupMode = false;

function setBusy(busy) {
  form.setAttribute("aria-busy", String(busy));
  username.disabled = busy;
  password.disabled = busy;
  confirmation.disabled = busy || !setupMode;
  toggle.disabled = busy;
  button.disabled = busy;
  const label = setupMode ? "Create login & enter workspace" : "Sign in to FinFlow";
  button.replaceChildren(document.createTextNode(busy ? (setupMode ? "Creating your login…" : "Signing in…") : label));
  const arrow = document.createElement("span");
  arrow.textContent = "→";
  arrow.setAttribute("aria-hidden", "true");
  button.append(arrow);
}

function setMode(setup) {
  setupMode = setup;
  document.getElementById("setup-notice").hidden = !setup;
  document.getElementById("confirm-password-field").hidden = !setup;
  confirmation.required = setup;
  confirmation.value = "";
  password.minLength = setup ? 12 : 1;
  password.autocomplete = setup ? "new-password" : "current-password";
  document.getElementById("login-description").textContent = setup
    ? "Choose your credentials to enter your FinFlow workspace."
    : "Sign in to your FinFlow workspace.";
}

function showError(message) {
  errorMessage.textContent = message;
  errorMessage.hidden = false;
}

async function checkAccess() {
  const response = await fetch("/api/auth/session", { cache: "no-store" });
  if (!response.ok) throw new Error("Unable to check access. Refresh the page to try again.");
  const session = await response.json();
  if (session.authenticated) {
    window.location.replace("/");
    return;
  }
  setMode(!session.configured);
  setBusy(false);
  username.focus();
}

toggle.addEventListener("click", () => {
  const show = password.type === "password";
  password.type = show ? "text" : "password";
  toggle.textContent = show ? "Hide" : "Show";
  toggle.setAttribute("aria-label", show ? "Hide password" : "Show password");
  toggle.setAttribute("aria-pressed", String(show));
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorMessage.hidden = true;
  if (setupMode && password.value !== confirmation.value) {
    showError("Passwords do not match. Please confirm your password.");
    confirmation.value = "";
    confirmation.focus();
    return;
  }
  const credentials = { username: username.value.trim(), password: password.value };
  setBusy(true);
  try {
    const response = await fetch(setupMode ? "/api/auth/setup" : "/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(credentials),
    });
    const result = await response.json();
    if (!response.ok) {
      if (setupMode && response.status === 409) {
        setMode(false);
        throw new Error("An owner account already exists. Sign in with its username and password.");
      }
      if (!setupMode && response.status === 503) {
        setMode(true);
        throw new Error("Create your private login below to enter the workspace.");
      }
      throw new Error(typeof result.detail === "string" ? result.detail : "Check your username and password and try again.");
    }
    password.value = "";
    confirmation.value = "";
    window.location.replace("/");
  } catch (error) {
    password.value = "";
    confirmation.value = "";
    showError(error.message);
    setBusy(false);
    password.focus();
  }
});

window.addEventListener("pageshow", (event) => {
  if (event.persisted) window.location.reload();
});

checkAccess().catch((error) => {
  button.textContent = "Refresh to try again";
  form.setAttribute("aria-busy", "false");
  showError(error.message);
});
