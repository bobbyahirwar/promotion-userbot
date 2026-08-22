import React from "react";
import { CheckCircle2, ShieldAlert, PauseCircle, PlayCircle, Clock } from "lucide-react";
import { PromoStats } from "../types";

interface StatusBannerProps {
  stats: PromoStats;
  onTogglePromo: () => void;
  onClearCooldown?: () => void;
}

export const StatusBanner: React.FC<StatusBannerProps> = ({ stats, onTogglePromo, onClearCooldown }) => {
  const formatTime = (secs: number) => {
    const m = Math.floor(secs / 60);
    const s = Math.floor(secs % 60);
    return `${m}m ${s}s`;
  };

  return (
    <div className="space-y-3">
      {/* Primary Status Card */}
      <div className="p-4 rounded-xl border bg-slate-900/90 border-slate-800 flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div className="flex items-start sm:items-center gap-3.5">
          <div
            className={`w-12 h-12 rounded-xl flex items-center justify-center shrink-0 border ${
              stats.isPromoRunning
                ? stats.cooldownActive
                  ? "bg-amber-500/10 border-amber-500/30 text-amber-400"
                  : "bg-emerald-500/10 border-emerald-500/30 text-emerald-400"
                : "bg-rose-500/10 border-rose-500/30 text-rose-400"
            }`}
          >
            {stats.isPromoRunning ? (
              stats.cooldownActive ? (
                <Clock className="w-6 h-6 animate-pulse" />
              ) : (
                <CheckCircle2 className="w-6 h-6" />
              )
            ) : (
              <PauseCircle className="w-6 h-6" />
            )}
          </div>

          <div>
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold text-slate-100">
                Promotion Loop:
              </span>
              <span
                className={`text-xs px-2.5 py-0.5 rounded-full font-medium ${
                  stats.isPromoRunning
                    ? stats.cooldownActive
                      ? "bg-amber-500/20 text-amber-300 border border-amber-500/40"
                      : "bg-emerald-500/20 text-emerald-300 border border-emerald-500/40"
                    : "bg-slate-800 text-slate-400 border border-slate-700"
                }`}
              >
                {stats.isPromoRunning
                  ? stats.cooldownActive
                    ? "In Cooldown"
                    : "Active & Sending"
                  : "Stopped"}
              </span>
            </div>
            <p className="text-xs text-slate-400 mt-1">
              {stats.isPromoRunning
                ? stats.cooldownActive
                  ? `Global FloodWait Cooldown active. Remaining: ${formatTime(
                      stats.cooldownRemainingSeconds
                    )}. Reason: ${stats.cooldownReason || "FloodWait"}`
                  : `Rotating message #${stats.currentMessageIndex + 1} through ${
                      stats.activeGroupsCount
                    } active groups every ${stats.cycleIntervalSeconds}s.`
                : "Loop is currently halted. Use /startpromo or the button to start dispatching."}
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          {stats.cooldownActive && onClearCooldown && (
            <button
              id="btn-clear-cooldown"
              onClick={onClearCooldown}
              className="px-3 py-2 rounded-lg bg-amber-500/10 hover:bg-amber-500/20 border border-amber-500/40 text-amber-300 text-xs font-medium transition cursor-pointer"
            >
              Force Reset Cooldown
            </button>
          )}

          <button
            id="btn-toggle-promo"
            onClick={onTogglePromo}
            className={`flex items-center gap-2 px-4 py-2 rounded-lg text-xs font-medium transition cursor-pointer ${
              stats.isPromoRunning
                ? "bg-rose-500/20 hover:bg-rose-500/30 border border-rose-500/40 text-rose-300"
                : "bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-semibold"
            }`}
          >
            {stats.isPromoRunning ? (
              <>
                <PauseCircle className="w-4 h-4" />
                <span>Stop Promotion (/stoppromo)</span>
              </>
            ) : (
              <>
                <PlayCircle className="w-4 h-4" />
                <span>Start Promotion (/startpromo)</span>
              </>
            )}
          </button>
        </div>
      </div>

      {/* Safety Circuit Breaker Alert if triggered */}
      {stats.safetyPaused && (
        <div className="p-3.5 rounded-xl border bg-rose-950/40 border-rose-800/60 text-rose-200 flex items-center justify-between text-xs">
          <div className="flex items-center gap-2.5">
            <ShieldAlert className="w-4 h-4 text-rose-400 shrink-0" />
            <span>
              <strong>Safety Circuit Breaker Active:</strong> High failure rate detected in previous cycle. Promotion is temporarily paused to protect userbot account.
            </span>
          </div>
        </div>
      )}
    </div>
  );
};
