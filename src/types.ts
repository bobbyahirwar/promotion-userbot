export interface PromoStats {
  totalSent: number;
  totalSuccess: number;
  totalFailed: number;
  activeGroupsCount: number;
  totalGroupsCount: number;
  messagesCount: number;
  currentMessageIndex: number;
  cycleIntervalSeconds: number;
  rateLimitRange: [number, number];
  consecutiveErrors: number;
  isPromoRunning: boolean;
  cooldownActive: boolean;
  cooldownRemainingSeconds: number;
  cooldownReason: string | null;
  safetyPaused: boolean;
  lastCycleDurationSeconds: number;
  uptimeSeconds: number;
}

export interface PromoMessage {
  id: string;
  type: "text" | "photo" | "video";
  text?: string;
  caption?: string;
  mediaFileId?: string;
  addedAt: string;
}

export interface TargetGroup {
  id: string;
  chatId: number | string;
  title: string;
  username?: string;
  membersCount?: number;
  status: "active" | "inactive" | "blacklisted" | "slowmode" | "forbidden";
  lastSentAt?: string;
  lastError?: string;
}

export interface CycleLog {
  id: string;
  timestamp: string;
  cycleId: string;
  targetChat: string;
  chatId: string | number;
  status: "success" | "failed" | "skipped" | "cooldown";
  details: string;
  messageType: "text" | "photo" | "video";
}
