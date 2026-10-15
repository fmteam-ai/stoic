import { describe, it, expect } from "vitest";
import { diffSavedConfig, fmtValue } from "../configDrift";

describe("diffSavedConfig (v1.60.40 Config Drift Check)", () => {
  it("is empty when the server echoes every field (ignoring number/string and key-order noise)", () => {
    const sent = { risk_level: "medium", max_lot_size: 0.6, symbols: ["XAUUSD"], gold_off_hours_end_utc: "06:00",
      max_spread_pips: { XAUUSD: 5 }, let_winners_run: false, signal_cooldown_minutes: 30 };
    const server = { ...sent, max_lot_size: "0.60", max_spread_pips: { XAUUSD: 5.0 }, extra_server_field: 1 };
    expect(diffSavedConfig(sent, server)).toEqual([]);
  });

  it("flags a field the server dropped (the 15 Oct gold_off_hours bug)", () => {
    const sent = { gold_off_hours_end_utc: "00:30", gold_off_hours_start_utc: "22:00" };
    const server = { risk_level: "medium" };
    const d = diffSavedConfig(sent, server);
    expect(d.map((x) => x.field).sort()).toEqual(["gold_off_hours_end_utc", "gold_off_hours_start_utc"]);
    expect(d[0].server).toBeUndefined();
  });

  it("flags server-side clamps and coercions with both values", () => {
    const d = diffSavedConfig({ signal_cooldown_minutes: 1, symbols: ["xauusd"] },
      { signal_cooldown_minutes: 15, symbols: ["XAUUSD"] });
    expect(d).toEqual([
      { field: "signal_cooldown_minutes", sent: 1, server: 15 },
      { field: "symbols", sent: ["xauusd"], server: ["XAUUSD"] },
    ]);
  });

  it("treats null / undefined / empty string as the same unset value", () => {
    expect(diffSavedConfig({ a: null, b: undefined, c: "" }, { a: undefined, b: "", c: null })).toEqual([]);
  });

  it("formats values for the warning text", () => {
    expect(fmtValue(undefined)).toBe("unset");
    expect(fmtValue(["XAUUSD", "BTCUSD"])).toBe("XAUUSD, BTCUSD");
    expect(fmtValue([])).toBe("none");
    expect(fmtValue(0.6)).toBe("0.6");
    expect(fmtValue({ XAUUSD: 5 })).toBe('{"XAUUSD":5}');
  });
});
