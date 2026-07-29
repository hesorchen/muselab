// Lazy file-surface capability helpers. Kept out of app.js so credential
// minting and browser-triggered downloads have one small, testable boundary.

export async function mintTicket(endpoint, headers, body = undefined) {
  const options = { method: "POST", headers };
  if (body !== undefined) options.body = JSON.stringify(body);
  const response = await fetch(endpoint, options);
  if (!response.ok) {
    throw new Error(`capability ticket failed (${response.status})`);
  }
  const payload = await response.json();
  if (!payload || !payload.ticket) {
    throw new Error("capability ticket missing");
  }
  return payload.ticket;
}

export function triggerDownload(url, filename) {
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename || "download";
  anchor.rel = "noopener";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}
