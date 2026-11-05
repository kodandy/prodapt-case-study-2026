import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { jsonResponse } from "../app/__tests__/harness";
import { App } from "../App";

function mainText(): string {
  return document.querySelector("main")?.textContent ?? "";
}

describe("App areas", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(() => Promise.resolve(jsonResponse(404, { detail: "not found" }))),
    );
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("offers four areas, starting on User", () => {
    // Arrange
    render(<App />);

    // Act
    const names = screen.getAllByRole("button", { pressed: true }).map((button) => button.textContent);

    // Assert
    expect(names).toEqual(["User"]);
    for (const name of ["User", "Merchant", "Dashboard", "Ops console"]) {
      expect(screen.getByRole("button", { name }).textContent).toBe(name);
    }
  });

  it("shows the merchant sign-in when Dashboard is clicked", () => {
    // Arrange
    render(<App />);

    // Act
    fireEvent.click(screen.getByRole("button", { name: "Dashboard" }));

    // Assert
    expect(screen.getByRole("heading", { name: "Merchant sign-in (demo)" }).textContent).toBe("Merchant sign-in (demo)");
    expect(screen.getByRole("button", { name: "Dashboard" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "User" }).getAttribute("aria-pressed")).toBe("false");
    expect(mainText()).not.toMatch(/\bops\b/i);
  });

  it("shows the ops sign-in note when Ops console is clicked", () => {
    // Arrange
    render(<App />);

    // Act
    fireEvent.click(screen.getByRole("button", { name: "Ops console" }));

    // Assert
    expect(screen.getByRole("heading", { name: "Merchant sign-in (demo)" }).textContent).toBe("Merchant sign-in (demo)");
    expect(screen.getByRole("button", { name: "Ops console" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "Dashboard" }).getAttribute("aria-pressed")).toBe("false");
    expect(mainText()).toMatch(/\bops\b/i);
  });

  it("keeps the existing Merchant area reachable", () => {
    // Arrange
    render(<App />);

    // Act
    fireEvent.click(screen.getByRole("button", { name: "Merchant" }));

    // Assert
    expect(screen.getByRole("button", { name: "Merchant" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("heading", { name: "Merchant sign-in (demo)" }).textContent).toBe("Merchant sign-in (demo)");
  });

  it("switches back from Ops console to Dashboard", () => {
    // Arrange
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "Ops console" }));

    // Act
    fireEvent.click(screen.getByRole("button", { name: "Dashboard" }));

    // Assert
    expect(screen.getByRole("button", { name: "Dashboard" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "Ops console" }).getAttribute("aria-pressed")).toBe("false");
    expect(mainText()).not.toMatch(/\bops\b/i);
  });
});
