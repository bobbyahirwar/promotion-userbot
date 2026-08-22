import React from "react";
import { BookOpen, Command } from "lucide-react";

export const BotCommandsGuide: React.FC = () => {
  const commands = [
    { cmd: "/startpromo", desc: "Starts the automated promo loop. Auto-rotates messages every 5 mins." },
    { cmd: "/stoppromo", desc: "Gracefully stops the active promotion task." },
    { cmd: "/health", desc: "Shows loop state, MongoDB connection, cooldown remaining & userbot status." },
    { cmd: "/syncgroups", desc: "Scans userbot joined chats and syncs target groups into MongoDB." },
    { cmd: "/groups", desc: "List, add, or remove target promotion groups & channels." },
    { cmd: "/msg", desc: "View, add (/addmsg), or delete promotional message templates (text/photo/video)." },
    { cmd: "/blacklist", desc: "Exclude specific groups/channels from receiving promotional messages." },
    { cmd: "/inactive", desc: "Manage auto-deactivated groups (e.g. ChatWriteForbidden, Slowmode)." },
    { cmd: "/stats", desc: "View full delivery counts, success rates, and cycle duration metrics." },
  ];

  return (
    <div className="border border-slate-800 bg-slate-900/60 rounded-xl p-4">
      <div className="flex items-center gap-2 mb-3">
        <BookOpen className="w-4 h-4 text-sky-400" />
        <h2 className="text-sm font-semibold text-slate-100">Telegram Bot Command Reference</h2>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-2.5">
        {commands.map((item, idx) => (
          <div key={idx} className="p-2.5 rounded-lg bg-slate-800/40 border border-slate-700/40">
            <div className="flex items-center gap-1.5 font-mono text-xs font-semibold text-sky-300">
              <Command className="w-3 h-3 text-sky-400" />
              <span>{item.cmd}</span>
            </div>
            <p className="text-[11px] text-slate-400 mt-1">{item.desc}</p>
          </div>
        ))}
      </div>
    </div>
  );
};
