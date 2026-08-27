import { describe, it, expect } from "vitest";
import { cn } from "../utils";

describe("cn (class merge)", () => {
  it("merges conditional classes", () => {
    const cond = [1].length === 0;
    expect(cn("a", cond && "b", "c")).toBe("a c");
  });

  it("resolves tailwind conflicts (last wins)", () => {
    expect(cn("p-2", "p-4")).toBe("p-4");
  });

  it("handles arrays and objects", () => {
    expect(cn(["x", { y: true, z: false }])).toBe("x y");
  });
});
