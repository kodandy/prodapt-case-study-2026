import { describe, expect, it, vi } from "vitest";
import { createAuthStore, type AuthState } from "../authStore";

const EMPTY: AuthState = { userSessionToken: null, merchantApiKey: null, merchantOpsToken: null };

describe("createAuthStore clearMerchant", () => {
  it("nulls the merchant API key and ops token and keeps the user session", () => {
    // Arrange
    const store = createAuthStore();
    store.setUserSession("session-1");
    store.setMerchant("key-1", "ops-1");

    // Act
    store.clearMerchant();

    // Assert
    expect(store.get()).toEqual({ userSessionToken: "session-1", merchantApiKey: null, merchantOpsToken: null });
  });

  it("leaves an empty store empty", () => {
    // Arrange
    const store = createAuthStore();

    // Act
    store.clearMerchant();

    // Assert
    expect(store.get()).toEqual(EMPTY);
  });

  it("clears a merchant login that has an API key but no ops token yet", () => {
    // Arrange
    const store = createAuthStore();
    store.setUserSession("session-1");
    store.setMerchant("key-1", "");

    // Act
    store.clearMerchant();

    // Assert
    expect(store.get()).toEqual({ userSessionToken: "session-1", merchantApiKey: null, merchantOpsToken: null });
  });

  it("notifies listeners once with the state that keeps the user session", () => {
    // Arrange
    const store = createAuthStore();
    store.setUserSession("session-1");
    store.setMerchant("key-1", "ops-1");
    const listener = vi.fn<(state: AuthState) => void>();
    store.subscribe(listener);

    // Act
    store.clearMerchant();

    // Assert
    expect(listener).toHaveBeenCalledTimes(1);
    expect(listener).toHaveBeenCalledWith({ userSessionToken: "session-1", merchantApiKey: null, merchantOpsToken: null });
  });

  it("does not touch the user-side state when the user session is absent", () => {
    // Arrange
    const store = createAuthStore();
    store.setMerchant("key-1", "ops-1");

    // Act
    store.clearMerchant();

    // Assert
    expect(store.get()).toEqual(EMPTY);
  });
});
