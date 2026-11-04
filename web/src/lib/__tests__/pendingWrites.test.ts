import { describe, expect, it, vi } from "vitest";
import { createAuthStore, type AuthListener, type AuthState, type AuthStore } from "../authStore";
import { createPendingWrites } from "../pendingWrites";

const SIGNED_IN: AuthState = { userSessionToken: "user-token", merchantApiKey: "merchant-key", merchantOpsToken: "ops-token" };

const TRANSFER_SCOPE = "user:transfer";
const REFUND_SCOPE = "merchant:refund:pay_1";
const TRANSFER_BODY = { recipient_email: "bob@example.com", amount_minor: 1250, currency: "USD" };

type Uuid = `${string}-${string}-${string}-${string}-${string}`;
const UUID_1: Uuid = "00000000-0000-4000-8000-000000000001";
const UUID_2: Uuid = "00000000-0000-4000-8000-000000000002";

/** An AuthStore whose state the test sets directly, so one credential can go null while the other stays. */
function createFakeStore(initial: AuthState): { store: AuthStore; emit: (next: AuthState) => void } {
  let state = initial;
  const listeners = new Set<AuthListener>();
  const store: AuthStore = {
    get: () => state,
    setUserSession: () => undefined,
    setMerchant: () => undefined,
    setMerchantOpsToken: () => undefined,
    clear: () => undefined,
    clearMerchant: () => undefined,
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
  return {
    store,
    emit(next) {
      state = next;
      for (const listener of [...listeners]) listener(state);
    },
  };
}

function setup(initial: AuthState = SIGNED_IN) {
  const fake = createFakeStore(initial);
  return { ...fake, pending: createPendingWrites(fake.store) };
}

describe("createPendingWrites", () => {
  describe("keyFor", () => {
    it("returns an Idempotency-Key from newIdempotencyKey for a new scope and body", () => {
      // Arrange
      vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValueOnce(UUID_1);
      const { pending } = setup();

      // Act
      const key = pending.keyFor(TRANSFER_SCOPE, TRANSFER_BODY);

      // Assert
      expect(key).toBe(UUID_1);
    });

    it("returns the same key for the same scope and body", () => {
      // Arrange
      const { pending } = setup();
      const first = pending.keyFor(TRANSFER_SCOPE, TRANSFER_BODY);

      // Act
      const second = pending.keyFor(TRANSFER_SCOPE, { ...TRANSFER_BODY });

      // Assert
      expect(second).toBe(first);
      expect(first).not.toBe("");
    });

    it("does not call newIdempotencyKey again for a body it already holds", () => {
      // Arrange
      const uuid = vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValueOnce(UUID_1).mockReturnValueOnce(UUID_2);
      const { pending } = setup();
      pending.keyFor(TRANSFER_SCOPE, TRANSFER_BODY);

      // Act
      const again = pending.keyFor(TRANSFER_SCOPE, TRANSFER_BODY);

      // Assert
      expect(again).toBe(UUID_1);
      expect(uuid).toHaveBeenCalledTimes(1);
    });

    it("ignores object key order when fingerprinting the body", () => {
      // Arrange
      const { pending } = setup();
      const first = pending.keyFor(TRANSFER_SCOPE, { recipient_email: "bob@example.com", amount_minor: 1250, currency: "USD" });

      // Act
      const second = pending.keyFor(TRANSFER_SCOPE, { currency: "USD", amount_minor: 1250, recipient_email: "bob@example.com" });

      // Assert
      expect(second).toBe(first);
    });

    it("ignores object key order in nested objects", () => {
      // Arrange
      const { pending } = setup();
      const first = pending.keyFor(TRANSFER_SCOPE, { outer: { a: 1, b: 2 }, list: [{ x: 1, y: 2 }] });

      // Act
      const second = pending.keyFor(TRANSFER_SCOPE, { list: [{ y: 2, x: 1 }], outer: { b: 2, a: 1 } });

      // Assert
      expect(second).toBe(first);
    });

    it("returns a different key for a different body in the same scope", () => {
      // Arrange
      const { pending } = setup();
      const first = pending.keyFor(TRANSFER_SCOPE, TRANSFER_BODY);

      // Act
      const second = pending.keyFor(TRANSFER_SCOPE, { ...TRANSFER_BODY, amount_minor: 1251 });

      // Assert
      expect(second).not.toBe(first);
      expect(second).not.toBe("");
    });

    it("returns a different key for the same body in a different scope", () => {
      // Arrange
      const { pending } = setup();
      const body = { amount_minor: 4000 };
      const first = pending.keyFor("merchant:refund:pay_1", body);

      // Act
      const second = pending.keyFor("merchant:refund:pay_2", body);

      // Assert
      expect(second).not.toBe(first);
      expect(second).not.toBe("");
    });

    it("keeps each body's key stable while another body is also pending", () => {
      // Arrange
      const { pending } = setup();
      const other = { ...TRANSFER_BODY, amount_minor: 99 };
      const keyA = pending.keyFor(TRANSFER_SCOPE, TRANSFER_BODY);
      const keyB = pending.keyFor(TRANSFER_SCOPE, other);

      // Act
      const againA = pending.keyFor(TRANSFER_SCOPE, TRANSFER_BODY);
      const againB = pending.keyFor(TRANSFER_SCOPE, other);

      // Assert
      expect([againA, againB]).toEqual([keyA, keyB]);
      expect(keyA).not.toBe(keyB);
    });
  });

  describe("settle", () => {
    it("makes the next keyFor for that scope and body return a new key", () => {
      // Arrange
      const { pending } = setup();
      const first = pending.keyFor(TRANSFER_SCOPE, TRANSFER_BODY);

      // Act
      pending.settle(TRANSFER_SCOPE, { ...TRANSFER_BODY });
      const second = pending.keyFor(TRANSFER_SCOPE, TRANSFER_BODY);

      // Assert
      expect(second).not.toBe(first);
      expect(second).not.toBe("");
    });

    it("removes only the settled body and keeps other bodies in the same scope", () => {
      // Arrange
      const { pending } = setup();
      const other = { ...TRANSFER_BODY, amount_minor: 99 };
      pending.keyFor(TRANSFER_SCOPE, TRANSFER_BODY);
      const otherKey = pending.keyFor(TRANSFER_SCOPE, other);

      // Act
      pending.settle(TRANSFER_SCOPE, TRANSFER_BODY);

      // Assert
      expect(pending.keyFor(TRANSFER_SCOPE, other)).toBe(otherKey);
    });

    it("removes only the settled scope and keeps the same body in another scope", () => {
      // Arrange
      const { pending } = setup();
      const body = { amount_minor: 4000 };
      pending.keyFor("merchant:refund:pay_1", body);
      const otherScopeKey = pending.keyFor("merchant:refund:pay_2", body);

      // Act
      pending.settle("merchant:refund:pay_1", body);

      // Assert
      expect(pending.keyFor("merchant:refund:pay_2", body)).toBe(otherScopeKey);
    });

    it("does nothing for a scope and body that holds no key", () => {
      // Arrange
      const { pending } = setup();
      const held = pending.keyFor(TRANSFER_SCOPE, TRANSFER_BODY);

      // Act
      pending.settle(TRANSFER_SCOPE, { ...TRANSFER_BODY, amount_minor: 7 });
      pending.settle("user:unknown", {});

      // Assert
      expect(pending.keyFor(TRANSFER_SCOPE, TRANSFER_BODY)).toBe(held);
    });
  });

  describe("when the auth store changes", () => {
    it("drops user entries when userSessionToken becomes null", () => {
      // Arrange
      const { pending, emit } = setup();
      const before = pending.keyFor(TRANSFER_SCOPE, TRANSFER_BODY);

      // Act
      emit({ ...SIGNED_IN, userSessionToken: null });

      // Assert
      const after = pending.keyFor(TRANSFER_SCOPE, TRANSFER_BODY);
      expect(after).not.toBe(before);
      expect(after).not.toBe("");
    });

    it("keeps merchant entries when only userSessionToken becomes null", () => {
      // Arrange
      const { pending, emit } = setup();
      const refundKey = pending.keyFor(REFUND_SCOPE, { amount_minor: 4000 });

      // Act
      emit({ ...SIGNED_IN, userSessionToken: null });

      // Assert
      expect(pending.keyFor(REFUND_SCOPE, { amount_minor: 4000 })).toBe(refundKey);
    });

    it("drops merchant entries when merchantApiKey becomes null", () => {
      // Arrange
      const { pending, emit } = setup();
      const before = pending.keyFor(REFUND_SCOPE, { amount_minor: 4000 });

      // Act
      emit({ ...SIGNED_IN, merchantApiKey: null });

      // Assert
      const after = pending.keyFor(REFUND_SCOPE, { amount_minor: 4000 });
      expect(after).not.toBe(before);
      expect(after).not.toBe("");
    });

    it("keeps user entries when only merchantApiKey becomes null", () => {
      // Arrange
      const { pending, emit } = setup();
      const transferKey = pending.keyFor(TRANSFER_SCOPE, TRANSFER_BODY);

      // Act
      emit({ ...SIGNED_IN, merchantApiKey: null });

      // Assert
      expect(pending.keyFor(TRANSFER_SCOPE, TRANSFER_BODY)).toBe(transferKey);
    });

    it("drops every entry of the signed-out kind, not just one", () => {
      // Arrange
      const { pending, emit } = setup();
      const refundA = pending.keyFor("merchant:refund:pay_1", { amount_minor: 100 });
      const refundB = pending.keyFor("merchant:refund:pay_2", { amount_minor: 200 });

      // Act
      emit({ ...SIGNED_IN, merchantApiKey: null });

      // Assert
      expect(pending.keyFor("merchant:refund:pay_1", { amount_minor: 100 })).not.toBe(refundA);
      expect(pending.keyFor("merchant:refund:pay_2", { amount_minor: 200 })).not.toBe(refundB);
    });

    it("keeps all entries on a change that leaves both credentials set", () => {
      // Arrange
      const { pending, emit } = setup();
      const transferKey = pending.keyFor(TRANSFER_SCOPE, TRANSFER_BODY);
      const refundKey = pending.keyFor(REFUND_SCOPE, { amount_minor: 4000 });

      // Act
      emit({ ...SIGNED_IN, merchantOpsToken: "ops-token-2" });

      // Assert
      expect(pending.keyFor(TRANSFER_SCOPE, TRANSFER_BODY)).toBe(transferKey);
      expect(pending.keyFor(REFUND_SCOPE, { amount_minor: 4000 })).toBe(refundKey);
    });

    it("gives the same body a new key after a real store is cleared", () => {
      // Arrange
      const store = createAuthStore();
      store.setUserSession("user-token");
      store.setMerchant("merchant-key", "ops-token");
      const pending = createPendingWrites(store);
      const transferBefore = pending.keyFor(TRANSFER_SCOPE, TRANSFER_BODY);
      const refundBefore = pending.keyFor(REFUND_SCOPE, { amount_minor: 4000 });

      // Act
      store.clear();

      // Assert
      expect(pending.keyFor(TRANSFER_SCOPE, TRANSFER_BODY)).not.toBe(transferBefore);
      expect(pending.keyFor(REFUND_SCOPE, { amount_minor: 4000 })).not.toBe(refundBefore);
    });
  });
});
