export function safeUrl(raw) {
    try {
        const url = new URL(raw);
        if (url.protocol !== "https:" && url.protocol !== "http:")
            return null;
        return url.toString();
    }
    catch {
        return null;
    }
}
export function pageKey(raw) {
    const safe = safeUrl(raw);
    if (!safe)
        return null;
    const url = new URL(safe);
    const dropped = [];
    url.searchParams.forEach((_value, key) => {
        if (key === "fbclid" || key === "gclid" || key.startsWith("utm_"))
            dropped.push(key);
    });
    for (const key of dropped)
        url.searchParams.delete(key);
    url.hash = "";
    if (url.pathname.length > 1)
        url.pathname = url.pathname.replace(/\/+$/, "");
    url.hostname = url.hostname.toLowerCase();
    return url.toString();
}
export function samePage(left, right) {
    const a = pageKey(left);
    const b = pageKey(right);
    return Boolean(a && b && a === b);
}
