import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import Navbar from "@/components/Navbar";

const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Neo v2 — Board Meeting Intelligence",
  description: "Cross-college trustee intelligence platform for HCC",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className={`${inter.className} bg-slate-50 text-slate-900 antialiased`}>
        <Navbar />
        <main className="mx-auto max-w-screen-2xl px-6 py-8">
          {children}
        </main>
      </body>
    </html>
  );
}
