import { useState, useEffect } from "react";
import { Header } from "./components/Header";
import { StatusBanner } from "./components/StatusBanner";
import { StatsCards } from "./components/StatsCards";
import { GroupsList } from "./components/GroupsList";
import { MessagesList } from "./components/MessagesList";
import { LogsViewer } from "./components/LogsViewer";
import { BotCommandsGuide } from "./components/BotCommandsGuide";
import { PromoStats, TargetGroup, PromoMessage, CycleLog } from "./types";

export function App() {
  const [isRefreshing, setIsRefreshing] = useState(false);

  const [stats, setStats] = useState<PromoStats>({
    totalSent: 142,
    totalSuccess: 138,
    totalFailed: 4,
    activeGroupsCount: 18,
    totalGroupsCount: 22,
    messagesCount: 3,
    currentMessageIndex: 0,
    cycleIntervalSeconds: 300,
    rateLimitRange: [15.0, 30.0],
    consecutiveErrors: 0,
    isPromoRunning: true,
    cooldownActive: false,
    cooldownRemainingSeconds: 0,
    cooldownReason: null,
    safetyPaused: false,
    lastCycleDurationSeconds: 145.4,
    uptimeSeconds: 7320,
  });

  const [groups, setGroups] = useState<TargetGroup[]>([
    { id: "1", chatId: -1001524389101, title: "Crypto Traders Global", username: "cryptotraders_global", membersCount: 14200, status: "active", lastSentAt: "2 mins ago" },
    { id: "2", chatId: -1001893420192, title: "DeFi & Web3 Community", username: "defi_hub_official", membersCount: 8900, status: "active", lastSentAt: "3 mins ago" },
    { id: "3", chatId: -1001920391290, title: "Tech Startups & Founders", username: "tech_founders_chat", membersCount: 21500, status: "active", lastSentAt: "4 mins ago" },
    { id: "4", chatId: -1001784910293, title: "Forex Signals & Analysis", username: "forex_signals_pro", membersCount: 6500, status: "active", lastSentAt: "5 mins ago" },
    { id: "5", chatId: -1001648291039, title: "Software Engineers Hub", username: "swe_developers", membersCount: 32000, status: "active", lastSentAt: "6 mins ago" },
    { id: "6", chatId: -1001392019482, title: "Private VIP Channel", status: "forbidden", lastError: "ChatWriteForbidden" },
    { id: "7", chatId: -1001192839102, title: "Archived Ads Group", status: "blacklisted" },
    { id: "8", chatId: -1001994829102, title: "Alpha Calls Community", username: "alphacalls_tg", membersCount: 4300, status: "active", lastSentAt: "7 mins ago" },
  ]);

  const [messages] = useState<PromoMessage[]>([
    {
      id: "m1",
      type: "text",
      text: "🚀 Looking for high-yield trading bots and signals? Join our official community and get real-time alpha calls! Link: https://t.me/alphapromochannel",
      addedAt: "2026-08-20 14:30",
    },
    {
      id: "m2",
      type: "photo",
      caption: "✨ Automated Multi-Exchange Trading Engine is now live. Check out the latest weekly profits breakdown! 📈",
      mediaFileId: "AgACAgIAAxkBAAI...photo_01",
      addedAt: "2026-08-21 09:15",
    },
    {
      id: "m3",
      type: "text",
      text: "🔥 Exclusive early access whitelist open for our Web3 AI suite. Limited spots available this week!",
      addedAt: "2026-08-22 01:00",
    },
  ]);

  const [logs, setLogs] = useState<CycleLog[]>([
    { id: "l1", timestamp: "12:45:10", cycleId: "cycle_1755866710", targetChat: "Crypto Traders Global", chatId: -1001524389101, status: "success", details: "Sent text message #1 (Delay: 18.2s)", messageType: "text" },
    { id: "l2", timestamp: "12:45:30", cycleId: "cycle_1755866710", targetChat: "DeFi & Web3 Community", chatId: -1001893420192, status: "success", details: "Sent text message #1 (Delay: 20.4s)", messageType: "text" },
    { id: "l3", timestamp: "12:45:52", cycleId: "cycle_1755866710", targetChat: "Private VIP Channel", chatId: -1001392019482, status: "failed", details: "ChatWriteForbidden: Cannot send to this chat. Auto-deactivated group.", messageType: "text" },
    { id: "l4", timestamp: "12:46:12", cycleId: "cycle_1755866710", targetChat: "Tech Startups & Founders", chatId: -1001920391290, status: "success", details: "Sent text message #1 (Delay: 19.8s)", messageType: "text" },
    { id: "l5", timestamp: "12:46:33", cycleId: "cycle_1755866710", targetChat: "Forex Signals & Analysis", chatId: -1001784910293, status: "success", details: "Sent text message #1 (Delay: 21.0s)", messageType: "text" },
    { id: "l6", timestamp: "12:46:55", cycleId: "cycle_1755866710", targetChat: "Software Engineers Hub", chatId: -1001648291039, status: "success", details: "Sent text message #1 (Delay: 22.1s)", messageType: "text" },
  ]);

  // Uptime tick
  useEffect(() => {
    const timer = setInterval(() => {
      setStats((prev) => ({
        ...prev,
        uptimeSeconds: prev.uptimeSeconds + 1,
        cooldownRemainingSeconds: Math.max(0, prev.cooldownRemainingSeconds - 1),
        cooldownActive: prev.cooldownRemainingSeconds - 1 > 0,
      }));
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  const handleRefresh = () => {
    setIsRefreshing(true);
    setTimeout(() => {
      setIsRefreshing(false);
    }, 600);
  };

  const handleTogglePromo = () => {
    setStats((prev) => ({
      ...prev,
      isPromoRunning: !prev.isPromoRunning,
    }));
  };

  const handleClearCooldown = () => {
    setStats((prev) => ({
      ...prev,
      cooldownActive: false,
      cooldownRemainingSeconds: 0,
      cooldownReason: null,
    }));
  };

  const handleToggleGroupStatus = (id: string) => {
    setGroups((prev) =>
      prev.map((g) => {
        if (g.id === id) {
          const nextStatus = g.status === "active" ? "blacklisted" : "active";
          return { ...g, status: nextStatus };
        }
        return g;
      })
    );
  };

  const handleClearLogs = () => {
    setLogs([]);
  };

  return (
    <div className="min-h-screen bg-[#0b0f17] text-slate-100 flex flex-col font-sans selection:bg-sky-500/30 selection:text-sky-200">
      <Header stats={stats} onRefresh={handleRefresh} isRefreshing={isRefreshing} />

      <main className="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-6 space-y-6">
        <StatusBanner
          stats={stats}
          onTogglePromo={handleTogglePromo}
          onClearCooldown={handleClearCooldown}
        />

        <StatsCards stats={stats} />

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <GroupsList groups={groups} onToggleStatus={handleToggleGroupStatus} />
          <MessagesList messages={messages} currentIndex={stats.currentMessageIndex} />
        </div>

        <LogsViewer logs={logs} onClearLogs={handleClearLogs} />

        <BotCommandsGuide />
      </main>

      <footer className="border-t border-slate-900 bg-slate-950/60 py-4 text-center text-xs text-slate-600">
        Telegram Promotion Userbot • MTProto Dispatch Engine & Health Monitor
      </footer>
    </div>
  );
}

export default App;
