import Link from "next/link";
import { GITHUB_URL, SITE } from "../lib/site";

const links = [
  { href: "/#features", label: "Product" },
  { href: "/#how-it-works", label: "How it works" },
  { href: "/demo", label: "Live demo", testId: "nav-demo" },
  { href: "/arena", label: "Arena", testId: "nav-arena" },
  { href: GITHUB_URL, label: "Docs", external: true },
];

export function SiteNav({ active }: { active?: "home" | "demo" }) {
  return (
    <header className="sticky top-0 z-50 border-b border-white/10 bg-zinc-950/85 backdrop-blur-md">
      <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-6 py-4">
        <Link href="/" className="group flex items-center gap-2">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-violet-600 text-sm font-bold text-white">
            AG
          </span>
          <span className="text-lg font-semibold tracking-tight text-white group-hover:text-violet-200">
            {SITE.name}
          </span>
        </Link>

        <nav className="hidden items-center gap-1 md:flex">
          {links.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              data-testid={link.testId}
              target={link.external ? "_blank" : undefined}
              rel={link.external ? "noreferrer" : undefined}
              className={`rounded-lg px-3 py-2 text-sm transition-colors ${
                active === "demo" && link.href === "/demo"
                  ? "bg-white text-zinc-950"
                  : "text-zinc-400 hover:bg-white/5 hover:text-zinc-100"
              }`}
            >
              {link.label}
            </Link>
          ))}
        </nav>

        <div className="flex items-center gap-2">
          <Link
            href={GITHUB_URL}
            target="_blank"
            rel="noreferrer"
            className="hidden rounded-lg border border-white/10 px-3 py-2 text-sm text-zinc-300 hover:border-white/20 sm:inline-flex"
          >
            GitHub
          </Link>
          <Link
            href="/demo"
            data-testid="nav-get-started"
            className="rounded-lg bg-violet-600 px-4 py-2 text-sm font-medium text-white hover:bg-violet-500"
          >
            Get started
          </Link>
        </div>
      </div>
    </header>
  );
}
