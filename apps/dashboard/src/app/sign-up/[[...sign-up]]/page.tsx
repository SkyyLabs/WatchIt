import { SignUp } from "@clerk/nextjs";
import { serverLogger } from "../../../lib/server-logger";

export default function Page() {
  serverLogger.info({ route: "/sign-up" }, "render sign-up page");

  return <SignUp />;
}
