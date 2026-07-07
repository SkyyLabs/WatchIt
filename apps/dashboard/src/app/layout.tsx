import { ClerkProvider } from "@clerk/nextjs";
import { TooltipProvider } from "@/components/ui/tooltip";
import { DashboardDataProvider } from "@/lib/dashboard-data";
import { serverLogger } from "../lib/server-logger";
import "./globals.css";

export const metadata = {
  title: "WatchIt",
  description: "Guardian console for WatchIt",
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  serverLogger.info({ route: "root-layout" }, "render dashboard root layout");

  return (
    <ClerkProvider>
      <html lang="en" className="font-sans">
        <body className="antialiased">
          <TooltipProvider>
            <DashboardDataProvider>{children}</DashboardDataProvider>
          </TooltipProvider>
        </body>
      </html>
    </ClerkProvider>
  )
}
