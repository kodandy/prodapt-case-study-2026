import { describe, expect, it } from "vitest";
import { formatMinor, minorDigits, parseToMinor, type Currency } from "../money";

describe("minorDigits", () => {
  it.each<[Currency, number]>([
    ["JPY", 0],
    ["USD", 2],
    ["EUR", 2],
    ["INR", 2],
  ])("returns the minor digits for %s", (currency, expected) => {
    // Arrange / Act
    const digits = minorDigits(currency);

    // Assert
    expect(digits).toBe(expected);
  });
});

describe("formatMinor", () => {
  it.each<[number, Currency, string]>([
    [123456, "USD", "$1,234.56"],
    [1500, "JPY", "¥1,500"],
    [5, "USD", "$0.05"],
    [0, "USD", "$0.00"],
    [1, "USD", "$0.01"],
    [29, "USD", "$0.29"],
    [1999, "USD", "$19.99"],
    [100, "EUR", "€1.00"],
    [150000, "INR", "₹1,500.00"],
  ])("formats %i %s as %s in en-US", (amountMinor, currency, expected) => {
    // Arrange / Act
    const text = formatMinor(amountMinor, currency);

    // Assert
    expect(text).toBe(expected);
  });

  it("formats with the requested locale", () => {
    // Arrange / Act
    const text = formatMinor(123456, "EUR", "de-DE");

    // Assert
    expect(text).toBe("1.234,56 €");
  });
});

describe("parseToMinor", () => {
  it.each<[string, Currency, number]>([
    ["12.34", "USD", 1234],
    ["12", "USD", 1200],
    ["12.3", "USD", 1230],
    ["12.30", "USD", 1230],
    ["0.05", "USD", 5],
    ["0.29", "USD", 29],
    ["1.15", "USD", 115],
    ["19.99", "USD", 1999],
    ["12.5", "EUR", 1250],
    ["12.5", "INR", 1250],
    [" 7 ", "JPY", 7],
    ["1500", "JPY", 1500],
  ])("parses %j in %s as %i minor units", (input, currency, expected) => {
    // Arrange / Act
    const result = parseToMinor(input, currency);

    // Assert
    expect(result).toEqual({ ok: true, amountMinor: expected });
  });

  it("returns 29 exactly for 0.29 USD (the float trap 0.29 * 100 = 28.999999999999996)", () => {
    // Arrange
    const floatProduct = 0.29 * 100;

    // Act
    const result = parseToMinor("0.29", "USD");

    // Assert
    expect(floatProduct).not.toBe(29);
    expect(result).toEqual({ ok: true, amountMinor: 29 });
  });

  it.each<[string, Currency]>([
    ["1.5", "JPY"],
    ["1.234", "USD"],
    ["1.230", "USD"],
    ["1.005", "EUR"],
  ])("rejects %j in %s with too_many_decimals", (input, currency) => {
    // Arrange / Act
    const result = parseToMinor(input, currency);

    // Assert
    expect(result).toEqual({ ok: false, reason: "too_many_decimals" });
  });

  it.each<[string]>([[""], ["  "], ["\t\n"]])("rejects the blank input %j with empty", (input) => {
    // Arrange / Act
    const result = parseToMinor(input, "USD");

    // Assert
    expect(result).toEqual({ ok: false, reason: "empty" });
  });

  it.each<[string]>([
    ["-1"],
    ["+1"],
    ["1e3"],
    ["1E3"],
    ["1,000"],
    ["abc"],
    [".5"],
    ["1."],
    ["0x10"],
    ["."],
    ["1..2"],
    ["1.2.3"],
    ["1 2"],
    ["１２"],
    ["--1"],
    ["-0"],
  ])("rejects %j with invalid", (input) => {
    // Arrange / Act
    const result = parseToMinor(input, "USD");

    // Assert
    expect(result).toEqual({ ok: false, reason: "invalid" });
  });

  it.each<[string, Currency]>([
    ["0", "USD"],
    ["0.00", "USD"],
    ["0", "JPY"],
  ])("rejects the zero amount %j in %s with not_positive", (input, currency) => {
    // Arrange / Act
    const result = parseToMinor(input, currency);

    // Assert
    expect(result).toEqual({ ok: false, reason: "not_positive" });
  });

  it("accepts exactly Number.MAX_SAFE_INTEGER minor units", () => {
    // Arrange
    const usdInput = "90071992547409.91";

    // Act
    const usd = parseToMinor(usdInput, "USD");
    const jpy = parseToMinor("9007199254740991", "JPY");

    // Assert
    expect(usd).toEqual({ ok: true, amountMinor: Number.MAX_SAFE_INTEGER });
    expect(jpy).toEqual({ ok: true, amountMinor: Number.MAX_SAFE_INTEGER });
  });

  it.each<[string, Currency]>([
    ["90071992547409.92", "USD"],
    ["9007199254740992", "JPY"],
    ["99999999999999999999", "JPY"],
    ["99999999999999999999.99", "USD"],
  ])("rejects %j in %s above Number.MAX_SAFE_INTEGER minor units with invalid", (input, currency) => {
    // Arrange / Act
    const result = parseToMinor(input, currency);

    // Assert
    expect(result).toEqual({ ok: false, reason: "invalid" });
  });
});
