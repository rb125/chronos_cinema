import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Chronos Cinema — AI Documentary Studio",
  description:
    "A next-generation AI agent that generates live, multimodal educational documentaries with synchronized narration, video, and music.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-cinema-black text-cinema-text antialiased font-sans">
        {children}
      </body>
    </html>
  );
}
