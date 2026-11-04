import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { createAuthStore, type AuthState } from "../authStore";

const EMPTY: AuthState = { userSessionToken: null, merchantApiKey: null, merchantOpsToken: null };

describe("createAuthStore", () => {
  describe("get", () => {
    it("starts with every credential null", () => {
      // Arrange
      const store = createAuthStore();

      // Act
      const state = store.get();

      // Assert
      expect(state).toEqual(EMPTY);
    });
  });

  describe("setters", () => {
    it("setUserSession stores the session and leaves the merchant fields null", () => {
      // Arrange
      const store = createAuthStore();

      // Act
      store.setUserSession("session-1");

      // Assert
      expect(store.get()).toEqual({ ...EMPTY, userSessionToken: "session-1" });
    });

    it("setMerchant stores the API key and the ops token and leaves the user session null", () => {
      // Arrange
      const store = createAuthStore();

      // Act
      store.setMerchant("key-1", "ops-1");

      // Assert
      expect(store.get()).toEqual({ ...EMPTY, merchantApiKey: "key-1", merchantOpsToken: "ops-1" });
    });

    it("setMerchantOpsToken replaces the ops token and keeps the API key", () => {
      // Arrange
      const store = createAuthStore();
      store.setMerchant("key-1", "ops-1");

      // Act
      store.setMerchantOpsToken("ops-2");

      // Assert
      expect(store.get()).toEqual({ ...EMPTY, merchantApiKey: "key-1", merchantOpsToken: "ops-2" });
    });

    it("setUserSession keeps an existing merchant login", () => {
      // Arrange
      const store = createAuthStore();
      store.setMerchant("key-1", "ops-1");

      // Act
      store.setUserSession("session-1");

      // Assert
      expect(store.get()).toEqual({
        userSessionToken: "session-1",
        merchantApiKey: "key-1",
        merchantOpsToken: "ops-1",
      });
    });
  });

  describe("clear", () => {
    it("nulls every credential", () => {
      // Arrange
      const store = createAuthStore();
      store.setUserSession("session-1");
      store.setMerchant("key-1", "ops-1");

      // Act
      store.clear();

      // Assert
      expect(store.get()).toEqual(EMPTY);
    });

    it("is harmless on an empty store", () => {
      // Arrange
      const store = createAuthStore();

      // Act
      store.clear();

      // Assert
      expect(store.get()).toEqual(EMPTY);
    });
  });

  describe("isolation", () => {
    it("keeps two stores independent", () => {
      // Arrange
      const first = createAuthStore();
      const second = createAuthStore();

      // Act
      first.setUserSession("session-1");

      // Assert
      expect(first.get()).toEqual({ ...EMPTY, userSessionToken: "session-1" });
      expect(second.get()).toEqual(EMPTY);
    });
  });

  describe("subscribe", () => {
    it("calls the listener with the new state on every change", () => {
      // Arrange
      const store = createAuthStore();
      const listener = vi.fn<(state: AuthState) => void>();
      store.subscribe(listener);

      // Act
      store.setUserSession("session-1");
      store.setMerchant("key-1", "ops-1");
      store.setMerchantOpsToken("ops-2");
      store.clear();

      // Assert
      expect(listener).toHaveBeenCalledTimes(4);
      expect(listener).toHaveBeenNthCalledWith(1, { ...EMPTY, userSessionToken: "session-1" });
      expect(listener).toHaveBeenNthCalledWith(2, {
        userSessionToken: "session-1",
        merchantApiKey: "key-1",
        merchantOpsToken: "ops-1",
      });
      expect(listener).toHaveBeenNthCalledWith(3, {
        userSessionToken: "session-1",
        merchantApiKey: "key-1",
        merchantOpsToken: "ops-2",
      });
      expect(listener).toHaveBeenNthCalledWith(4, EMPTY);
    });

    it("does not call the listener when nothing changed yet", () => {
      // Arrange
      const store = createAuthStore();
      const listener = vi.fn<(state: AuthState) => void>();

      // Act
      store.subscribe(listener);

      // Assert
      expect(listener).toHaveBeenCalledTimes(0);
    });

    it("stops calling the listener after unsubscribe", () => {
      // Arrange
      const store = createAuthStore();
      const listener = vi.fn<(state: AuthState) => void>();
      const unsubscribe = store.subscribe(listener);
      store.setUserSession("session-1");

      // Act
      unsubscribe();
      store.setUserSession("session-2");
      store.clear();

      // Assert
      expect(listener).toHaveBeenCalledTimes(1);
      expect(listener).toHaveBeenCalledWith({ ...EMPTY, userSessionToken: "session-1" });
    });

    it("notifies every subscriber and unsubscribes them independently", () => {
      // Arrange
      const store = createAuthStore();
      const first = vi.fn<(state: AuthState) => void>();
      const second = vi.fn<(state: AuthState) => void>();
      const unsubscribeFirst = store.subscribe(first);
      store.subscribe(second);

      // Act
      store.setUserSession("session-1");
      unsubscribeFirst();
      store.setUserSession("session-2");

      // Assert
      expect(first).toHaveBeenCalledTimes(1);
      expect(second).toHaveBeenCalledTimes(2);
    });
  });

  describe("memory-only guarantee", () => {
    let setItem: MockInstance;
    let cookieSetter: MockInstance;

    beforeEach(() => {
      setItem = vi.spyOn(Storage.prototype, "setItem");
      cookieSetter = vi.spyOn(document, "cookie", "set");
    });

    afterEach(() => {
      window.localStorage.clear();
      window.sessionStorage.clear();
    });

    it("never writes to Storage or document.cookie and never changes the URL", () => {
      // Arrange
      const hrefBefore = window.location.href;
      const store = createAuthStore();
      const listener = vi.fn<(state: AuthState) => void>();
      const unsubscribe = store.subscribe(listener);

      // Act
      store.setUserSession("secret-session-token");
      store.setMerchant("secret-api-key", "secret-ops-token");
      store.setMerchantOpsToken("secret-ops-token-2");
      store.get();
      unsubscribe();
      store.clear();

      // Assert
      expect(setItem).toHaveBeenCalledTimes(0);
      expect(cookieSetter).toHaveBeenCalledTimes(0);
      expect(window.localStorage.length).toBe(0);
      expect(window.sessionStorage.length).toBe(0);
      expect(window.location.href).toBe(hrefBefore);
    });
  });
});
