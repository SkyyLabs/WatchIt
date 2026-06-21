import pino from "pino";

export const serverLogger = pino({
  name: "watchit-dashboard",
  level: process.env.WATCHIT_LOG_LEVEL || process.env.LOG_LEVEL || "info",
  base: {
    service: "dashboard",
    runtime: "nextjs",
  },
  timestamp: pino.stdTimeFunctions.isoTime,
});
