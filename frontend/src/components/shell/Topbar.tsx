import { Mi } from "./Mi";
import type { PageKey } from "./Sidebar";

export function Topbar({ onNavigate, onOpenWatchlist }: {
  onNavigate: (page: PageKey) => void;
  onOpenWatchlist: () => void;
}) {
  return (
    <header className="fixed top-0 left-0 w-full z-50 flex items-center justify-between px-grid-gutter h-12 bg-surface border-b border-surface-border">
      <div className="flex items-center gap-4">
        <span className="font-headline-sm text-headline-sm font-bold text-primary tracking-tight">ALPHA MONITOR</span>
        <nav className="hidden md:flex gap-6 ml-8">
          <button onClick={() => onNavigate("dashboard")} className="text-on-surface-variant hover:text-primary">行情</button>
          <button onClick={() => onNavigate("morning")} className="text-on-surface-variant hover:text-primary">简报</button>
        </nav>
      </div>
      <div className="flex items-center gap-4">
        <button onClick={onOpenWatchlist} className="flex items-center gap-2 text-on-surface-variant hover:text-primary font-mono-label text-mono-label">
          <Mi name="search" size={18} /><span className="hidden sm:inline">管理自选股</span>
        </button>
        <button aria-label="异动日志" onClick={() => onNavigate("alerts")} className="text-on-surface-variant hover:text-primary"><Mi name="notifications" /></button>
        <button aria-label="系统设置" onClick={() => onNavigate("settings")} className="text-on-surface-variant hover:text-primary"><Mi name="settings" /></button>
      </div>
    </header>
  );
}
