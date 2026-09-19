import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StockDetail } from "./StockDetail";

vi.mock("../components/charts/KlineChart", () => ({ KlineChart: () => <div /> }));
afterEach(() => vi.unstubAllGlobals());

describe("manual explanation", () => {
  it("requests a summary instead of inventing a limit-down event", async () => {
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => new Response(
      JSON.stringify(init?.method === "POST"
        ? { text: "今日表现综述", source: "template", partial: true, generated_at: "2026-09-18" }
        : []), { headers: { "content-type": "application/json" } },
    ));
    vi.stubGlobal("fetch", fetchMock);
    render(<StockDetail code="600519" row={{ code: "600519", name: "贵州茅台", price: 1257.12,
      change_pct: -0.78, volume_ratio: 1.14, volume: 100, is_holding: false, group_id: null,
      display_order: 0, updated_at: null, status: "normal" }} />);
    await userEvent.click(screen.getByRole("button", { name: "为什么" }));
    await waitFor(() => expect(screen.getByText("规则模板（未调用云端 AI）")).toBeInTheDocument());
    const request = fetchMock.mock.calls.find(([url]) => url === "/explain");
    expect(JSON.parse(String(request?.[1]?.body))).toMatchObject({ anomaly_type: "summary", on_demand: true });
  });
});
