import React from "react";
import { MessageSquare, Image, Video, FileText, CheckCircle2 } from "lucide-react";
import { PromoMessage } from "../types";

interface MessagesListProps {
  messages: PromoMessage[];
  currentIndex: number;
}

export const MessagesList: React.FC<MessagesListProps> = ({ messages, currentIndex }) => {
  const getIcon = (type: PromoMessage["type"]) => {
    switch (type) {
      case "photo":
        return <Image className="w-3.5 h-3.5 text-emerald-400" />;
      case "video":
        return <Video className="w-3.5 h-3.5 text-purple-400" />;
      case "text":
      default:
        return <FileText className="w-3.5 h-3.5 text-sky-400" />;
    }
  };

  return (
    <div className="border border-slate-800 bg-slate-900/60 rounded-xl p-4 flex flex-col h-full">
      <div className="flex items-center justify-between gap-2 mb-3">
        <div className="flex items-center gap-2">
          <MessageSquare className="w-4 h-4 text-purple-400" />
          <h2 className="text-sm font-semibold text-slate-100">Configured Promotion Messages</h2>
          <span className="text-xs text-slate-500">({messages.length})</span>
        </div>
        <span className="text-[11px] text-slate-400">
          Next to send: <strong className="text-sky-400">#{currentIndex + 1}</strong>
        </span>
      </div>

      <div className="space-y-2 overflow-y-auto max-h-[340px] pr-1">
        {messages.length === 0 ? (
          <div className="p-8 text-center text-xs text-slate-500 border border-dashed border-slate-800 rounded-lg">
            No promo messages configured. Use <code>/addmsg</code> in bot to add templates.
          </div>
        ) : (
          messages.map((msg, index) => {
            const isNext = index === currentIndex;
            return (
              <div
                key={msg.id}
                className={`p-3 rounded-lg border transition ${
                  isNext
                    ? "bg-sky-500/10 border-sky-500/40 ring-1 ring-sky-500/30"
                    : "bg-slate-800/40 border-slate-700/40"
                }`}
              >
                <div className="flex items-center justify-between text-xs mb-1.5">
                  <div className="flex items-center gap-1.5">
                    {getIcon(msg.type)}
                    <span className="font-semibold text-slate-200">Message #{index + 1}</span>
                    <span className="text-[10px] uppercase font-mono px-1.5 py-0.2 rounded bg-slate-700/80 text-slate-300">
                      {msg.type}
                    </span>
                  </div>

                  {isNext && (
                    <span className="text-[10px] font-medium px-2 py-0.5 rounded-full bg-sky-500/20 text-sky-300 border border-sky-500/40 flex items-center gap-1">
                      <CheckCircle2 className="w-2.5 h-2.5" /> Next in Queue
                    </span>
                  )}
                </div>

                <div className="text-xs text-slate-300 whitespace-pre-wrap line-clamp-3 bg-slate-900/50 p-2 rounded border border-slate-800 font-sans">
                  {msg.text || msg.caption || "(No text preview)"}
                </div>

                <div className="text-[10px] text-slate-500 mt-1.5 flex items-center justify-between">
                  <span>Added: {msg.addedAt}</span>
                  {msg.mediaFileId && (
                    <span className="font-mono text-[9px] text-slate-500 truncate max-w-[150px]">
                      File: {msg.mediaFileId}
                    </span>
                  )}
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
};
