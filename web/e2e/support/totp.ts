import { createHmac } from "node:crypto";

export const TOTP_STEP_SECONDS = 30;
const TOTP_DIGITS = 6;
const BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

/** RFC 4648 base32 (no padding needed) to bytes. */
export function decodeBase32(secret: string): Buffer {
  const bytes: number[] = [];
  let bits = 0;
  let accumulator = 0;
  for (const char of secret.replace(/=+$/, "").toUpperCase()) {
    const value = BASE32_ALPHABET.indexOf(char);
    if (value < 0) throw new Error("TOTP secret is not valid base32");
    accumulator = (accumulator << 5) | value;
    bits += 5;
    if (bits >= 8) {
      bytes.push((accumulator >>> (bits - 8)) & 0xff);
      bits -= 8;
    }
  }
  return Buffer.from(bytes);
}

export function totpStepAt(epochMs: number): number {
  return Math.floor(epochMs / 1000 / TOTP_STEP_SECONDS);
}

/** RFC 6238 with HMAC-SHA1, 30 s steps, 6 digits, for an explicit time step. */
export function totpCodeForStep(base32Secret: string, step: number): string {
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(step));
  const digest = createHmac("sha1", decodeBase32(base32Secret)).update(counter).digest();
  const offset = (digest[digest.length - 1] ?? 0) & 0x0f;
  const binary =
    (((digest[offset] ?? 0) & 0x7f) << 24) |
    (((digest[offset + 1] ?? 0) & 0xff) << 16) |
    (((digest[offset + 2] ?? 0) & 0xff) << 8) |
    ((digest[offset + 3] ?? 0) & 0xff);
  return String(binary % 10 ** TOTP_DIGITS).padStart(TOTP_DIGITS, "0");
}
