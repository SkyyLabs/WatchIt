import { SignIn } from "@clerk/nextjs";
import { serverLogger } from "../../../lib/server-logger";

export default function Page() {
  serverLogger.info({ route: "/sign-in" }, "render sign-in page");

  return <SignIn />;
}
