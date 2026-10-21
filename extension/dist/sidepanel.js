import { safeUrl, samePage } from "./url.js";
const PROVIDERS = ["xai", "openai", "anthropic", "gemini"];
const ROLES = ["gate", "grade", "rewrite", "rerank", "synthesize", "critic"];
const inExtension = typeof chrome !== "undefined" && Boolean(chrome.runtime?.id);
const briefEl = document.querySelector("#brief");
const settingsEl = document.querySelector("#settings-view");
const statusEl = document.querySelector("#status");
const metaEl = document.querySelector("#meta");
function setStatus(message) {
    statusEl.textContent = message;
}
async function storedConfig() {
    if (inExtension) {
        const stored = await chrome.storage.local.get(["apiBase", "apiToken", "etag"]);
        return {
            apiBase: stored.apiBase || "http://127.0.0.1:8787",
            apiToken: stored.apiToken || "dev-token",
            etag: stored.etag || "",
        };
    }
    return {
        apiBase: localStorage.getItem("apiBase") || location.origin,
        apiToken: localStorage.getItem("apiToken") || "dev-token",
        etag: localStorage.getItem("etag") || "",
    };
}
async function saveConfig(partial) {
    if (inExtension) {
        await chrome.storage.local.set(partial);
        return;
    }
    for (const [key, value] of Object.entries(partial))
        localStorage.setItem(key, value);
}
async function api(path, init = {}) {
    const { apiBase, apiToken, etag } = await storedConfig();
    const headers = new Headers(init.headers);
    headers.set("Authorization", "Bearer " + apiToken);
    if (init.method === undefined && etag)
        headers.set("If-None-Match", etag);
    return fetch(apiBase.replace(/\/$/, "") + path, { ...init, headers });
}
function field(label, text) {
    const block = document.createElement("div");
    block.className = "field";
    const name = document.createElement("span");
    name.textContent = label;
    const body = document.createElement("p");
    body.textContent = text;
    block.append(name, body);
    return block;
}
async function openSource(url) {
    const safe = safeUrl(url);
    if (!safe) {
        setStatus("Blocked a non-http link.");
        return;
    }
    if (inExtension) {
        await chrome.tabs.create({ url: safe });
        return;
    }
    window.open(safe, "_blank", "noopener");
}
function renderEdition(edition) {
    briefEl.replaceChildren();
    metaEl.textContent =
        edition.edition_date +
            (edition.generator === "fake" ? " · demo model" : " · " + (edition.model || "your keys"));
    if (!edition.items.length) {
        const empty = document.createElement("p");
        empty.textContent = "No claims survived the evidence check.";
        briefEl.append(empty);
    }
    for (const item of edition.items) {
        const article = document.createElement("article");
        const title = document.createElement("h2");
        title.textContent = item.title;
        article.append(title, field("Decision", item.decision), field("Mechanism", item.mechanism), field("What breaks", item.what_breaks), field("What to copy", item.what_to_copy));
        const sources = document.createElement("div");
        sources.className = "sources";
        for (const source of item.sources) {
            const safe = safeUrl(source.url);
            if (!safe)
                continue;
            const control = document.createElement(inExtension ? "button" : "a");
            if (control instanceof HTMLAnchorElement) {
                control.href = safe;
                control.target = "_blank";
                control.rel = "noopener";
            }
            else {
                control.type = "button";
                control.addEventListener("click", () => void openSource(safe));
            }
            const kind = document.createElement("span");
            kind.className = "kind";
            kind.textContent = source.kind + " ";
            const label = document.createElement("span");
            label.textContent = source.title;
            control.append(kind, label);
            sources.append(control);
        }
        article.append(sources);
        briefEl.append(article);
    }
    if (edition.dropped?.length) {
        const note = document.createElement("p");
        note.textContent =
            edition.dropped.length +
                (edition.dropped.length === 1 ? " slot" : " slots") +
                " dropped for thin evidence.";
        briefEl.append(note);
    }
}
async function readCachedEdition() {
    if (inExtension) {
        const stored = await chrome.storage.local.get(["edition"]);
        return stored.edition || null;
    }
    const raw = localStorage.getItem("edition");
    return raw ? JSON.parse(raw) : null;
}
async function loadEdition() {
    setStatus("Loading edition…");
    const response = await api("/v1/editions/latest");
    if (response.status === 304) {
        const cached = await readCachedEdition();
        if (cached)
            renderEdition(cached);
        setStatus("");
        return;
    }
    if (response.status === 404) {
        briefEl.replaceChildren();
        metaEl.textContent = "";
        const empty = document.createElement("p");
        empty.textContent = "No edition yet.";
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = "Build demo edition";
        button.addEventListener("click", () => void buildDemo());
        briefEl.append(empty, button);
        setStatus("");
        return;
    }
    if (!response.ok) {
        setStatus("Could not load the edition (" + response.status + ").");
        return;
    }
    const edition = (await response.json());
    const etag = response.headers.get("ETag") || "";
    await saveConfig({ etag });
    if (inExtension)
        await chrome.storage.local.set({ edition });
    else
        localStorage.setItem("edition", JSON.stringify(edition));
    renderEdition(edition);
    setStatus("");
    await markActiveTab(edition);
}
async function markActiveTab(edition) {
    if (!inExtension)
        return;
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.url || tab.id === undefined)
        return;
    const hit = edition.items.some((item) => item.sources.some((source) => samePage(tab.url || "", source.url)));
    await chrome.action.setBadgeText({ tabId: tab.id, text: hit ? "1" : "" });
}
async function buildDemo() {
    setStatus("Building a demo edition…");
    const response = await api("/v1/jobs/demo", { method: "POST" });
    if (!response.ok) {
        const detail = await response.text();
        setStatus(detail.slice(0, 240));
        return;
    }
    await loadEdition();
}
function select(options, value) {
    const element = document.createElement("select");
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = "Provider";
    element.append(blank);
    for (const option of options) {
        const node = document.createElement("option");
        node.value = option;
        node.textContent = option;
        element.append(node);
    }
    element.value = value;
    return element;
}
async function renderSettings() {
    settingsEl.replaceChildren();
    let response;
    try {
        response = await api("/v1/settings");
    }
    catch (error) {
        setStatus(error instanceof Error ? error.message : "Could not reach the API.");
        return;
    }
    if (!response.ok) {
        setStatus("Settings need a reachable API and token.");
        return;
    }
    const payload = (await response.json());
    const known = new Map(payload.credentials.map((item) => [item.provider, item]));
    if (known.has("xai") && ROLES.every((role) => !payload.config.roles[role]?.provider)) {
        await assignProvider("xai");
        const again = await api("/v1/settings");
        if (again.ok) {
            const next = (await again.json());
            payload.config = next.config;
        }
    }
    const { apiBase, apiToken } = await storedConfig();
    const connection = document.createElement("div");
    connection.append(labeled("API base", "api-base", apiBase), labeled("API token", "api-token", apiToken, "password"));
    const saveConnection = document.createElement("button");
    saveConnection.type = "button";
    saveConnection.textContent = "Save connection";
    saveConnection.addEventListener("click", () => void saveConnectionClick());
    connection.append(saveConnection);
    const keys = document.createElement("div");
    keys.className = "keys";
    for (const provider of PROVIDERS) {
        const row = document.createElement("div");
        row.className = "key-row";
        const name = document.createElement("strong");
        name.textContent = provider;
        const input = document.createElement("input");
        input.type = "password";
        input.placeholder = known.get(provider) ? "saved · " + known.get(provider)?.last4 : "API key";
        input.dataset.provider = provider;
        input.className = "key";
        const save = document.createElement("button");
        save.type = "button";
        save.textContent = "Save";
        save.addEventListener("click", () => void saveKey(provider, input.value));
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "Remove";
        remove.addEventListener("click", () => void removeKey(provider));
        row.append(name, input, save, remove);
        keys.append(row);
    }
    const roles = document.createElement("div");
    const embedProvider = select(["xai", "openai", "gemini"], payload.config.embed.provider);
    embedProvider.id = "embed-provider";
    const embedModel = document.createElement("input");
    embedModel.id = "embed-model";
    embedModel.type = "text";
    embedModel.value = payload.config.embed.model;
    embedModel.placeholder = "text-embedding-3-large";
    roles.append(wrap("Embed provider", embedProvider), wrap("Embed model", embedModel));
    for (const role of ROLES) {
        const current = payload.config.roles[role] || { provider: "", model: "" };
        const provider = select(PROVIDERS, current.provider);
        provider.dataset.role = role;
        provider.className = "role-provider";
        const model = document.createElement("input");
        model.type = "text";
        model.value = current.model;
        model.dataset.role = role;
        model.className = "role-model";
        model.placeholder = role === "synthesize" || role === "critic" ? "strong model id" : "fast model id";
        roles.append(wrap(role, provider), wrap(role + " model", model));
    }
    const saveRoles = document.createElement("button");
    saveRoles.type = "button";
    saveRoles.textContent = "Save roles";
    saveRoles.addEventListener("click", () => void saveRolesClick());
    const build = document.createElement("button");
    build.type = "button";
    build.textContent = "Build edition";
    build.addEventListener("click", () => void buildEdition());
    settingsEl.append(connection, keys, roles, saveRoles, build);
}
function labeled(caption, id, value, type = "text") {
    const label = document.createElement("label");
    const title = document.createElement("em");
    title.textContent = caption;
    const input = document.createElement("input");
    input.id = id;
    input.type = type;
    input.value = value;
    label.append(title, input);
    return label;
}
function wrap(caption, control) {
    const label = document.createElement("label");
    const title = document.createElement("em");
    title.textContent = caption;
    label.append(title, control);
    return label;
}
async function saveConnectionClick() {
    const apiBase = document.querySelector("#api-base").value.trim();
    const apiToken = document.querySelector("#api-token").value.trim();
    if (inExtension && !apiBase.startsWith("http://127.0.0.1") && !apiBase.startsWith("http://localhost")) {
        const origin = new URL(apiBase).origin + "/*";
        const granted = await chrome.permissions.request({ origins: [origin] });
        if (!granted) {
            setStatus("Host permission was not granted.");
            return;
        }
    }
    await saveConfig({ apiBase, apiToken });
    setStatus("Connection saved.");
    await loadEdition();
}
async function saveKey(provider, apiKey) {
    if (!apiKey.trim()) {
        setStatus("Paste a key before saving.");
        return;
    }
    const response = await api("/v1/credentials", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider, api_key: apiKey }),
    });
    if (!response.ok) {
        setStatus(await response.text());
        return;
    }
    setStatus(provider + " key stored. The brief on screen is still the demo until you build a new edition.");
    await assignProvider(provider);
    await renderSettings();
}
const DEFAULT_CHAT_MODEL = {
    xai: "grok-4.7",
};
const DEFAULT_EMBED_MODEL = {
    xai: "v1",
    openai: "text-embedding-3-large",
    gemini: "gemini-embedding-2",
};
async function assignProvider(provider) {
    const current = await api("/v1/settings");
    if (!current.ok)
        return;
    const payload = (await current.json());
    const roles = payload.config.roles;
    const alreadySet = ROLES.some((role) => roles[role]?.provider);
    if (alreadySet)
        return;
    const chatModel = DEFAULT_CHAT_MODEL[provider] || "";
    const nextRoles = {};
    for (const role of ROLES)
        nextRoles[role] = { provider, model: chatModel };
    await api("/v1/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            embed: {
                provider: DEFAULT_EMBED_MODEL[provider] ? provider : "",
                model: DEFAULT_EMBED_MODEL[provider] || "",
                dimensions: 768,
            },
            roles: nextRoles,
        }),
    });
}
async function removeKey(provider) {
    const response = await api("/v1/credentials/" + provider, { method: "DELETE" });
    if (!response.ok) {
        setStatus(await response.text());
        return;
    }
    setStatus(provider + " key removed.");
    await renderSettings();
}
async function saveRolesClick() {
    const roles = {};
    for (const role of ROLES) {
        const provider = document.querySelector(`.role-provider[data-role="${role}"]`).value;
        const model = document.querySelector(`.role-model[data-role="${role}"]`).value.trim();
        roles[role] = { provider, model };
    }
    const body = {
        embed: {
            provider: document.querySelector("#embed-provider").value,
            model: document.querySelector("#embed-model").value.trim(),
            dimensions: 768,
        },
        roles,
    };
    const response = await api("/v1/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    });
    if (!response.ok) {
        setStatus(await response.text());
        return false;
    }
    setStatus("Roles saved.");
    return true;
}
async function buildEdition() {
    setStatus("Saving roles, then calling the model with your key.");
    const saved = await saveRolesClick();
    if (!saved)
        return;
    let response;
    try {
        response = await api("/v1/editions/run", { method: "POST" });
    }
    catch (error) {
        setStatus(error instanceof Error ? error.message : "Could not reach the API.");
        return;
    }
    if (!response.ok) {
        setStatus(await response.text());
        return;
    }
    settingsEl.setAttribute("hidden", "");
    briefEl.removeAttribute("hidden");
    await loadEdition();
}
async function fetchLatest() {
    setStatus("Fetching arXiv, Cloudflare, and Kubernetes, then writing the brief with Grok…");
    let response;
    try {
        response = await api("/v1/jobs/daily", { method: "POST" });
    }
    catch (error) {
        setStatus(error instanceof Error ? error.message : "Could not reach the API.");
        return;
    }
    if (!response.ok) {
        setStatus(await response.text());
        return;
    }
    await saveConfig({ etag: "" });
    settingsEl.setAttribute("hidden", "");
    briefEl.removeAttribute("hidden");
    await loadEdition();
}
document.querySelector("#refresh").addEventListener("click", () => void loadEdition());
document.querySelector("#fetch-latest").addEventListener("click", () => void fetchLatest());
document.querySelector("#settings").addEventListener("click", () => {
    const open = settingsEl.hasAttribute("hidden");
    if (open) {
        settingsEl.removeAttribute("hidden");
        briefEl.setAttribute("hidden", "");
        settingsEl.scrollIntoView({ block: "start" });
        void renderSettings();
    }
    else {
        settingsEl.setAttribute("hidden", "");
        briefEl.removeAttribute("hidden");
    }
});
void loadEdition().catch((error) => {
    setStatus(error instanceof Error ? error.message : "Failed to load");
});
