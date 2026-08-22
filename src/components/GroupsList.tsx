import React, { useState } from "react";
import { Users, Search, Check, AlertCircle, Ban, Clock } from "lucide-react";
import { TargetGroup } from "../types";

interface GroupsListProps {
  groups: TargetGroup[];
  onToggleStatus: (id: string) => void;
}

export const GroupsList: React.FC<GroupsListProps> = ({ groups, onToggleStatus }) => {
  const [searchTerm, setSearchTerm] = useState("");
  const [filter, setFilter] = useState<"all" | "active" | "inactive" | "blacklisted">("all");

  const filteredGroups = groups.filter((g) => {
    const matchesSearch =
      g.title.toLowerCase().includes(searchTerm.toLowerCase()) ||
      String(g.chatId).includes(searchTerm) ||
      (g.username && g.username.toLowerCase().includes(searchTerm.toLowerCase()));

    if (!matchesSearch) return false;
    if (filter === "all") return true;
    return g.status === filter;
  });

  const getStatusBadge = (status: TargetGroup["status"]) => {
    switch (status) {
      case "active":
        return (
          <span className="px-2 py-0.5 rounded-full text-[10px] font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 flex items-center gap-1">
            <Check className="w-2.5 h-2.5" /> Active
          </span>
        );
      case "blacklisted":
        return (
          <span className="px-2 py-0.5 rounded-full text-[10px] font-medium bg-rose-500/10 text-rose-400 border border-rose-500/30 flex items-center gap-1">
            <Ban className="w-2.5 h-2.5" /> Blacklisted
          </span>
        );
      case "forbidden":
        return (
          <span className="px-2 py-0.5 rounded-full text-[10px] font-medium bg-amber-500/10 text-amber-400 border border-amber-500/30 flex items-center gap-1">
            <AlertCircle className="w-2.5 h-2.5" /> No Permission
          </span>
        );
      case "inactive":
      default:
        return (
          <span className="px-2 py-0.5 rounded-full text-[10px] font-medium bg-slate-800 text-slate-400 border border-slate-700 flex items-center gap-1">
            <Clock className="w-2.5 h-2.5" /> Inactive
          </span>
        );
    }
  };

  return (
    <div className="border border-slate-800 bg-slate-900/60 rounded-xl p-4 flex flex-col h-full">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
        <div className="flex items-center gap-2">
          <Users className="w-4 h-4 text-sky-400" />
          <h2 className="text-sm font-semibold text-slate-100">Target Groups & Channels</h2>
          <span className="text-xs text-slate-500">({groups.length})</span>
        </div>

        <div className="flex items-center gap-2">
          <div className="relative">
            <Search className="w-3.5 h-3.5 text-slate-500 absolute left-2.5 top-1/2 -translate-y-1/2" />
            <input
              type="text"
              placeholder="Search chat ID or title..."
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="pl-8 pr-3 py-1 bg-slate-800/80 border border-slate-700/80 rounded-lg text-xs text-slate-200 placeholder:text-slate-500 focus:outline-none focus:border-sky-500 w-44"
            />
          </div>

          <select
            value={filter}
            onChange={(e) => setFilter(e.target.value as any)}
            className="bg-slate-800/80 border border-slate-700/80 rounded-lg text-xs text-slate-300 px-2 py-1 focus:outline-none"
          >
            <option value="all">All</option>
            <option value="active">Active</option>
            <option value="inactive">Inactive</option>
            <option value="blacklisted">Blacklisted</option>
          </select>
        </div>
      </div>

      <div className="overflow-y-auto max-h-[340px] space-y-2 pr-1">
        {filteredGroups.length === 0 ? (
          <div className="p-8 text-center text-xs text-slate-500 border border-dashed border-slate-800 rounded-lg">
            No matching groups found. Run <code>/syncgroups</code> in bot to sync from userbot dialogs.
          </div>
        ) : (
          filteredGroups.map((g) => (
            <div
              key={g.id}
              className="p-3 rounded-lg bg-slate-800/40 border border-slate-700/40 flex items-center justify-between hover:bg-slate-800/70 transition"
            >
              <div className="min-w-0 pr-2">
                <div className="flex items-center gap-2">
                  <span className="text-xs font-medium text-slate-200 truncate">{g.title}</span>
                  {g.username && (
                    <span className="text-[10px] text-sky-400 truncate">@{g.username}</span>
                  )}
                </div>
                <div className="flex items-center gap-2 text-[10px] text-slate-500 mt-0.5">
                  <span>ID: {g.chatId}</span>
                  {g.membersCount && <span>• {g.membersCount} members</span>}
                  {g.lastSentAt && <span>• Last sent: {g.lastSentAt}</span>}
                  {g.lastError && (
                    <span className="text-rose-400 font-mono">• Err: {g.lastError}</span>
                  )}
                </div>
              </div>

              <div className="flex items-center gap-2 shrink-0">
                {getStatusBadge(g.status)}
                <button
                  onClick={() => onToggleStatus(g.id)}
                  title="Toggle blacklist/active"
                  className="px-2 py-1 rounded text-[10px] bg-slate-700/60 hover:bg-slate-700 text-slate-300 transition cursor-pointer"
                >
                  Toggle
                </button>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
};
