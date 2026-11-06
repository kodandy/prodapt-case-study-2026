import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const CSS = readFileSync(join(process.cwd(), "src", "styles.css"), "utf8");

function ruleBody(selector: string): string {
  const start = CSS.indexOf(`${selector} {`);
  if (start < 0) return "";
  return CSS.slice(start, CSS.indexOf("}", start) + 1);
}

describe("styles at narrow widths", () => {
  it("lets grid columns shrink below 360px so a 375px screen does not overflow", () => {
    // Arrange
    const columns = ruleBody(".columns");

    // Act
    const usesFixedMinimum = /minmax\(\s*360px/.test(CSS);

    // Assert
    expect(columns).toContain("minmax(min(100%, 360px), 1fr)");
    expect(usesFixedMinimum).toBe(false);
  });

  it("scrolls wide tables horizontally inside their container", () => {
    // Arrange
    const tableScroll = ruleBody(".table-scroll");

    // Act
    const scrolls = tableScroll.includes("overflow-x: auto");

    // Assert
    expect(scrolls).toBe(true);
  });
});
