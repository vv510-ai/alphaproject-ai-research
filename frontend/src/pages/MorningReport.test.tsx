import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MorningReport } from "./MorningReport";

afterEach(() => vi.unstubAllGlobals());
describe("briefing backend contract", () => {
  it("renders canonical backend blocks rather than waiting for nonexistent summary fields", async () => {
    const report = { on_date: "2026-09-18", version: "raw", push_status: "local_only",
      created_at: "2026-09-18T10:00:00", content_json: JSON.stringify({
        body_text: "自选 1 只。", market_overview: { data: { yesterday: { "上证指数": 3911.87 },
          global: [{ name: "标普500", change_pct: -0.9 }], sectors: [{ name: "银行Ⅱ", change_pct: 0.2, as_of: "2026-09-18", source: "财联社（日线）" }] } },
        watchlist: { data: [{ code: "600519", name: "贵州茅台" }] },
        news: { data: [{ title: "测试新闻标题" }] }, calendar: { data: {
          earnings: [{ code: "000655", event: "金岭矿业 财报披露", date: "2026-10-14" }],
          econ: [], availability: { earnings: "ready", econ: "missing" },
        } },
      }) };
    vi.stubGlobal("fetch", vi.fn(async (url: string) => new Response(JSON.stringify(
      url === "/briefing/history" ? [report] : report,
    ), { headers: { "content-type": "application/json" } })));
    render(<MorningReport />);
    expect(await screen.findByText(/上证指数：3911.87/)).toBeInTheDocument();
    expect(await screen.findByText(/外盘 标普500：-0.9%/)).toBeInTheDocument();
    expect(await screen.findByText(/板块 银行Ⅱ：0.2%/)).toBeInTheDocument();
    expect(await screen.findByText(/2026-09-18.*财联社（日线）/)).toBeInTheDocument();
    expect(await screen.findByText(/金岭矿业 财报披露/)).toBeInTheDocument();
    expect(await screen.findByText(/经济日历暂时获取失败/)).toBeInTheDocument();
    expect(await screen.findByText("贵州茅台（600519）")).toBeInTheDocument();
    expect(await screen.findByText("• 测试新闻标题")).toBeInTheDocument();
  });
});
