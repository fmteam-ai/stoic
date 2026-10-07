import { test, expect, Page } from "@playwright/test";

// A15-6 / A16-6 — chart provenance end-to-end: the strip must say exactly what the data is.
// The authenticated Analytics page renders `provenance` from /api/analytics/attribution through
// the shared <ChartProvenance> contract. We replay the real response with the provenance swapped
// for each truth case: fresh broker-reconciled, stale derived, missing, conflicting (invalid
// contract) and partial (gaps + cache fallback). Nothing may be hidden or shown as better than it is.

const base = (overrides: Record<string, unknown>) => ({
  contract_version: 1,
  provider: "mt5_bridge",
  source_kind: "broker_reconciled",
  as_of: new Date().toISOString(),
  timezone: "UTC",
  freshness_s: 20,
  stale: false,
  points: 120,
  missing_intervals_count: 0,
  cache_status: "live",
  environment: "demo",
  reconciliation_id: "rec_fresh_1",
  ...overrides,
});

async function withProvenance(page: Page, prov: unknown) {
  await page.route("**/api/analytics/attribution*", async (route) => {
    const res = await route.fetch();
    let body: Record<string, unknown> = {};
    try { body = await res.json(); } catch { body = {}; }
    body.provenance = prov;
    await route.fulfill({ response: res, status: 200, headers: { ...res.headers(), "content-type": "application/json" }, body: JSON.stringify(body) });
  });
  // the app shell lazy-loads page chunks; on a slow dev server the "taking longer than usual" fallback
  // may show first — retry the navigation instead of failing on infrastructure
  for (let attempt = 0; attempt < 3; attempt++) {
    await page.goto("/analytics");
    try {
      await expect(page.getByTestId("analytics-header")).toBeVisible({ timeout: 20_000 });
      return;
    } catch {
      const retry = page.getByRole("button", { name: "RETRY" });
      if (await retry.count()) await retry.first().click();
    }
  }
  await expect(page.getByTestId("analytics-header")).toBeVisible({ timeout: 30_000 });
}

test.describe("chart provenance (A16-6)", () => {
  test("fresh broker-reconciled data is labelled BROKER-RECONCILED and not stale", async ({ page }) => {
    await withProvenance(page, base({}));
    const kind = page.getByTestId("analytics-provenance-kind");
    await expect(kind).toBeVisible({ timeout: 30_000 });
    await expect(kind).toContainText("BROKER-RECONCILED");
    await expect(page.getByTestId("analytics-provenance-freshness")).not.toContainText("STALE");
    await expect(page.getByTestId("analytics-provenance-gaps")).toHaveCount(0);
    await expect(page.getByTestId("analytics-provenance-missing")).toHaveCount(0);
  });

  test("stale derived (ledger) data is labelled DERIVED and STALE", async ({ page }) => {
    await withProvenance(page, base({ source_kind: "derived", provider: "stoic_ledger", freshness_s: 7200, stale: true, reconciliation_id: null, ledger_id: "ldg_42" }));
    await expect(page.getByTestId("analytics-provenance-kind")).toContainText("DERIVED", { timeout: 30_000 });
    await expect(page.getByTestId("analytics-provenance-kind")).not.toContainText("BROKER");
    await expect(page.getByTestId("analytics-provenance-freshness")).toContainText("STALE");
  });

  test("missing provenance is never hidden", async ({ page }) => {
    await withProvenance(page, null);
    await expect(page.getByTestId("analytics-provenance-missing")).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId("analytics-provenance-missing")).toContainText("PROVENANCE MISSING");
    await expect(page.getByTestId("analytics-provenance-kind")).toHaveCount(0);
  });

  test("conflicting / unknown contract is treated as missing, not as reconciled", async ({ page }) => {
    await withProvenance(page, base({ contract_version: 2, source_kind: "broker_reconciled" }));
    await expect(page.getByTestId("analytics-provenance-missing")).toBeVisible({ timeout: 30_000 });
    await withProvenance(page, base({ source_kind: "verified_by_vendor" }));
    await expect(page.getByTestId("analytics-provenance-missing")).toBeVisible({ timeout: 30_000 });
  });

  test("partial data shows the gaps and the cache fallback", async ({ page }) => {
    await withProvenance(page, base({ source_kind: "indicative", missing_intervals_count: 3, cache_status: "stale_cache", freshness_s: 900, stale: true }));
    await expect(page.getByTestId("analytics-provenance-kind")).toContainText("INDICATIVE", { timeout: 30_000 });
    await expect(page.getByTestId("analytics-provenance-gaps")).toContainText("3 gaps");
    await expect(page.getByTestId("analytics-provenance-fallback")).toContainText("STALE_CACHE");
    await expect(page.getByTestId("analytics-provenance-freshness")).toContainText("STALE");
  });

  test("simulated research series can never pass as broker data", async ({ page }) => {
    await withProvenance(page, base({ source_kind: "simulated", provider: "stoic_research", environment: "research" }));
    await expect(page.getByTestId("analytics-provenance-kind")).toContainText("SIMULATED", { timeout: 30_000 });
    await expect(page.getByTestId("analytics-provenance-kind")).not.toContainText("BROKER");
  });
});
