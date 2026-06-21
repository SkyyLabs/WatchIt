import { ClerkProvider } from "@clerk/nextjs";
import { serverLogger } from "../lib/server-logger";

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
      <html lang="en">
        <body>{children}</body>
      </html>
    </ClerkProvider>
  )
}
