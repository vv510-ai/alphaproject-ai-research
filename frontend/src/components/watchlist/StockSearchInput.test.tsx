import { afterEach, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { StockSearchInput } from "./StockSearchInput";
import { sdk } from "../../services/sdk";

afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

async function search(value: string) {
  fireEvent.change(screen.getByLabelText("搜索股票"), { target: { value } });
  await act(async () => { await vi.advanceTimersByTimeAsync(150); });
}

it("distinguishes empty results from failure and allows retry", async () => {
  vi.useFakeTimers();
  const lookup = vi.spyOn(sdk, "search").mockResolvedValueOnce([])
    .mockRejectedValueOnce(new Error("offline"))
    .mockResolvedValueOnce([{ code: "600519", name: "贵州茅台" }]);
  const pick = vi.fn();
  render(<StockSearchInput onPick={pick} />);
  await search("不存在的股票");
  expect(screen.getByRole("status")).toHaveTextContent("未找到匹配股票");
  await search("贵州茅台");
  expect(screen.getByRole("alert")).toHaveTextContent("搜索失败");
  fireEvent.click(screen.getByRole("button", { name: "重新搜索" }));
  expect(screen.getByRole("status")).toHaveTextContent("正在搜索");
  await act(async () => { await vi.advanceTimersByTimeAsync(150); });
  fireEvent.click(screen.getByRole("button", { name: /600519 贵州茅台/ }));
  expect(pick).toHaveBeenCalledWith({ code: "600519", name: "贵州茅台" });
  expect(lookup).toHaveBeenLastCalledWith("贵州茅台");
  expect(screen.getByLabelText("搜索股票")).toHaveValue("");
});

it("removes old candidates immediately and ignores late responses", async () => {
  vi.useFakeTimers();
  let resolveOld!: (rows: { code: string; name: string }[]) => void;
  vi.spyOn(sdk, "search").mockResolvedValueOnce([{ code: "600519", name: "贵州茅台" }])
    .mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }))
    .mockResolvedValueOnce([{ code: "000001", name: "平安银行" }]);
  render(<StockSearchInput onPick={vi.fn()} />);
  await search("茅台");
  expect(screen.getByText("贵州茅台")).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("搜索股票"), { target: { value: "旧查询" } });
  expect(screen.queryByText("贵州茅台")).not.toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(150); });
  await search("平安");
  await act(async () => { resolveOld([{ code: "600519", name: "贵州茅台" }]); });
  expect(screen.getByText("平安银行")).toBeInTheDocument();
  expect(screen.queryByText("贵州茅台")).not.toBeInTheDocument();
  await search("");
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});
