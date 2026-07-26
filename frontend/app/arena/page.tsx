import type { Metadata } from "next";

import { ArenaPanel } from "../../components/ArenaPanel";
import { SiteFooter } from "../../components/SiteFooter";
import { SiteNav } from "../../components/SiteNav";

export const metadata: Metadata = {
  title: "Arena — AgentGate",
  description:
    "A public adversarial suite runs against the live gate every six hours; the CI badge goes red on any false allow.",
};

export default function ArenaPage() {
  return (
    <>
      <SiteNav />
      <main className="mx-auto max-w-6xl px-6 py-10">
        <ArenaPanel />
      </main>
      <SiteFooter />
    </>
  );
}
