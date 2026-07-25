import { redirect } from "next/navigation";

export default async function VerifyRedirect() {
  redirect("/demo");
}
