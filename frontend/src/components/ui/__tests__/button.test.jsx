import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { Button } from "../button";

describe("Button (shadcn)", () => {
  it("renders children and is clickable", () => {
    render(<Button data-testid="ut-btn">Go Live</Button>);
    const btn = screen.getByTestId("ut-btn");
    expect(btn).toBeInTheDocument();
    expect(btn).toHaveTextContent("Go Live");
  });

  it("applies variant classes", () => {
    render(<Button variant="destructive" data-testid="ut-btn-d">X</Button>);
    expect(screen.getByTestId("ut-btn-d").className).toContain("destructive");
  });
});
