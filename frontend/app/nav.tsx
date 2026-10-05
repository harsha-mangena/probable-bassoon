"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const TABS = [
  { href: "/today", label: "Today" },
  { href: "/bookings", label: "Bookings" },
  { href: "/call", label: "Text Call" },
  { href: "/setup", label: "Setup" },
  { href: "/admin", label: "Admin" },
];

export default function Nav() {
  const path = usePathname();
  return (
    <nav>
      {TABS.map((t) => (
        <Link key={t.href} href={t.href} className={path === t.href ? "active" : ""}>
          {t.label}
        </Link>
      ))}
    </nav>
  );
}
