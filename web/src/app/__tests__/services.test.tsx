import { cleanup, render } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createApiClient } from "../../lib/api";
import { createAuthStore } from "../../lib/authStore";
import { ServicesProvider, useServices, type AppServices } from "../services";

const BODY = { recipient_email: "bob@example.com", amount_minor: 1250, currency: "USD" };

function createServices(): AppServices {
  const store = createAuthStore();
  const client = createApiClient({ fetch: vi.fn<typeof fetch>(() => Promise.reject(new Error("unused"))), store });
  return { store, client };
}

type PendingWrites = ReturnType<typeof useServices>["pendingWrites"];

function Probe(props: { seen: PendingWrites[] }): ReactNode {
  props.seen.push(useServices().pendingWrites);
  return null;
}

describe("ServicesProvider pendingWrites", () => {
  afterEach(() => {
    cleanup();
  });

  it("exposes a pendingWrites that returns one key for the same scope and body", () => {
    // Arrange
    const seen: PendingWrites[] = [];
    render(
      <ServicesProvider services={createServices()}>
        <Probe seen={seen} />
      </ServicesProvider>,
    );
    const pendingWrites = seen[0];

    // Act
    const first = pendingWrites?.keyFor("user:transfer", BODY);
    const second = pendingWrites?.keyFor("user:transfer", BODY);

    // Assert
    expect(second).toBe(first);
    expect(first).not.toBe("");
    expect(typeof first).toBe("string");
  });

  it("keeps the same pendingWrites instance across re-renders of one provider", () => {
    // Arrange
    const services = createServices();
    const seen: PendingWrites[] = [];
    const tree = (
      <ServicesProvider services={services}>
        <Probe seen={seen} />
      </ServicesProvider>
    );
    const { rerender } = render(tree);

    // Act
    rerender(
      <ServicesProvider services={services}>
        <Probe seen={seen} />
      </ServicesProvider>,
    );

    // Assert
    expect(typeof seen[0]?.keyFor).toBe("function");
    expect(seen.length).toBeGreaterThan(1);
    for (const instance of seen) expect(instance).toBe(seen[0]);
  });

  it("creates a separate pendingWrites instance per provider", () => {
    // Arrange
    const seenA: PendingWrites[] = [];
    const seenB: PendingWrites[] = [];

    // Act
    render(
      <>
        <ServicesProvider services={createServices()}>
          <Probe seen={seenA} />
        </ServicesProvider>
        <ServicesProvider services={createServices()}>
          <Probe seen={seenB} />
        </ServicesProvider>
      </>,
    );

    // Assert
    expect(seenA[0]).not.toBe(seenB[0]);
    expect(seenA[0]?.keyFor("user:transfer", BODY)).not.toBe(seenB[0]?.keyFor("user:transfer", BODY));
  });

  it("is created from the provider's store, so signing out drops its user entries", () => {
    // Arrange
    const services = createServices();
    services.store.setUserSession("user-token");
    const seen: PendingWrites[] = [];
    render(
      <ServicesProvider services={services}>
        <Probe seen={seen} />
      </ServicesProvider>,
    );
    const before = seen[0]?.keyFor("user:transfer", BODY);

    // Act
    services.store.clear();

    // Assert
    const after = seen[0]?.keyFor("user:transfer", BODY);
    expect(after).not.toBe(before);
    expect(after).not.toBe("");
  });

  it("is created from the provider's store, so merchant sign-out drops its merchant entries", () => {
    // Arrange
    const services = createServices();
    services.store.setMerchant("merchant-key", "ops-token");
    const seen: PendingWrites[] = [];
    render(
      <ServicesProvider services={services}>
        <Probe seen={seen} />
      </ServicesProvider>,
    );
    const before = seen[0]?.keyFor("merchant:refund:pay_1", { amount_minor: 4000 });

    // Act
    services.store.clear();

    // Assert
    const after = seen[0]?.keyFor("merchant:refund:pay_1", { amount_minor: 4000 });
    expect(after).not.toBe(before);
    expect(after).not.toBe("");
  });
});
