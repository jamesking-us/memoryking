import { readFileSync, statSync } from "node:fs";
import { MemoryKingError } from "./errors.js";

export interface CredentialSource {
  credential?: string;
  credentialFile?: string;
}

/** Exactly one of `credential` and `credentialFile` must be given. */
export function requireOneCredential(source: CredentialSource): void {
  if (
    (source.credential === undefined) ===
    (source.credentialFile === undefined)
  ) {
    throw new MemoryKingError("credential_required");
  }
}

/** The credential, read fresh from a private file so a rotation is picked up. */
export function readCredential(source: CredentialSource): string {
  if (source.credential !== undefined) return source.credential;
  const file = source.credentialFile as string;
  let token: string;
  try {
    if (process.platform !== "win32" && (statSync(file).mode & 0o077) !== 0) {
      throw new MemoryKingError("credential_file_not_private");
    }
    token = readFileSync(file, "utf8").trim();
  } catch (error) {
    if (error instanceof MemoryKingError) throw error;
    throw new MemoryKingError("credential_required");
  }
  if (token === "" || token.length > 8192)
    throw new MemoryKingError("invalid_token");
  return token;
}
