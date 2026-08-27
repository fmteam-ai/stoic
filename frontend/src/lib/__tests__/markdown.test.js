import { describe, it, expect } from "vitest";
import { renderMarkdown } from "../markdown";

describe("renderMarkdown", () => {
  it("renders headings", () => {
    const html = renderMarkdown("# Title");
    expect(html).toContain("Title");
    expect(html).toMatch(/<h1/);
  });

  it("renders bold and inline code", () => {
    const html = renderMarkdown("**bold** and `code`");
    expect(html).toContain("<strong");
    expect(html).toContain("<code");
  });

  it("renders unordered lists", () => {
    const html = renderMarkdown("- one\n- two");
    expect(html).toContain("<ul");
    expect(html).toContain("one");
  });

  it("returns empty string for empty input", () => {
    expect(renderMarkdown("")).toBe("");
    expect(renderMarkdown(null)).toBe("");
  });
});
