/**
 * DESIGN.md ref:
 *   §Components — Inputs (prominent 1px border)
 *   §Shapes — rounded-md (4px) for inputs
 *
 * Reference HTML: dashboard_a_ai/code.html "Search to Add"
 *   — input + dropdown of {code, name} candidates, click to add.
 */
import { useEffect, useRef, useState } from "react";
import { sdk } from "../../services/sdk";
import type { StockBasic } from "../../services/sdk";

interface Props {
  onPick: (s: StockBasic) => void;
  disabled?: boolean;
  placeholderHint?: string;
}

export function StockSearchInput({ onPick, disabled, placeholderHint }: Props) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<StockBasic[]>([]);
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [attempt, setAttempt] = useState(0);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const trimmed = q.trim();
    if (!trimmed) {
      setResults([]);
      setStatus("idle");
      setOpen(false);
      return;
    }
    setResults([]);
    setStatus("loading");
    setOpen(true);
    let cancelled = false;
    const handle = setTimeout(() => {
      sdk
        .search(trimmed)
        .then((r) => {
          if (!cancelled) {
            setResults(r.slice(0, 8));
            setStatus("ready");
          }
        })
        .catch(() => {
          if (!cancelled) setStatus("error");
        });
    }, 150);
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [q, attempt]);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  return (
    <div ref={ref} className="relative">
      <input
        aria-label="搜索股票"
        type="text"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onFocus={() => q.trim() && setOpen(true)}
        disabled={disabled}
        placeholder={placeholderHint ?? "代码 / 名称 / 拼音首字母"}
        className="w-full rounded-md border border-[color:var(--color-surface-1-border)] bg-[color:var(--color-surface-1)] px-3 py-1.5 text-[length:var(--text-table-data)] text-[color:var(--color-foreground)] placeholder:text-[color:var(--color-foreground-subtle)] focus:border-[color:var(--color-primary)] focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
      />
      {open && status !== "idle" && results.length === 0 && (
        <div className="absolute left-0 right-0 top-full z-10 mt-1 rounded-md border border-[color:var(--color-surface-1-border)] bg-[color:var(--color-surface-1)] p-3 text-sm">
          <p role={status === "error" ? "alert" : "status"}>
            {status === "loading" ? "正在搜索…" : status === "error"
              ? "搜索失败，请重试。" : "未找到匹配股票，请检查代码或名称。"}
          </p>
          {status === "error" && (
            <button type="button" className="mt-2 underline" onClick={() => setAttempt((n) => n + 1)}>
              重新搜索
            </button>
          )}
        </div>
      )}
      {open && results.length > 0 && (
        <ul
          role="listbox"
          className="absolute left-0 right-0 top-full z-10 mt-1 max-h-72 overflow-auto rounded-md border border-[color:var(--color-surface-1-border)] bg-[color:var(--color-surface-1)] shadow-lg"
        >
          {results.map((r) => (
            <li key={r.code}>
              <button
                type="button"
                onClick={() => {
                  onPick(r);
                  setQ("");
                  setOpen(false);
                }}
                className="flex w-full items-center gap-3 px-3 py-1.5 text-left hover:bg-[color:var(--color-surface-2)]"
              >
                <span className="font-mono text-[length:var(--text-mono-label)] text-[color:var(--color-foreground-muted)]">
                  {r.code}
                </span>
                <span className="text-[length:var(--text-table-data)] text-[color:var(--color-foreground)]">
                  {r.name}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
