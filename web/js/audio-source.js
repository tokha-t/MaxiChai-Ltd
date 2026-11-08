// Известный маршрут организаторов возвращает HTML-плеер, а не аудиопоток.
export function audioSource(raw, baseUrl) {
  if (!raw || typeof raw !== "string") return null;
  try {
    const url = new URL(raw, baseUrl);
    if (!["http:", "https:"].includes(url.protocol)) return null;
    const externalPage = url.protocol === "https:"
      && url.hostname.endsWith(".environment.api.powerplatform.com")
      && /^\/powerautomate\/automations\/direct\/cu\/27\/workflows\/[0-9a-f]+\/triggers\/manual\/paths\/invoke\/Conversation\/[0-9a-f-]+\/?$/i.test(url.pathname);
    return { url: url.href, mode: externalPage ? "external" : "native" };
  } catch { return null; }
}
