export class HttpError extends Error {
  constructor(
    readonly status: number,
    readonly retryAfter: string | null,
    readonly code: string,
  ) {
    super(code);
  }
}
export async function jsonRequest(
  path: string,
  signal: AbortSignal,
  body?: { csrf: string },
): Promise<unknown> {
  if (!path.startsWith("/viewer/") || path.includes("#") || path.length > 8192)
    throw new Error("invalid_request");
  const response = await fetch(path, {
    method: body ? "POST" : "GET",
    credentials: "same-origin",
    cache: "no-store",
    redirect: "error",
    referrerPolicy: "no-referrer",
    signal,
    headers: body
      ? { "Content-Type": "application/json", Accept: "application/json" }
      : { Accept: "application/json" },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  if (!response.headers.get("content-type")?.startsWith("application/json"))
    throw new Error("invalid_response");
  const reader = response.body?.getReader();
  if (!reader) throw new Error("invalid_response");
  const chunks: Uint8Array[] = [];
  let bytes = 0;
  try {
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      bytes += part.value.byteLength;
      if (bytes > 1048576) throw new Error("response_limit");
      chunks.push(part.value);
    }
  } catch (error) {
    await reader.cancel().catch(() => undefined);
    throw error;
  } finally {
    reader.releaseLock();
  }
  const joined = new Uint8Array(bytes);
  let offset = 0;
  for (const chunk of chunks) {
    joined.set(chunk, offset);
    offset += chunk.length;
  }
  const value = JSON.parse(
    new TextDecoder("utf-8", { fatal: true }).decode(joined),
  );
  if (!response.ok)
    throw new HttpError(
      response.status,
      response.headers.get("Retry-After"),
      typeof value?.error === "string" ? value.error : "service_unavailable",
    );
  return value;
}
