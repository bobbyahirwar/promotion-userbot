import React, { useState } from "react";
import { Terminal, CheckCircle2, XCircle, Trash2 } from "lucide-react";
import { CycleLog } from "../types";

interface LogsViewerProps {
  logs: CycleLog[];
  onClearLogs: () => void;
}

export const LogsViewer: React.FC<LogsViewerProps> = ({ logs, onClearLogs }) => {
  const [filter, setFilter] = useState<"all" | "success" | "failed">("all");

  const filteredLogs = logs.filter((log) => {
    if (filter === "all") return true;
    return log.status === filter;
  });

  return (
    <div className="border border-slate-800 bg-slate-900/60 rounded-xl p-4 flex flex-col">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
        <div className="flex items-center gap-2">
          <Terminal className="w-4 h-4 text-emerald-400" />
          <h2 className="text-sm font-semibold text-slate-100">Live MTProto Dispatch Logs</h2>
          <span className="text-xs text-slate-500">({logs.length} events)</span>
        </div>

        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1 bg-slate-800/80 p-0.5 rounded-lg border border-slate-700/80 text-xs">
            <button
              onClick={() => setFilter("all")}
              className={`px-2 py-0.5 rounded transition ${
                filter === "all" ? "bg-slate-700 text-slate-100 font-medium" : "text-slate-400"
              }`}
            >
              All
            </button>
            <button
              onClick={() => setFilter("success")}
              className={`px-2 py-0.5 rounded transition ${
                filter === "success" ? "bg-emerald-500/20 text-emerald-300 font-medium" : "text-slate-400"
              }`}
            >
              Success
            </button>
            <button
              onClick={() => setFilter("failed")}
              className={`px-2 py-0.5 rounded transition ${
                filter === "failed" ? "bg-rose-500/20 text-rose-300 font-medium" : "text-slate-400"
              }`}
            >
              Errors
            </button>
          </div>

          <button
            onClick={onClearLogs}
            className="flex items-center gap-1 px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-400 hover:text-slate-200 text-xs transition cursor-pointer"
          >
            <Trash2 className="w-3 h-3" />
            <span>Clear</span>
          </button>
        </div>
      </div>

      <div className="bg-slate-950/80 border border-slate-800 rounded-lg p-3 font-mono text-xs max-h-[300px] overflow-y-auto space-y-1.5">
        {filteredLogs.length === 0 ? (
          <div className="text-slate-600 text-center py-6">
            No dispatch logs yet. When promotion loop runs, MTProto sending events appear here.
          </div>
        ) : (
          filteredLogs.map((log) => (
            <div
              key={log.id}
              className={`flex items-start gap-2 py-1 px-2 rounded ${
                log.status === "success"
                  ? "hover:bg-emerald-950/20 text-slate-300"
                  : "hover:bg-rose-950/20 text-rose-300"
              }`}
            >
              <span className="text-slate-600 shrink-0 text-[10px]">{log.timestamp}</span>
              {log.status === "success" ? (
                <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400 shrink-0 mt-0.5" />
              ) : (
                <XCircle className="w-3.5 h-3.5 text-rose-400 shrink-0 mt-0.5" />
              )}
              <span className="text-sky-400 shrink-0">[{log.targetChat}]</span>
              <span className="text-slate-400 truncate">{log.details}</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
};
