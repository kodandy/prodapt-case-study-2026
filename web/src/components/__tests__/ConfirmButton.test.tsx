import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ConfirmButton } from "../ConfirmButton";

interface Deferred {
  promise: Promise<void>;
  resolve: () => void;
  reject: (reason: Error) => void;
}

function deferred(): Deferred {
  let resolve: () => void = () => undefined;
  let reject: (reason: Error) => void = () => undefined;
  const promise = new Promise<void>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function isDisabled(element: HTMLElement): boolean {
  return element.hasAttribute("disabled");
}

describe("ConfirmButton", () => {
  afterEach(() => {
    cleanup();
  });

  it("renders its children as the button label and starts enabled", () => {
    // Arrange
    const onConfirm = vi.fn<() => Promise<void>>().mockResolvedValue(undefined);

    // Act
    render(<ConfirmButton onConfirm={onConfirm}>Send 10 USD</ConfirmButton>);

    // Assert
    const button = screen.getByRole("button", { name: "Send 10 USD" });
    expect(isDisabled(button)).toBe(false);
    expect(onConfirm).toHaveBeenCalledTimes(0);
  });

  it("calls onConfirm once on a click", async () => {
    // Arrange
    const onConfirm = vi.fn<() => Promise<void>>().mockResolvedValue(undefined);
    render(<ConfirmButton onConfirm={onConfirm}>Confirm</ConfirmButton>);
    const button = screen.getByRole("button", { name: "Confirm" });

    // Act
    await act(async () => {
      fireEvent.click(button);
    });

    // Assert
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("calls onConfirm once and disables the button when clicked twice while pending", async () => {
    // Arrange
    const pending = deferred();
    const onConfirm = vi.fn<() => Promise<void>>().mockReturnValue(pending.promise);
    render(<ConfirmButton onConfirm={onConfirm}>Confirm</ConfirmButton>);
    const button = screen.getByRole("button", { name: "Confirm" });

    // Act
    await act(async () => {
      fireEvent.click(button);
      fireEvent.click(button);
    });

    // Assert
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(isDisabled(button)).toBe(true);

    await act(async () => {
      pending.resolve();
    });
  });

  it("enables the button again after onConfirm resolves", async () => {
    // Arrange
    const pending = deferred();
    const onConfirm = vi.fn<() => Promise<void>>().mockReturnValue(pending.promise);
    render(<ConfirmButton onConfirm={onConfirm}>Confirm</ConfirmButton>);
    const button = screen.getByRole("button", { name: "Confirm" });
    await act(async () => {
      fireEvent.click(button);
    });
    expect(isDisabled(button)).toBe(true);

    // Act
    await act(async () => {
      pending.resolve();
    });

    // Assert
    await waitFor(() => {
      expect(isDisabled(button)).toBe(false);
    });
  });

  it("accepts a second confirmation after the first one resolved", async () => {
    // Arrange
    const onConfirm = vi.fn<() => Promise<void>>().mockResolvedValue(undefined);
    render(<ConfirmButton onConfirm={onConfirm}>Confirm</ConfirmButton>);
    const button = screen.getByRole("button", { name: "Confirm" });
    await act(async () => {
      fireEvent.click(button);
    });
    await waitFor(() => {
      expect(isDisabled(button)).toBe(false);
    });

    // Act
    await act(async () => {
      fireEvent.click(button);
    });

    // Assert
    expect(onConfirm).toHaveBeenCalledTimes(2);
  });

  it("enables the button again after onConfirm rejects", async () => {
    // Arrange
    const pending = deferred();
    const onConfirm = vi.fn<() => Promise<void>>().mockReturnValue(pending.promise);
    render(<ConfirmButton onConfirm={onConfirm}>Confirm</ConfirmButton>);
    const button = screen.getByRole("button", { name: "Confirm" });
    await act(async () => {
      fireEvent.click(button);
    });
    expect(isDisabled(button)).toBe(true);

    // Act
    await act(async () => {
      pending.reject(new Error("boom"));
    });

    // Assert
    await waitFor(() => {
      expect(isDisabled(button)).toBe(false);
    });
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("is disabled by the disabled prop and does not call onConfirm on a click", async () => {
    // Arrange
    const onConfirm = vi.fn<() => Promise<void>>().mockResolvedValue(undefined);
    render(
      <ConfirmButton onConfirm={onConfirm} disabled>
        Confirm
      </ConfirmButton>,
    );
    const button = screen.getByRole("button", { name: "Confirm" });

    // Act
    await act(async () => {
      fireEvent.click(button);
    });

    // Assert
    expect(isDisabled(button)).toBe(true);
    expect(onConfirm).toHaveBeenCalledTimes(0);
  });
});
