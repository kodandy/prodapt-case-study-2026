import { describe, expect, it, vi } from "vitest";
import { createAuthStore } from "../authStore";
import { createPendingWrites } from "../pendingWrites";

const SCOPE = "user:transfer";
const OTHER_SCOPE = "user:other";
const BODY = { recipient_email: "bob@example.com", amount_minor: 1250, currency: "USD" };

type Uuid = `${string}-${string}-${string}-${string}-${string}`;
const UUID_1: Uuid = "00000000-0000-4000-8000-000000000001";
const UUID_2: Uuid = "00000000-0000-4000-8000-000000000002";
const UUID_3: Uuid = "00000000-0000-4000-8000-000000000003";

function setup() {
  const store = createAuthStore();
  store.setUserSession("user-token");
  return { store, pending: createPendingWrites(store) };
}

describe("PendingWrites.settle with a key (compare-and-delete)", () => {
  it("removes the entry when the given key equals the stored key", () => {
    // Arrange
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValueOnce(UUID_1).mockReturnValueOnce(UUID_2);
    const { pending } = setup();
    const stored = pending.keyFor(SCOPE, BODY);

    // Act
    pending.settle(SCOPE, { ...BODY }, stored);

    // Assert
    expect(stored).toBe(UUID_1);
    expect(pending.keyFor(SCOPE, BODY)).toBe(UUID_2);
  });

  it("keeps the newer entry when a stale settle carries an older key", () => {
    // Arrange
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValueOnce(UUID_1).mockReturnValueOnce(UUID_2);
    const { pending } = setup();
    const stale = pending.keyFor(SCOPE, BODY);
    pending.settle(SCOPE, BODY);
    const newer = pending.keyFor(SCOPE, BODY);

    // Act
    pending.settle(SCOPE, BODY, stale);

    // Assert
    expect(stale).toBe(UUID_1);
    expect(newer).toBe(UUID_2);
    expect(pending.keyFor(SCOPE, BODY)).toBe(UUID_2);
  });

  it("keeps the entry when the key is unrelated to the stored key", () => {
    // Arrange
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValueOnce(UUID_1);
    const { pending } = setup();
    const stored = pending.keyFor(SCOPE, BODY);

    // Act
    pending.settle(SCOPE, BODY, UUID_3);

    // Assert
    expect(pending.keyFor(SCOPE, BODY)).toBe(stored);
    expect(stored).toBe(UUID_1);
  });

  it("does not remove an entry in another scope even when the key matches that entry", () => {
    // Arrange
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValueOnce(UUID_1).mockReturnValueOnce(UUID_2);
    const { pending } = setup();
    const keyInScope = pending.keyFor(SCOPE, BODY);
    const keyInOther = pending.keyFor(OTHER_SCOPE, BODY);

    // Act
    pending.settle(OTHER_SCOPE, BODY, keyInScope);

    // Assert
    expect(pending.keyFor(OTHER_SCOPE, BODY)).toBe(keyInOther);
    expect(pending.keyFor(SCOPE, BODY)).toBe(keyInScope);
  });

  it("does nothing, and does not throw, for a scope and body that holds no entry", () => {
    // Arrange
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValueOnce(UUID_1);
    const { pending } = setup();

    // Act
    const settle = () => pending.settle(SCOPE, BODY, UUID_3);

    // Assert
    expect(settle).not.toThrow();
    expect(pending.keyFor(SCOPE, BODY)).toBe(UUID_1);
  });

  it("keeps an entry minted after the credential was cleared when a settle with the pre-clear key arrives late", () => {
    // Arrange
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValueOnce(UUID_1).mockReturnValueOnce(UUID_2);
    const { store, pending } = setup();
    const aliceKey = pending.keyFor(SCOPE, BODY);
    store.clear();
    store.setUserSession("bob-token");
    const bobKey = pending.keyFor(SCOPE, BODY);

    // Act
    pending.settle(SCOPE, BODY, aliceKey);

    // Assert
    expect(aliceKey).toBe(UUID_1);
    expect(bobKey).toBe(UUID_2);
    expect(pending.keyFor(SCOPE, BODY)).toBe(UUID_2);
  });

  it("compares keys exactly, so a differently cased copy of the stored key does not settle it", () => {
    // Arrange
    const { pending } = setup();
    const stored = pending.keyFor(SCOPE, BODY);

    // Act
    pending.settle(SCOPE, BODY, stored.toUpperCase());

    // Assert
    expect(pending.keyFor(SCOPE, BODY)).toBe(stored);
  });
});

describe("PendingWrites.settle without a key", () => {
  it("still removes the entry unconditionally", () => {
    // Arrange
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValueOnce(UUID_1).mockReturnValueOnce(UUID_2);
    const { pending } = setup();
    pending.keyFor(SCOPE, BODY);

    // Act
    pending.settle(SCOPE, BODY);

    // Assert
    expect(pending.keyFor(SCOPE, BODY)).toBe(UUID_2);
  });
});
