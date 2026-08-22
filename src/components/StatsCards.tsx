import React from "react";
import { Send, CheckCircle, XCircle, Users, MessageSquare, Timer } from "lucide-react";
import { PromoStats } from "../types";

interface StatsCardsProps {
  stats: PromoStats;
}

export const StatsCards: React.FC<StatsCardsProps> = ({ stats }) => {
  const successRate =
    stats.totalSent > 0 ? Math.round((stats.totalSuccess / stats.totalSent) * 100) : 100;

  const cards = [
    {
      title: "Total Messages Dispatched",
      value: stats.totalSent.toLocaleString(),
      subtext: `${successRate}% delivery rate`,
      icon: Send,
      color: "sky",
    },
    {
      title: "Successful Deliveries",
      value: stats.totalSuccess.toLocaleString(),
      subtext: "Without MTProto error",
      icon: CheckCircle,
      color: "emerald",
    },
    {
      title: "Failed / Skipped",
      value: stats.totalFailed.toLocaleString(),
      subtext: `${stats.consecutiveErrors} consecutive errs`,
      icon: XCircle,
      color: stats.totalFailed > 0 ? "rose" : "slate",
    },
    {
      title: "Target Active Groups",
      value: `${stats.activeGroupsCount} / ${stats.totalGroupsCount}`,
      subtext: "Blacklist & Inactives filtered",
      icon: Users,
      color: "indigo",
    },
    {
      title: "Message Rotation Queue",
      value: `${stats.messagesCount} templates`,
      subtext: `Index #${stats.currentMessageIndex + 1} next`,
      icon: MessageSquare,
      color: "purple",
    },
    {
      title: "Cadence & Delays",
      value: `${stats.cycleIntervalSeconds / 60}m cycle`,
      subtext: `${stats.rateLimitRange[0]}-${stats.rateLimitRange[1]}s per group`,
      icon: Timer,
      color: "amber",
    },
  ];

  return (
    <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
      {cards.map((c, i) => {
        const Icon = c.icon;
        return (
          <div
            key={i}
            className="p-3.5 rounded-xl border bg-slate-900/60 border-slate-800 flex flex-col justify-between"
          >
            <div className="flex items-center justify-between text-slate-400 mb-2">
              <span className="text-xs font-medium truncate">{c.title}</span>
              <Icon className="w-3.5 h-3.5 text-slate-500 shrink-0" />
            </div>
            <div>
              <div className="text-lg font-bold text-slate-100">{c.value}</div>
              <div className="text-[11px] text-slate-500 mt-0.5 truncate">{c.subtext}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
};
