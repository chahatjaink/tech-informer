import { samePage } from "./url.js";
const DEFAULTS = {
    apiBase: "http://127.0.0.1:8787",
    apiToken: "dev-token",
};
async function config() {
    const stored = await chrome.storage.local.get(["apiBase", "apiToken", "etag"]);
    return {
        apiBase: stored.apiBase || DEFAULTS.apiBase,
        apiToken: stored.apiToken || DEFAULTS.apiToken,
        etag: stored.etag,
    };
}
export async function refreshEdition() {
    try {
        const { apiBase, apiToken, etag } = await config();
        const headers = { Authorization: "Bearer " + apiToken };
        if (etag)
            headers["If-None-Match"] = etag;
        const response = await fetch(apiBase.replace(/\/$/, "") + "/v1/editions/latest", { headers });
        if (response.status === 304)
            return;
        if (!response.ok)
            return;
        const edition = await response.json();
        await chrome.storage.local.set({
            edition,
            etag: response.headers.get("ETag"),
        });
    }
    catch {
        // The local API is down. The alarm retries; the side panel reports the error.
    }
}
async function badge(tabId, tabUrl) {
    if (tabId === undefined)
        return;
    const stored = await chrome.storage.local.get(["edition"]);
    const edition = stored.edition;
    const hit = Boolean(tabUrl &&
        edition?.items?.some((item) => item.sources?.some((source) => samePage(tabUrl, source.url))));
    await chrome.action.setBadgeText({ tabId, text: hit ? "1" : "" });
}
chrome.runtime.onInstalled.addListener(() => {
    chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => undefined);
    chrome.alarms.create("refresh", { periodInMinutes: 30 });
    chrome.storage.local.get(["apiBase"]).then((stored) => {
        if (!stored.apiBase)
            void chrome.storage.local.set(DEFAULTS);
        void refreshEdition();
    });
});
chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === "refresh")
        void refreshEdition();
});
chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
    if (info.status === "complete")
        void badge(tabId, tab.url);
});
chrome.tabs.onActivated.addListener((active) => {
    chrome.tabs.get(active.tabId).then((tab) => badge(tab.id, tab.url)).catch(() => undefined);
});
