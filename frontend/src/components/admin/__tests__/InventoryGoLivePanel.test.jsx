// M123-P1 — "Propose approval" must be enabled when the ONLY remaining violation is the
// "no approved inventory configuration event — production fails closed" one (approving IS the fix; the backend's
// approve_current ignores exactly that violation). It used to be disabled on violations.length > 0 → unreachable go-live.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent, cleanup } from "@testing-library/react";

const get = vi.fn();
const post = vi.fn();
vi.mock("@/lib/api", () => ({ default: { get: (...a) => get(...a), post: (...a) => post(...a), delete: vi.fn() }, formatApiError: (e) => String(e) }));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { InventoryGoLivePanel } from "../InventoryGoLivePanel";

const NO_EVENT = "no approved inventory configuration event — production fails closed";
const baseInv = (over = {}) => ({
    ok: false, counts: { configured: 1, live_configured: 0, enabled: 1, live_enabled: 0, bots_enabled: 1, connected: 1, fresh: 1, tradable: 1,
                         bots_configured_raw: 2, bots_null_account: 1 },
    accounts: [], inventory_hash: "abc123", approved_hash: null, structural_defects: [], approval_mode: "single_admin",
    violations: [NO_EVENT], blocking_violations: [], approvable: true, ...over,
});
const basePend = (over = {}) => ({
    expectation: { accounts: 1, enabled: 1, bots: 1, approved_by: "op@x" }, expectation_pending: null, hash_pending: null,
    orphan_bots: [{ id: "b1", user_id: "admin", active: false, symbol: null, name: null }], orphan_total: 1,
    admin_count: 1, me: "op@x", approval_mode: "single_admin", ...over,
});

function mockApi(inv, pend) {
    get.mockImplementation((url) => {
        if (url === "/authority/inventory") return Promise.resolve({ data: inv });
        if (url === "/authority/inventory/pending") return Promise.resolve({ data: pend });
        if (url === "/authority/inventory/policies") return Promise.resolve({ data: { policies: [] } });
        return Promise.reject(new Error("unexpected " + url));
    });
}

beforeEach(() => { get.mockReset(); post.mockReset(); });
afterEach(() => cleanup());

describe("InventoryGoLivePanel · Propose approval", () => {
    it("is ENABLED when expectation is approved and the only violation is the missing approved event", async () => {
        mockApi(baseInv(), basePend());
        render(<InventoryGoLivePanel />);
        const btn = await screen.findByTestId("hash-propose");
        await waitFor(() => expect(btn).not.toBeDisabled());
        expect(screen.getByTestId("inventory-violations").textContent).toContain("(cleared by approval)");
        expect(screen.getByTestId("hash-propose-hint").textContent).toMatch(/records the first configuration event/);
        post.mockResolvedValue({ data: {} });
        fireEvent.click(btn);
        await waitFor(() => expect(post).toHaveBeenCalledWith("/authority/inventory/approve", { note: "go-live approval" }));
    });

    it("stays DISABLED while a real (blocking) violation remains", async () => {
        const real = "enabled bots 0 != expected 1";
        mockApi(baseInv({ violations: [NO_EVENT, real], blocking_violations: [real], approvable: false }), basePend());
        render(<InventoryGoLivePanel />);
        const btn = await screen.findByTestId("hash-propose");
        expect(btn).toBeDisabled();
        expect(screen.getByTestId("hash-propose-hint").textContent).toMatch(/1 left/);
    });

    it("falls back to the same filter as the backend when the API has no approvable field (older backend)", async () => {
        const inv = baseInv(); delete inv.approvable; delete inv.blocking_violations;
        mockApi(inv, basePend());
        render(<InventoryGoLivePanel />);
        const btn = await screen.findByTestId("hash-propose");
        await waitFor(() => expect(btn).not.toBeDisabled());
    });
});

describe("InventoryGoLivePanel · copy and default profile", () => {
    it("single-operator mode never says 'a DIFFERENT admin must approve' for a pending expectation", async () => {
        mockApi(baseInv(), basePend({ expectation_pending: { accounts: 1, enabled: 1, bots: 1, proposed_by: "op@x" } }));
        render(<InventoryGoLivePanel />);
        const line = await screen.findByTestId("expectation-pending");
        expect(line.textContent).toMatch(/fresh step-up \(single-operator mode\)/);
        expect(line.textContent).not.toMatch(/DIFFERENT admin/);
    });

    it("four-eyes mode keeps the second-admin wording", async () => {
        mockApi(baseInv({ approval_mode: "four_eyes" }), basePend({ approval_mode: "four_eyes", admin_count: 2,
                                                                     expectation_pending: { accounts: 1, enabled: 1, bots: 1, proposed_by: "a@x" } }));
        render(<InventoryGoLivePanel />);
        expect((await screen.findByTestId("expectation-pending")).textContent).toMatch(/a DIFFERENT admin must approve/);
    });

    it("shows the inactive seed default profile greyed as harmless, not as a red defect", async () => {
        mockApi(baseInv(), basePend());
        render(<InventoryGoLivePanel />);
        const box = await screen.findByTestId("inventory-default-profiles");
        expect(box.textContent).toMatch(/default profile — inactive, harmless/i);
        expect(screen.queryByTestId("inventory-orphans")).toBeNull();
    });

    it("an ACTIVE account-less bot is still a red defect", async () => {
        mockApi(baseInv(), basePend({ orphan_bots: [{ id: "b2", user_id: "u", active: true, symbol: "XAUUSD" }], orphan_total: 1 }));
        render(<InventoryGoLivePanel />);
        const box = await screen.findByTestId("inventory-orphans");
        expect(box.textContent).toMatch(/delete to clear the defect/i);
        expect(screen.queryByTestId("inventory-default-profiles")).toBeNull();
    });
});
