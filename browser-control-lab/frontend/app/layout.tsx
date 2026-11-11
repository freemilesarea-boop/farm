import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Browser Control Lab",
  description: "Local Playwright test-session control dashboard",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ko">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
