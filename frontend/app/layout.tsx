import type { Metadata } from "next";
import "./globals.css";
import Nav from "./nav";

export const metadata: Metadata = {
  title: "Front Desk",
  description: "Scheduling agent — every booking id comes from the server",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header>
          <h1>Front Desk</h1>
          <p>Scheduling agent · Glow Studio · every booking id comes from the server</p>
        </header>
        <Nav />
        <main>{children}</main>
      </body>
    </html>
  );
}
