import React from "react";
import { RefreshCw, Radio, Server } from "lucide-react";
import { PromoStats } from "../types";

interface HeaderProps {
  stats: PromoStats;
  onRefresh: () => void;
  isRefreshing: boolean;
}

export const Header: React.FC<HeaderProps> = ({ stats, onRefresh, isRefreshing }) => {
  const formatUptime = (seconds: number) => {
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (d > 0) return `${d}d ${h}h ${m}m`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m ${seconds % 60}s`;
  };

  return (
    <header className="border-b border-slate-800 bg-slate-900/80 backdrop-blur sticky top-0 z-20">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-3.5 flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-sky-500/10 border border-sky-500/30 flex items-center justify-center text-sky-400">
            <Radio className="w-5 h-5 animate-pulse" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-lg font-semibold text-slate-100 tracking-tight">
                Telegram Promo Userbot
              </h1>
              <span className="text-xs px-2 py-0.5 rounded-full font-medium border bg-emerald-500/10 border-emerald-500/30 text-emerald-400 flex items-center gap-1">
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-ping"></span>
                v2.0 Fixed
              </span>
            </div>
            <p className="text-xs text-slate-400 flex items-center gap-2">
              <span>Dual Pyrogram Client (Bot + Userbot)</span>
              <span className="text-slate-600">•</span>
              <span className="text-slate-400">Uptime: {formatUptime(stats.uptimeSeconds)}</span>
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2.5">
          <div className="hidden sm:flex items-center gap-2 px-3 py-1.5 rounded-lg bg-slate-800/80 border border-slate-700/60 text-xs text-slate-300">
            <Server className="w-3.5 h-3.5 text-sky-400" />
            <span>Port 8080 (Health/Keepalive OK)</span>
          </div>

          <button
            id="btn-refresh-state"
            onClick={onRefresh}
            disabled={isRefreshing}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 active:bg-slate-600 border border-slate-700 text-slate-200 text-xs font-medium transition cursor-pointer disabled:opacity-50"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${isRefreshing ? "animate-spin text-sky-400" : ""}`} />
            <span>Sync</span>
          </button>
        </div>
      </div>
    </header>
  );
};
